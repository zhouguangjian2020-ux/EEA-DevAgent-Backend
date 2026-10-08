"""Atomic, versioned metadata updates using the existing rw login and audit table."""
import hashlib
import json

from psycopg.types.json import Jsonb

from app.schemas.metadata_edit import FunctionRead, MetadataPatch
from app.schemas.projects import ProjectRead
from app.services.business_access import authorize, capabilities
from app.services.manual_work import conflict, lock_request, write_transaction
from app.services.projects import ProjectReadError
from app.services.function_names import normalize_name, require_available_name

_COLUMNS = "id,code,name,description,lock_version::text AS lock_version,created_at,updated_at"


def _update(subject, project_id, function_id, payload: MetadataPatch, request_id):
    kind = "function" if function_id else "project"
    model = FunctionRead if function_id else ProjectRead
    normalized = payload.model_dump(mode="json", exclude_unset=True)
    digest = hashlib.sha256(json.dumps([str(project_id), str(function_id), normalized],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with write_transaction() as db:
        actor = authorize(db, subject, project_id, kind + ".update", function_id=function_id)
        # Parent before child. Deleted ancestors cannot be edited or replayed.
        project = db.execute("SELECT " + _COLUMNS + " FROM ee.projects WHERE id=%s AND archived_at IS NULL FOR NO KEY UPDATE", (project_id,)).fetchone()
        if not project:
            raise ProjectReadError(404, "project_not_found", "项目不存在或无权访问")
        old = project
        if function_id:
            old = db.execute("SELECT project_id," + _COLUMNS + " FROM ee.business_functions WHERE project_id=%s AND id=%s AND archived_at IS NULL FOR NO KEY UPDATE", (project_id, function_id)).fetchone()
            if not old:
                raise ProjectReadError(404, "function_not_found", "功能不存在或无权访问")
        authorize(db, subject, project_id, kind + ".update", function_id=function_id)
        lock_request(db, payload.mutation_id)
        prior = db.execute("SELECT project_id,actor_id,event_type,function_id,before_state,after_state FROM ee.security_audit_events WHERE id=%s", (payload.mutation_id,)).fetchone()
        if prior:
            if (prior["project_id"] != project_id or prior["actor_id"] != actor
                    or prior["function_id"] != function_id or prior["event_type"] != kind + ".update"
                    or (prior["before_state"] or {}).get("request_hash") != digest):
                conflict("操作标识已用于其他请求，请刷新核对")
            return model.model_validate(prior["after_state"]["result"])
        if old["lock_version"] != payload.base_lock_version:
            conflict("资料已被修改。原输入已保留，请先复制需要的内容，再重新读取最新资料")
        if int(old["lock_version"]) == 9223372036854775807:
            conflict("资料版本已达上限，请联系维护人员")
        name = payload.name if "name" in payload.model_fields_set else old["name"]
        description = payload.description if "description" in payload.model_fields_set else old["description"]
        # Description-only edits and cosmetic case/space changes do not repair or
        # merge legacy duplicate names. New names must pass the shared guard.
        if function_id and normalize_name(name) != normalize_name(old["name"]):
            require_available_name(db, project_id, name, exclude_id=function_id)
        # Table names below are constants, never caller input. Only these three columns are writable.
        table = "ee.business_functions" if function_id else "ee.projects"
        columns = "project_id," + _COLUMNS if function_id else _COLUMNS
        row = db.execute("UPDATE " + table + " SET name=%s,description=%s,lock_version=lock_version+1 WHERE id=%s RETURNING " + columns,
            (name, description, function_id or project_id)).fetchone()
        result = model.model_validate({**row, "can_update": True, "can_delete": kind+".archive" in capabilities(db,subject,project_id)})
        before = model.model_validate({**old, "can_update": True}).model_dump(mode="json")
        db.execute("""INSERT INTO ee.security_audit_events
            (id,project_id,actor_id,event_type,scope_kind,function_id,before_state,after_state,request_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (payload.mutation_id, project_id, actor, kind + ".update", kind, function_id,
             Jsonb({"request_hash": digest, "request": normalized, "resource": before}),
             Jsonb({"result": result.model_dump(mode="json")}), request_id))
        return result


def update_project(subject, project_id, payload, request_id):
    return _update(subject, project_id, None, payload, request_id)


def update_function(subject, project_id, function_id, payload, request_id):
    return _update(subject, project_id, function_id, payload, request_id)

