"""Dependency-protected logical deletion; project-serialized and audit-idempotent."""
import hashlib
import json
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.core.database import project_connection, ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole
from app.schemas.deletion import DeleteRequest, DeleteResult, DeleteTarget, DeleteBlocker, DeletionImpact
from app.services.business_access import authorize, capabilities
from app.services.manual_work import conflict, lock_request, write_transaction
from app.services.projects import _SELECT, ProjectReadError
from app.services.resource_locks import lock_project, lock_function

SUPPORTED = ("PRD", "UC", "FR", "FR_ALLOCATION")
TABLES = {"project": "ee.projects", "function": "ee.business_functions", "artifact": "ee.artifacts"}
BLOCKER_LIMIT = 20


def _target(db, project_id, resource, resource_id, *, lock=False, include_archived=False):
    table = TABLES[resource]  # service constant, never caller SQL
    clause = "id=%s" if resource == "project" else "project_id=%s AND id=%s"
    params = (project_id,) if resource == "project" else (project_id,resource_id)
    if not include_archived:
        clause += " AND archived_at IS NULL"
    row = db.execute("SELECT *,lock_version::text AS version FROM " + table + " WHERE " + clause + (" FOR NO KEY UPDATE" if lock else ""), params).fetchone()
    if not row or (resource == "artifact" and row["type_code"] not in SUPPORTED):
        raise ProjectReadError(404, "delete_target_not_found", "对象不存在或无权访问")
    kind = row["type_code"] if resource == "artifact" else resource
    function_id = row["function_id"] if resource == "artifact" else row["id"] if resource == "function" else None
    target = DeleteTarget(id=row["id"],project_id=project_id,function_id=function_id,kind=kind,
        code=row["code"],name=row.get("name") or row["code"],lock_version=row["version"],
        head_revision_id=row.get("head_revision_id"),accepted_revision_id=row.get("accepted_revision_id"))
    return row,target


def _blockers(db, target, excluded_ids=()):
    args = (target.project_id,target.id)
    kind = target.kind
    if kind == "project":
        body = """SELECT id,'function'::text AS kind,code,false AS head,false AS accepted,true AS identity
                  FROM ee.business_functions WHERE project_id=%s AND archived_at IS NULL
                  UNION ALL SELECT id,type_code,code,false,false,true FROM ee.artifacts
                  WHERE project_id=%s AND archived_at IS NULL"""
        args = (target.project_id,target.project_id)
    elif kind == "function":
        body = """SELECT id,type_code AS kind,code,false AS head,false AS accepted,true AS identity
                  FROM ee.artifacts WHERE project_id=%s AND function_id=%s AND archived_at IS NULL"""
    elif kind == "PRD":
        body = """SELECT a.id,a.type_code AS kind,a.code,false AS head,false AS accepted,true AS identity
                  FROM ee.artifacts a JOIN ee.use_cases u ON u.artifact_id=a.id
                  WHERE a.project_id=%s AND u.prd_id=%s AND a.archived_at IS NULL"""
    elif kind == "UC":
        body = """SELECT a.id,a.type_code AS kind,a.code,
                    EXISTS(SELECT 1 FROM ee.fr_uc_sources s WHERE s.fr_revision_id=a.head_revision_id AND s.uc_id=%s) AS head,
                    EXISTS(SELECT 1 FROM ee.fr_uc_sources s WHERE s.fr_revision_id=a.accepted_revision_id AND s.uc_id=%s) AS accepted,
                    false AS identity
                  FROM ee.artifacts a WHERE a.project_id=%s AND a.type_code='FR' AND a.archived_at IS NULL
                    AND EXISTS(SELECT 1 FROM ee.fr_uc_sources s WHERE s.uc_id=%s
                    AND s.fr_revision_id IN (a.head_revision_id,a.accepted_revision_id))"""
        args = (target.id,target.id,target.project_id,target.id)
    elif kind == "FR":
        body = """SELECT a.id,a.type_code AS kind,a.code,false AS head,false AS accepted,true AS identity
                  FROM ee.artifacts a JOIN ee.fr_allocations f ON f.artifact_id=a.id
                  WHERE a.project_id=%s AND f.fr_id=%s AND a.archived_at IS NULL"""
    else:
        return 0,[]
    # Filter the entire dependency set BEFORE the presentation limit. In-batch
    # dependencies are explicit targets; every out-of-batch reference must block.
    exclusion = " WHERE NOT (deps.id = ANY(%s::uuid[]))" if excluded_ids else ""
    params = (*args, list(excluded_ids), BLOCKER_LIMIT) if excluded_ids else (*args, BLOCKER_LIMIT)
    rows = db.execute("SELECT deps.*,count(*) OVER () AS total FROM (" + body + ") deps" + exclusion + " ORDER BY kind,code,id LIMIT %s", params).fetchall()
    reason = {"project":"项目仍有未删除的功能或业务对象", "function":"功能仍有未删除的业务对象",
        "PRD":"仍有UC属于此PRD", "UC":"FR的当前工作版或当前确认版仍引用此UC", "FR":"仍有分配对象引用此FR（包括空分配）"}[kind]
    blockers = [DeleteBlocker(id=r["id"],kind=r["kind"],code=r["code"],reason=reason,
        reference_versions=[name for name,yes in (("head",r["head"]),("accepted",r["accepted"]),("stable_identity",r["identity"])) if yes]) for r in rows]
    return (rows[0]["total"] if rows else 0),blockers



def _impact(db, target, allowed, excluded_ids=()):
    count,blockers = _blockers(db,target,excluded_ids)
    fr_id,targets = None,[]
    if target.kind == "FR_ALLOCATION":
        fr_id = db.execute("SELECT fr_id FROM ee.fr_allocations WHERE artifact_id=%s", (target.id,)).fetchone()["fr_id"]
        rows = db.execute("""SELECT t.subsystem_id,s.code AS subsystem_code,i.name AS subsystem_name,
            bool_or(t.allocation_revision_id=%s) AS head,bool_or(t.allocation_revision_id=%s) AS accepted
            FROM ee.allocation_targets t JOIN ee.subsystems s ON s.id=t.subsystem_id
            JOIN ee.subsystem_version_items i ON i.subsystem_id=t.subsystem_id AND i.design_version_id=t.design_version_id
            WHERE t.allocation_revision_id IN (%s,%s)
            GROUP BY t.subsystem_id,s.code,i.name ORDER BY s.code,i.name""",
            (target.head_revision_id,target.accepted_revision_id,target.head_revision_id,target.accepted_revision_id)).fetchall()
        targets=[{**{k:r[k] for k in ("subsystem_id","subsystem_code","subsystem_name")},
            "reference_versions":[k for k in ("head","accepted") if r[k]]} for r in rows]
    return DeletionImpact(target=target,can_delete=target.kind+".archive" in allowed,
        blocked=count>0,blocker_count=count,blockers=blockers,blockers_truncated=count>len(blockers),
        allocation_fr_id=fr_id,allocation_targets=targets)

def get_impact(subject: str, project_id: UUID, resource: str, resource_id: UUID):
    try:
        with project_connection(repeatable_read=True) as db:
            # A coherent read snapshot; the DELETE still rechecks under write locks.
            if not db.execute(_SELECT + " AND p.id=%s", (subject,project_id)).fetchone():
                raise ProjectReadError(404,"project_not_found","项目不存在或无权访问")
            row,target = _target(db,project_id,resource,resource_id)
            if target.function_id and not db.execute("SELECT id FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL", (target.function_id,project_id)).fetchone():
                raise ProjectReadError(404,"function_not_found","功能不存在或已删除")
            return _impact(db,target,capabilities(db,subject,project_id))
    except (ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole) as error:
        raise ProjectReadError(503,"projects_not_configured","删除检查需要已配置的低权限数据库连接") from error
    except (psycopg.errors.QueryCanceled,psycopg.errors.LockNotAvailable) as error:
        raise ProjectReadError(504,"projects_query_timeout","删除检查超时，请重试") from error
    except psycopg.Error as error:
        raise ProjectReadError(503,"projects_database_unavailable","删除检查暂不可用") from error


def delete_one(subject: str, project_id: UUID, resource: str, resource_id: UUID, payload: DeleteRequest, request_id: str):
    normalized=payload.model_dump(mode="json")
    digest=hashlib.sha256(json.dumps([str(project_id),resource,str(resource_id),normalized],sort_keys=True).encode()).hexdigest()
    with write_transaction() as db:
        authorize(db,subject,project_id,"project.read",deletion_replay=True)
        project=lock_project(db,project_id,include_archived=True)
        row,target=_target(db,project_id,resource,resource_id,include_archived=True)
        parent=None
        if target.function_id:
            parent=lock_function(db,project_id,target.function_id,include_archived=True)
        row,target=_target(db,project_id,resource,resource_id,lock=True,include_archived=True)
        actor=authorize(db,subject,project_id,target.kind+".archive",function_id=target.function_id,deletion_replay=True)
        lock_request(db,payload.mutation_id)
        prior=db.execute("SELECT project_id,actor_id,event_type,function_id,before_state,after_state FROM ee.security_audit_events WHERE id=%s",(payload.mutation_id,)).fetchone()
        event=target.kind.lower()+".delete"
        if prior:
            if (prior["project_id"]!=project_id or prior["actor_id"]!=actor or prior["function_id"]!=target.function_id
                    or prior["event_type"]!=event or (prior["before_state"] or {}).get("request_hash")!=digest):
                conflict("操作标识已用于其他请求，请核对后重试")
            return DeleteResult.model_validate(prior["after_state"]["result"])
        if project["archived_at"] or (parent and parent["archived_at"]) or row["archived_at"]:
            raise ProjectReadError(404,"delete_target_not_found","对象不存在或已删除")
        if target.lock_version!=payload.base_lock_version:
            raise ProjectReadError(409,"deletion_version_conflict","对象已有变化，请重新检查删除影响")
        if int(target.lock_version)==9223372036854775807:
            conflict("对象版本已达上限，请联系维护人员")
        count,blockers=_blockers(db,target)
        if count:
            raise ProjectReadError(409,"deletion_blocked",f"不能删除：仍有{count}个未删除的依赖或业务对象，请先处理后重新检查")
        table=TABLES[resource]
        version="" if resource=="artifact" else ",lock_version=lock_version+1"
        updated=db.execute("UPDATE "+table+" SET archived_at=clock_timestamp()"+version+" WHERE id=%s RETURNING archived_at,lock_version::text AS lock_version",(target.id,)).fetchone()
        result=DeleteResult(mutation_id=payload.mutation_id,project_id=project_id,function_id=target.function_id,
            id=target.id,kind=target.kind,**updated)
        db.execute("""INSERT INTO ee.security_audit_events
            (id,project_id,actor_id,event_type,scope_kind,function_id,artifact_id,before_state,after_state,request_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (payload.mutation_id,project_id,actor,event,resource,target.function_id,target.id if resource=="artifact" else None,
             Jsonb({"request_hash":digest,"request":normalized,"resource":target.model_dump(mode="json")}),
             Jsonb({"result":result.model_dump(mode="json")}),request_id))
        return result

