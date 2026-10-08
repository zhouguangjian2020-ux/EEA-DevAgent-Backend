"""Project-scoped capabilities; a read grant never implies a write grant."""
from uuid import UUID

from app.core.config import settings
from app.services.projects import ProjectReadError, _SELECT


def capabilities(db, subject: str, project_id: UUID, function_id: UUID | None = None) -> set[str]:
    """Return project permissions plus the selected function's scoped permissions.

    Project bindings are inherited by every function. Function bindings are only
    considered when the caller supplies that function's ID, so an editor for one
    function cannot mutate another function through a shared endpoint.
    """
    if not settings.business_writes_enabled or not settings.projects_write_database_url.get_secret_value().strip():
        return set()
    if function_id is None:
        scope_clause = "b.scope_kind='project' AND 'project'=ANY(pd.allowed_scopes)"
        params = (project_id, subject)
    else:
        scope_clause = "((b.scope_kind='project' AND 'project'=ANY(pd.allowed_scopes)) OR " \
                       "(b.scope_kind='function' AND b.function_id=%s AND 'function'=ANY(pd.allowed_scopes)))"
        params = (project_id, subject, function_id)
    rows = db.execute(f"""
        SELECT DISTINCT pd.resource_kind || '.' || pd.action AS capability
        FROM ee.app_users u
        JOIN ee.project_members m ON m.user_id=u.id AND m.project_id=%s AND m.state='active'
        JOIN ee.access_bindings b ON b.user_id=u.id AND b.project_id=m.project_id
        JOIN ee.access_roles r ON r.id=b.role_id AND r.enabled
        JOIN ee.role_permissions rp ON rp.role_id=r.id
        JOIN ee.permission_definitions pd ON pd.code=rp.permission_code AND pd.enabled
        WHERE u.external_subject=%s AND u.state='active' AND {scope_clause}
    """, params).fetchall()
    return {row["capability"] for row in rows}


def authorize(
    db,
    subject: str,
    project_id: UUID,
    capability: str,
    *,
    function_id: UUID | None = None,
    deletion_replay=False,
) -> UUID:
    # Only deletion idempotency may authorize an archived project. Callers must not
    # perform a new mutation on it; all ordinary reads/writes retain the active filter.
    query = _SELECT.replace("p.archived_at IS NULL AND ", "") if deletion_replay else _SELECT
    if not db.execute(query + " AND p.id=%s", (subject, project_id)).fetchone():
        raise ProjectReadError(404, "project_not_found", "项目不存在或无权访问")
    if capability not in capabilities(db, subject, project_id, function_id):
        raise ProjectReadError(403, "business_write_forbidden", "当前账号没有执行此操作的项目或功能权限")
    return db.execute("SELECT id FROM ee.app_users WHERE external_subject=%s AND state='active'", (subject,)).fetchone()["id"]

