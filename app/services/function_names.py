"""API mutation name guard. Caller must hold the common project mutation lock."""
from app.services.projects import ProjectReadError


def normalize_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def require_available_name(db, project_id, name, *, exclude_id=None):
    key = normalize_name(name)
    rows = db.execute("SELECT id,name FROM ee.business_functions WHERE project_id=%s AND archived_at IS NULL", (project_id,))
    for row in rows:
        if row["id"] != exclude_id and normalize_name(row["name"]) == key:
            raise ProjectReadError(409, "function_name_conflict", "当前项目已有同名功能，请选择复用或修改名称后再新建")
