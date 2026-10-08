"""One project mutation lock shared by edit/create/review/delete operations."""
from app.services.projects import ProjectReadError


def lock_project(db, project_id, include_archived=False):
    row = db.execute("SELECT id,archived_at,lock_version::text AS lock_version FROM ee.projects WHERE id=%s FOR NO KEY UPDATE", (project_id,)).fetchone()
    if not row or (row["archived_at"] and not include_archived):
        raise ProjectReadError(404, "project_not_found", "项目不存在或无权访问")
    return row


def lock_function(db, project_id, function_id, include_archived=False):
    row = db.execute("SELECT id,archived_at FROM ee.business_functions WHERE project_id=%s AND id=%s FOR NO KEY UPDATE", (project_id,function_id)).fetchone()
    if not row or (row["archived_at"] and not include_archived):
        raise ProjectReadError(404, "function_not_found", "功能不存在或已删除")
    return row
