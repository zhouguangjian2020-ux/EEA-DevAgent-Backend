"""Small, project-scoped permission administration service.

Only fixed system roles are exposed. There is no arbitrary permission composition
or explicit deny rule in this first implementation.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole, project_connection
from app.schemas.permissions import (
    FunctionPermissionRead,
    FunctionPermissionUpsert,
    PermissionFunctionRead,
    PermissionSettingsResponse,
    ProjectMemberRead,
    ProjectMemberUpsert,
    ProjectPermissionRole,
)
from app.services.business_access import authorize, capabilities
from app.services.manual_work import write_transaction
from app.services.projects import ProjectReadError, _SELECT

PROJECT_ROLE_CODES = ("project_api_author", "project_api_reader")
FUNCTION_ROLE_CODES = ("function_viewer", "function_editor")
ROLE_DISPLAY_NAMES = {
    "project_api_author": "项目管理员",
    "project_api_reader": "项目查看者",
    "function_viewer": "功能查看者",
    "function_editor": "功能编辑者",
}


def _department(row: dict) -> str:
    return str(row.get("department") or "")


def _project_row(db, subject: str, project_id: UUID) -> dict:
    row = db.execute(_SELECT + " AND p.id=%s", (subject, project_id)).fetchone()
    if not row:
        raise ProjectReadError(404, "project_not_found", "项目不存在或无权访问")
    return row


def _require_manager(db, subject: str, project_id: UUID) -> UUID:
    return authorize(db, subject, project_id, "project.manage_access")


def _role_rows(db, codes: tuple[str, ...]) -> list[ProjectPermissionRole]:
    rows = db.execute(
        "SELECT code,name,description FROM ee.access_roles "
        "WHERE code = ANY(%s) AND enabled ORDER BY array_position(%s::text[],code)",
        (list(codes), list(codes)),
    ).fetchall()
    return [
        ProjectPermissionRole(
            code=row["code"],
            name=ROLE_DISPLAY_NAMES.get(row["code"], row["name"]),
            description=row.get("description"),
        )
        for row in rows
    ]


def _read_settings(subject: str, project_id: UUID) -> PermissionSettingsResponse:
    try:
        with project_connection() as db:
            project = _project_row(db, subject, project_id)
            can_manage = "project.manage_access" in capabilities(db, subject, project_id)
            if not can_manage:
                raise ProjectReadError(403, "permission_manage_forbidden", "只有项目管理员可以管理项目权限")
            project_roles = _role_rows(db, PROJECT_ROLE_CODES)
            function_roles = _role_rows(db, FUNCTION_ROLE_CODES)
            members_rows = db.execute(
                """
                SELECT u.id AS user_id, c.login_name, u.display_name,
                       u.metadata->>'department' AS department, u.state,
                       m.state AS project_state, r.code AS role_code, r.name AS role_name
                FROM ee.project_members m
                JOIN ee.app_users u ON u.id=m.user_id
                LEFT JOIN ee.app_credentials c ON c.user_id=u.id
                LEFT JOIN ee.access_bindings b ON b.project_id=m.project_id
                    AND b.user_id=m.user_id AND b.scope_kind='project'
                LEFT JOIN ee.access_roles r ON r.id=b.role_id
                WHERE m.project_id=%s
                ORDER BY u.display_name, u.id
                """,
                (project_id,),
            ).fetchall()
            functions_rows = db.execute(
                "SELECT id,code,name FROM ee.business_functions "
                "WHERE project_id=%s AND archived_at IS NULL ORDER BY code,id",
                (project_id,),
            ).fetchall()
            function_permission_rows = db.execute(
                """
                SELECT b.function_id, u.id AS user_id, c.login_name, u.display_name,
                       u.metadata->>'department' AS department, r.code AS role_code, r.name AS role_name
                FROM ee.access_bindings b
                JOIN ee.app_users u ON u.id=b.user_id AND u.state='active'
                LEFT JOIN ee.app_credentials c ON c.user_id=u.id
                JOIN ee.access_roles r ON r.id=b.role_id AND r.enabled
                WHERE b.project_id=%s AND b.scope_kind='function' AND b.artifact_id IS NULL
                ORDER BY b.function_id, u.display_name, u.id
                """,
                (project_id,),
            ).fetchall()
    except ProjectReadError:
        raise
    except (ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole) as error:
        raise ProjectReadError(503, "permission_database_not_configured", "权限管理需要已配置的低权限数据库连接") from error
    except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as error:
        raise ProjectReadError(504, "permission_query_timeout", "权限查询超时，请稍后重试") from error
    except psycopg.Error as error:
        raise ProjectReadError(503, "permission_database_unavailable", "权限查询暂不可用") from error

    members = [
        ProjectMemberRead(
            user_id=row["user_id"], login_name=row.get("login_name") or "",
            display_name=row["display_name"], department=_department(row), state=row["state"],
            project_state=row["project_state"], role_code=row.get("role_code") or "",
            role_name=row.get("role_name") or "未分配",
        )
        for row in members_rows
    ]
    permissions_by_function: dict[UUID, list[FunctionPermissionRead]] = {row["id"]: [] for row in functions_rows}
    for row in function_permission_rows:
        if row["function_id"] in permissions_by_function:
            permissions_by_function[row["function_id"]].append(
                FunctionPermissionRead(
                    user_id=row["user_id"], login_name=row.get("login_name") or "",
                    display_name=row["display_name"], department=_department(row),
                    role_code=row["role_code"], role_name=row["role_name"],
                )
            )
    functions = [
        PermissionFunctionRead(id=row["id"], code=row["code"], name=row["name"], permissions=permissions_by_function[row["id"]])
        for row in functions_rows
    ]
    return PermissionSettingsResponse(
        project_id=project_id, project_name=project["name"], can_manage=can_manage,
        project_roles=project_roles, function_roles=function_roles,
        members=members, functions=functions,
    )


def get_permission_settings(subject: str, project_id: UUID) -> PermissionSettingsResponse:
    return _read_settings(subject, project_id)


def _ensure_active_user(db, user_id: UUID) -> dict:
    row = db.execute(
        "SELECT id,external_subject,state FROM ee.app_users WHERE id=%s",
        (user_id,),
    ).fetchone()
    if not row or row["state"] != "active":
        raise ProjectReadError(404, "account_not_found", "账号不存在或已停用")
    return row


def _role_id(db, role_code: str, allowed: tuple[str, ...]) -> UUID:
    if role_code not in allowed:
        raise ProjectReadError(422, "permission_role_invalid", "权限角色不在首期固定角色范围内")
    row = db.execute("SELECT id FROM ee.access_roles WHERE code=%s AND enabled", (role_code,)).fetchone()
    if not row:
        raise ProjectReadError(503, "permission_role_not_ready", "权限角色尚未完成数据库部署")
    return row["id"]


def upsert_member(subject: str, project_id: UUID, payload: ProjectMemberUpsert, request_id: str) -> None:
    with write_transaction() as db:
        actor = _require_manager(db, subject, project_id)
        _ensure_active_user(db, payload.user_id)
        role_id = _role_id(db, payload.role_code, PROJECT_ROLE_CODES)
        if db.execute("SELECT 1 FROM ee.projects WHERE id=%s AND archived_at IS NULL", (project_id,)).fetchone() is None:
            raise ProjectReadError(404, "project_not_found", "项目不存在或已归档")
        db.execute(
            """INSERT INTO ee.project_members(project_id,user_id,state,added_by)
               VALUES(%s,%s,'active',%s)
               ON CONFLICT(project_id,user_id) DO UPDATE SET state='active',updated_at=clock_timestamp()""",
            (project_id, payload.user_id, actor),
        )
        binding = db.execute(
            "SELECT id FROM ee.access_bindings WHERE project_id=%s AND user_id=%s AND scope_kind='project' AND function_id IS NULL AND artifact_id IS NULL FOR UPDATE",
            (project_id, payload.user_id),
        ).fetchone()
        if binding:
            db.execute("UPDATE ee.access_bindings SET role_id=%s,updated_by=%s,updated_at=clock_timestamp() WHERE id=%s", (role_id, actor, binding["id"]))
        else:
            db.execute(
                "INSERT INTO ee.access_bindings(id,project_id,user_id,scope_kind,role_id,created_by,updated_by) VALUES(%s,%s,%s,'project',%s,%s,%s)",
                (uuid4(), project_id, payload.user_id, role_id, actor, actor),
            )
        db.execute(
            "INSERT INTO ee.security_audit_events(id,project_id,actor_id,subject_user_id,event_type,scope_kind,after_state,reason,request_id) VALUES(%s,%s,%s,%s,'project.member.upsert','project',%s,%s,%s)",
            (uuid4(), project_id, actor, payload.user_id, Jsonb({"role_code": payload.role_code}), "项目管理员调整项目成员角色", request_id),
        )


def remove_member(subject: str, project_id: UUID, user_id: UUID, request_id: str) -> None:
    with write_transaction() as db:
        actor = _require_manager(db, subject, project_id)
        member = db.execute("SELECT state FROM ee.project_members WHERE project_id=%s AND user_id=%s FOR UPDATE", (project_id, user_id)).fetchone()
        if not member:
            raise ProjectReadError(404, "project_member_not_found", "项目成员不存在")
        if db.execute("SELECT 1 FROM ee.projects WHERE id=%s AND created_by=%s", (project_id, user_id)).fetchone():
            raise ProjectReadError(409, "project_author_required", "项目创建人不能移出项目")
        removed_function_permissions = db.execute(
            "DELETE FROM ee.access_bindings "
            "WHERE project_id=%s AND user_id=%s AND scope_kind='function' "
            "AND artifact_id IS NULL RETURNING id",
            (project_id, user_id),
        ).fetchall()
        db.execute("UPDATE ee.project_members SET state='disabled',updated_at=clock_timestamp() WHERE project_id=%s AND user_id=%s", (project_id, user_id))
        db.execute(
            "INSERT INTO ee.security_audit_events(id,project_id,actor_id,subject_user_id,event_type,scope_kind,after_state,reason,request_id) VALUES(%s,%s,%s,%s,'project.member.remove','project',%s,%s,%s)",
            (uuid4(), project_id, actor, user_id, Jsonb({"state": "disabled", "function_permissions_removed": len(removed_function_permissions)}), "项目管理员移出项目成员并撤销其功能级权限", request_id),
        )


def upsert_function_permission(subject: str, project_id: UUID, function_id: UUID, payload: FunctionPermissionUpsert, request_id: str) -> None:
    with write_transaction() as db:
        actor = _require_manager(db, subject, project_id)
        _ensure_active_user(db, payload.user_id)
        if not db.execute("SELECT 1 FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL", (function_id, project_id)).fetchone():
            raise ProjectReadError(404, "function_not_found", "功能不存在或已归档")
        if not db.execute("SELECT 1 FROM ee.project_members WHERE project_id=%s AND user_id=%s AND state='active'", (project_id, payload.user_id)).fetchone():
            raise ProjectReadError(409, "project_member_required", "账号必须先加入当前项目，才能分配功能权限")
        role_id = _role_id(db, payload.role_code, FUNCTION_ROLE_CODES)
        binding = db.execute(
            "SELECT id FROM ee.access_bindings WHERE project_id=%s AND user_id=%s AND scope_kind='function' AND function_id=%s AND artifact_id IS NULL FOR UPDATE",
            (project_id, payload.user_id, function_id),
        ).fetchone()
        if binding:
            db.execute("UPDATE ee.access_bindings SET role_id=%s,updated_by=%s,updated_at=clock_timestamp() WHERE id=%s", (role_id, actor, binding["id"]))
        else:
            db.execute(
                "INSERT INTO ee.access_bindings(id,project_id,user_id,scope_kind,function_id,role_id,created_by,updated_by) VALUES(%s,%s,%s,'function',%s,%s,%s,%s)",
                (uuid4(), project_id, payload.user_id, function_id, role_id, actor, actor),
            )
        db.execute(
            "INSERT INTO ee.security_audit_events(id,project_id,actor_id,subject_user_id,event_type,scope_kind,function_id,after_state,reason,request_id) VALUES(%s,%s,%s,%s,'function.permission.upsert','function',%s,%s,%s,%s)",
            (uuid4(), project_id, actor, payload.user_id, function_id, Jsonb({"role_code": payload.role_code}), "项目管理员调整功能权限", request_id),
        )


def remove_function_permission(subject: str, project_id: UUID, function_id: UUID, user_id: UUID, request_id: str) -> None:
    with write_transaction() as db:
        actor = _require_manager(db, subject, project_id)
        deleted = db.execute(
            "DELETE FROM ee.access_bindings WHERE project_id=%s AND user_id=%s AND scope_kind='function' AND function_id=%s AND artifact_id IS NULL RETURNING id",
            (project_id, user_id, function_id),
        ).fetchone()
        if not deleted:
            raise ProjectReadError(404, "function_permission_not_found", "功能权限不存在")
        db.execute(
            "INSERT INTO ee.security_audit_events(id,project_id,actor_id,subject_user_id,event_type,scope_kind,function_id,after_state,reason,request_id) VALUES(%s,%s,%s,%s,'function.permission.remove','function',%s,%s,%s,%s)",
            (uuid4(), project_id, actor, user_id, function_id, Jsonb({"removed": True}), "项目管理员移除功能权限；成员回退到项目级权限", request_id),
        )


def account_candidates(subject: str, project_id: UUID) -> list[dict]:
    try:
        with project_connection() as db:
            _require_manager(db, subject, project_id)
            return db.execute(
                """
                SELECT u.id,c.login_name,u.display_name,u.metadata->>'department' AS department
                FROM ee.app_users u LEFT JOIN ee.app_credentials c ON c.user_id=u.id
                WHERE u.state='active' AND NOT EXISTS(
                  SELECT 1 FROM ee.project_members m WHERE m.project_id=%s AND m.user_id=u.id AND m.state='active'
                ) ORDER BY u.display_name,u.id
                """,
                (project_id,),
            ).fetchall()
    except ProjectReadError:
        raise
    except (ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole) as error:
        raise ProjectReadError(503, "permission_database_not_configured", "权限管理需要已配置的低权限数据库连接") from error
    except psycopg.Error as error:
        raise ProjectReadError(503, "permission_database_unavailable", "账号查询暂不可用") from error
