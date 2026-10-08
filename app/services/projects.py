"""SELECT-only access to ee.projects with current database-backed project permissions."""
from uuid import UUID

import psycopg

from app.core.database import (
    ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole, project_connection,
)
from app.schemas.projects import ProjectCapabilities, ProjectListResponse, ProjectRead
from app.core.config import settings

# Always parameterize the trusted authenticated subject and caller-supplied values.
# EXISTS prevents duplicate bindings from duplicating projects or breaking pagination.
_SELECT = """
SELECT p.id, p.code, p.name, p.description, p.lock_version::text AS lock_version,
       p.created_at, p.updated_at,
       EXISTS (
           SELECT 1 FROM ee.app_users u
           JOIN ee.project_members m ON m.user_id=u.id AND m.project_id=p.id AND m.state='active'
           JOIN ee.access_bindings b ON b.user_id=u.id AND b.project_id=p.id AND b.scope_kind='project'
           JOIN ee.access_roles r ON r.id=b.role_id AND r.enabled
           JOIN ee.role_permissions rp ON rp.role_id=r.id
           JOIN ee.permission_definitions pd ON pd.code=rp.permission_code AND pd.enabled
           WHERE u.external_subject=caller.subject AND u.state='active'
             AND pd.resource_kind='project' AND pd.action='update' AND 'project'=ANY(pd.allowed_scopes)
       ) AS can_update,
       EXISTS (
           SELECT 1 FROM ee.app_users u
           JOIN ee.project_members m ON m.user_id=u.id AND m.project_id=p.id AND m.state='active'
           JOIN ee.access_bindings b ON b.user_id=u.id AND b.project_id=p.id AND b.scope_kind='project'
           JOIN ee.access_roles r ON r.id=b.role_id AND r.enabled
           JOIN ee.role_permissions rp ON rp.role_id=r.id
           JOIN ee.permission_definitions pd ON pd.code=rp.permission_code AND pd.enabled
           WHERE u.external_subject=caller.subject AND u.state='active'
             AND pd.resource_kind='project' AND pd.action='archive' AND 'project'=ANY(pd.allowed_scopes)
       ) AS can_delete,
       EXISTS (
           SELECT 1 FROM ee.app_users u
           JOIN ee.project_members m ON m.user_id=u.id AND m.project_id=p.id AND m.state='active'
           JOIN ee.access_bindings b ON b.user_id=u.id AND b.project_id=p.id AND b.scope_kind='project'
           JOIN ee.access_roles r ON r.id=b.role_id AND r.enabled
           JOIN ee.role_permissions rp ON rp.role_id=r.id
           JOIN ee.permission_definitions pd ON pd.code=rp.permission_code AND pd.enabled
           WHERE u.external_subject=caller.subject AND u.state='active'
             AND pd.resource_kind='project' AND pd.action='manage_access' AND 'project'=ANY(pd.allowed_scopes)
       ) AS can_manage_access
FROM ee.projects p CROSS JOIN (SELECT %s::text AS subject) caller
WHERE p.archived_at IS NULL AND EXISTS (
    SELECT 1
    FROM ee.app_users u
    JOIN ee.project_members m ON m.user_id = u.id AND m.project_id = p.id AND m.state = 'active'
    JOIN ee.access_bindings b ON b.user_id = u.id AND b.project_id = p.id
        AND b.scope_kind = 'project' AND b.function_id IS NULL AND b.artifact_id IS NULL
    JOIN ee.access_roles role ON role.id = b.role_id AND role.enabled
    JOIN ee.role_permissions rp ON rp.role_id = role.id AND rp.permission_code = 'project.read'
    JOIN ee.permission_definitions perm ON perm.code = rp.permission_code
        AND perm.enabled AND perm.resource_kind = 'project' AND perm.action = 'read'
        AND 'project' = ANY(perm.allowed_scopes)
    WHERE u.external_subject = caller.subject AND u.state = 'active'
)
"""


class ProjectReadError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(code)


def _read(statement: str, params: tuple) -> list[dict]:
    try:
        with project_connection() as connection:
            return connection.execute(statement, params).fetchall()
    except ProjectDatabaseNotConfigured as error:
        raise ProjectReadError(503, 'projects_not_configured', '项目数据库尚未配置') from error
    except UnsafeProjectDatabaseRole as error:
        raise ProjectReadError(503, 'projects_unsafe_database_role', '项目接口需要专用低权限数据库账号') from error
    except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as error:
        raise ProjectReadError(504, 'projects_query_timeout', '项目查询超时，请稍后重试') from error
    except (psycopg.Error, ValueError) as error:
        # Never return/log DSNs, SQL text, password, or raw database error messages.
        raise ProjectReadError(503, 'projects_database_unavailable', '项目数据库暂不可用') from error


def project_read(row: dict) -> ProjectRead:
    enabled = settings.business_writes_enabled and bool(settings.projects_write_database_url.get_secret_value().strip())
    return ProjectRead.model_validate({**row, "can_update": enabled and bool(row.get("can_update", False)), "can_delete": enabled and bool(row.get("can_delete", False)), "can_manage_access": enabled and bool(row.get("can_manage_access", False))})


def list_projects(subject: str, limit: int, offset: int) -> ProjectListResponse:
    rows = _read(_SELECT + ' ORDER BY p.created_at, p.id LIMIT %s OFFSET %s', (subject, limit + 1, offset))
    can_create = False
    if subject in settings.projects_create_subjects and settings.projects_write_database_url.get_secret_value().strip():
        can_create = bool(_read("SELECT id FROM ee.app_users WHERE external_subject=%s AND state='active'", (subject,)))
    return ProjectListResponse(
        items=[project_read(row) for row in rows[:limit]],
        limit=limit, offset=offset, has_more=len(rows) > limit,
        capabilities=ProjectCapabilities(can_create=can_create),
    )


def get_project(subject: str, project_id: UUID) -> ProjectRead:
    rows = _read(_SELECT + ' AND p.id = %s', (subject, project_id))
    if not rows:
        # Missing and unauthorized IDs are indistinguishable: no project existence leak.
        raise ProjectReadError(404, 'project_not_found', '项目不存在或无权访问')
    return project_read(rows[0])


