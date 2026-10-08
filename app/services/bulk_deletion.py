"""Atomic explicit-selection deletion; shares the project lock with all writers."""
import hashlib
import json

import psycopg
from psycopg.types.json import Jsonb

from app.core.database import project_connection, ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole
from app.schemas.deletion import BulkDeleteRequest, BulkDeleteResult, BulkDeleteSelection, BulkDeletionImpact, DeleteResult
from app.services.business_access import authorize, capabilities
from app.services.deletion import _target, _blockers, _impact, TABLES
from app.services.manual_work import conflict, lock_request, write_transaction
from app.services.projects import _SELECT, ProjectReadError
from app.services.resource_locks import lock_project, lock_function

ORDER = {"FR_ALLOCATION": 0, "FR": 1, "UC": 2, "PRD": 3, "function": 4}


def resource(item):
    return "function" if item.kind == "function" else "artifact"


def matching_target(db, project_id, item, **kwargs):
    row, target = _target(db, project_id, resource(item), item.id, **kwargs)
    if target.kind != item.kind:
        raise ProjectReadError(404, "delete_target_not_found", "对象不存在、类型不匹配或无权访问")
    return row, target


def get_impact(subject, project_id, payload: BulkDeleteSelection, request_id):
    try:
        with project_connection(repeatable_read=True) as db:
            if not db.execute(_SELECT + " AND p.id=%s", (subject, project_id)).fetchone():
                raise ProjectReadError(404, "project_not_found", "项目不存在或无权访问")
            allowed = capabilities(db, subject, project_id)
            ids = [item.id for item in payload.items]
            impacts = []
            for item in payload.items:
                _, target = matching_target(db, project_id, item)
                if not db.execute("SELECT id FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL", (target.function_id, project_id)).fetchone():
                    raise ProjectReadError(404, "function_not_found", "所属功能不存在或已删除")
                impacts.append(_impact(db, target, allowed, ids))
            return BulkDeletionImpact(items=impacts, total=len(impacts), can_delete=all(i.can_delete for i in impacts), blocked=any(i.blocked for i in impacts))
    except (ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole) as error:
        raise ProjectReadError(503, "projects_not_configured", "批量删除检查需要已配置的数据库连接") from error
    except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as error:
        raise ProjectReadError(504, "projects_query_timeout", "批量删除检查超时，请重试") from error
    except psycopg.Error as error:
        raise ProjectReadError(503, "projects_database_unavailable", "批量删除检查暂不可用") from error


def delete_batch(subject, project_id, payload: BulkDeleteRequest, request_id):
    # Selection order has no business meaning. Canonicalization permits safe retries.
    items = sorted(payload.items, key=lambda i: (ORDER[i.kind], str(i.id)))
    normalized = {"mutation_id": str(payload.mutation_id), "items": [i.model_dump(mode="json") for i in items]}
    digest = hashlib.sha256(json.dumps([str(project_id), normalized], sort_keys=True).encode()).hexdigest()
    with write_transaction() as db:
        authorize(db, subject, project_id, "project.read", deletion_replay=True)
        project = lock_project(db, project_id, include_archived=True)
        actor = None
        for kind in sorted({i.kind for i in items}):
            actor = authorize(db, subject, project_id, kind + ".archive", deletion_replay=True)
        lock_request(db, payload.mutation_id)
        prior = db.execute("SELECT project_id,actor_id,event_type,before_state,after_state FROM ee.security_audit_events WHERE id=%s", (payload.mutation_id,)).fetchone()
        if prior:
            if (prior["project_id"] != project_id or prior["actor_id"] != actor or prior["event_type"] != "batch.delete"
                    or (prior["before_state"] or {}).get("request_hash") != digest):
                conflict("操作标识已用于其他请求，请核对后重试")
            return BulkDeleteResult.model_validate(prior["after_state"]["result"])
        if project["archived_at"]:
            raise ProjectReadError(404, "project_not_found", "项目不存在或已删除")
        targets = [matching_target(db, project_id, item)[1] for item in items]
        for function_id in sorted({t.function_id for t in targets}, key=str):
            lock_function(db, project_id, function_id)
        ids = [i.id for i in items]
        locked = []
        # Validate every object, version, permission and external dependency before
        # any UPDATE. The project lock prevents new references from racing this.
        for item in items:
            _, target = matching_target(db, project_id, item, lock=True)
            authorize(db, subject, project_id, target.kind + ".archive", function_id=target.function_id)
            if target.lock_version != item.base_lock_version:
                raise ProjectReadError(409, "deletion_version_conflict", "批次中有对象已变化，本批次未删除；请重新检查")
            if int(target.lock_version) == 9223372036854775807:
                conflict("对象版本已达上限，本批次未删除")
            count, _ = _blockers(db, target, ids)
            if count:
                raise ProjectReadError(409, "deletion_blocked", "存在未选中的活动依赖，本批次未删除；请先处理依赖")
            locked.append(target)
        results = []
        for target in locked:
            table = TABLES["function" if target.kind == "function" else "artifact"]
            version = ",lock_version=lock_version+1" if target.kind == "function" else ""
            updated = db.execute("UPDATE " + table + " SET archived_at=clock_timestamp()" + version + " WHERE id=%s RETURNING archived_at,lock_version::text AS lock_version", (target.id,)).fetchone()
            results.append(DeleteResult(mutation_id=payload.mutation_id, project_id=project_id, function_id=target.function_id, id=target.id, kind=target.kind, **updated))
        result = BulkDeleteResult(mutation_id=payload.mutation_id, project_id=project_id, items=results)
        # One batch audit holds every before/after object; the audit and ALL updates
        # share one transaction. Audit failure rolls the entire batch back.
        db.execute("""INSERT INTO ee.security_audit_events
            (id,project_id,actor_id,event_type,scope_kind,before_state,after_state,request_id)
            VALUES(%s,%s,%s,'batch.delete','project',%s,%s,%s)""",
            (payload.mutation_id, project_id, actor,
             Jsonb({"request_hash": digest, "request": normalized, "resources": [t.model_dump(mode="json") for t in locked]}),
             Jsonb({"result": result.model_dump(mode="json")}), request_id))
        return result

