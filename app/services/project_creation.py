"""Create a project, membership, grants, default catalogue and audit atomically."""
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.core.config import settings
from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole
from app.core.project_write_database import project_write_connection
from app.schemas.projects import ProjectCreate, ProjectRead
from app.services.projects import ProjectReadError, _SELECT, project_read
from app.services.default_subsystems import require_default_catalogue, initialize_default_catalogue

_COLUMNS = "id, code, name, description, lock_version::text AS lock_version, created_at, updated_at"


def create_project(subject: str, payload: ProjectCreate, request_id: str) -> tuple[ProjectRead, bool]:
    if subject not in settings.projects_create_subjects:
        raise ProjectReadError(403, "project_create_forbidden", "当前账号没有新增项目权限")
    try:
        with project_write_connection() as db:
            user = db.execute(
                "SELECT id FROM ee.app_users WHERE external_subject=%s AND state='active'", (subject,)
            ).fetchone()
            if not user:
                raise ProjectReadError(403, "project_create_forbidden", "当前账号未启用，无法新增项目")
            actor = user["id"]
            # Serializes retries of this UUID before any INSERT. No extra table or UPDATE privilege.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(payload.creation_id),))
            existing = db.execute(
                "SELECT created_by, name, description, code, archived_at FROM ee.projects WHERE id=%s",
                (payload.creation_id,),
            ).fetchone()
            code = "PRJ-" + payload.creation_id.hex.upper()
            if existing:
                authorized = db.execute(_SELECT + " AND p.id=%s", (subject, payload.creation_id)).fetchone()
                if (existing["created_by"] != actor or existing["name"] != payload.name
                    or existing["description"] != payload.description or existing["code"] != code
                    or existing["archived_at"] is not None or authorized is None):
                    raise ProjectReadError(409, "project_creation_conflict", "创建标识已使用且无法复用，请刷新项目列表核对")
                return project_read(authorized), False

            # New projects get author capabilities only after the deployment opt-in.
            role_code = "project_api_author" if settings.business_writes_enabled else "project_api_reader"
            expected = {"project.read"}
            if settings.business_writes_enabled:
                expected |= {"function.create"} | {kind + "." + action for kind in ("prd","uc","fr") for action in ("create","update","accept")}
            role = db.execute("SELECT id FROM ee.access_roles WHERE code=%s AND enabled", (role_code,)).fetchone()
            if role:
                permissions = db.execute("""SELECT pd.code,pd.resource_kind,pd.action,pd.allowed_scopes,pd.enabled
                    FROM ee.role_permissions rp JOIN ee.permission_definitions pd ON pd.code=rp.permission_code
                    WHERE rp.role_id=%s""", (role["id"],)).fetchall()
                allowed_sets = [expected]
                if settings.business_writes_enabled:
                    metadata_set = expected | {"project.update", "function.update"}
                    allowed_sets.append(metadata_set)
                    deletion_set = metadata_set | {kind + ".archive" for kind in ("project", "function", "prd", "uc", "fr", "fr_allocation")}
                    allowed_sets.append(deletion_set)
                    # permission-admin-20260929 requires the metadata baseline;
                    # deletion is optional. Keep exact sets, never accept extras.
                    allowed_sets.append(metadata_set | {"project.manage_access"})
                    allowed_sets.append(deletion_set | {"project.manage_access"})
                if settings.business_writes_enabled:
                    workflow_permissions = {'fr_allocation.create','fr_allocation.update','fr_allocation.accept'}
                    allowed_sets += [base | workflow_permissions for base in tuple(allowed_sets)]
                if {p["code"] for p in permissions} not in allowed_sets or any(
                    not p["enabled"] or "project" not in p["allowed_scopes"] or
                    p["resource_kind"].lower() + "." + p["action"] != p["code"] for p in permissions
                ):
                    role = None
            if not role:
                raise ProjectReadError(503, "project_create_role_not_ready", "项目初始权限角色尚未就绪，请联系维护人员")
            require_default_catalogue(db)
            row = db.execute(
                "INSERT INTO ee.projects(id,code,name,description,created_by) VALUES(%s,%s,%s,%s,%s) "
                "RETURNING " + _COLUMNS,
                (payload.creation_id, code, payload.name, payload.description, actor),
            ).fetchone()
            db.execute("INSERT INTO ee.project_members(project_id,user_id,added_by) VALUES(%s,%s,%s)",
                       (payload.creation_id, actor, actor))
            db.execute(
                "INSERT INTO ee.access_bindings(id,project_id,user_id,scope_kind,role_id,created_by,updated_by) "
                "VALUES(%s,%s,%s,'project',%s,%s,%s)",
                (uuid4(), payload.creation_id, actor, role["id"], actor, actor),
            )
            db.execute(
                "INSERT INTO ee.project_subsystem_designs(project_id,name,created_by,updated_by) VALUES(%s,%s,%s,%s)",
                (payload.creation_id, "子系统划分方案", actor, actor),
            )
            catalogue = initialize_default_catalogue(db, payload.creation_id, actor, request_id)
            db.execute(
                "INSERT INTO ee.security_audit_events(id,project_id,actor_id,subject_user_id,event_type,scope_kind,"
                "after_state,reason,request_id) VALUES(%s,%s,%s,%s,'project.create','project',%s,%s,%s)",
                (uuid4(), payload.creation_id, actor, actor,
                 Jsonb({"code": code, "name": payload.name, "granted_role": role_code, "subsystem_scheme": "default", "subsystem_catalogue": catalogue}),
                 "用户创建项目；初始化本人成员、项目权限及默认43项子系统目录，不复制需求或分配", request_id),
            )
            return project_read({
                **row,
                "can_update": settings.business_writes_enabled and any(p["code"] == "project.update" for p in permissions),
                "can_delete": settings.business_writes_enabled and any(p["code"] == "project.archive" for p in permissions),
                "can_manage_access": settings.business_writes_enabled and any(p["code"] == "project.manage_access" for p in permissions),
            }), True
    except ProjectDatabaseNotConfigured as error:
        raise ProjectReadError(503, "project_create_not_configured", "项目创建数据库尚未配置") from error
    except UnsafeProjectDatabaseRole as error:
        raise ProjectReadError(503, "project_create_unsafe_database_role", "项目创建需要专用低权限数据库账号") from error
    except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as error:
        raise ProjectReadError(504, "project_create_timeout", "创建请求超时，请使用原表单重试核对结果，不要重复新建") from error
    except psycopg.errors.UniqueViolation as error:
        raise ProjectReadError(409, "project_creation_conflict", "创建标识冲突，请刷新项目列表核对") from error
    except (psycopg.Error, ValueError) as error:
        # Do not expose SQL, DSNs, raw driver errors or passwords.
        raise ProjectReadError(503, "project_create_unavailable", "创建请求未能确认完成，请保留原表单重试核对") from error
