"""Manual work writes against R2. No new tables, no in-place body updates."""
from contextlib import contextmanager
import hashlib
import json
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.core.config import settings
from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole
from app.core.project_write_database import project_write_connection
from app.schemas.manual_work import (
    AcceptRevision, FrContent, FunctionCreate, PrdContent, PrdCreate,
    SaveRevision, UcContent, WriteResult,
)
from app.services.business_access import authorize
from app.services.projects import ProjectReadError
from app.services.resource_locks import lock_project, lock_function
from app.services.function_names import require_available_name


def conflict(message: str):
    raise ProjectReadError(409, "business_write_conflict", message)


@contextmanager
def write_transaction():
    if not settings.business_writes_enabled:
        raise ProjectReadError(503, "business_writes_disabled", "业务写入尚未启用，请联系维护人员")
    try:
        with project_write_connection() as db:
            yield db
    except (ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole) as error:
        raise ProjectReadError(503, "business_write_not_configured", "业务写入需要已配置的低权限账号") from error
    except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as error:
        raise ProjectReadError(504, "business_write_timeout", "操作超时，请保留原请求重试") from error
    except (psycopg.errors.UniqueViolation, psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation) as error:
        raise ProjectReadError(409, "business_write_conflict", "数据或版本已变化，请刷新核对后重试；原输入已保留") from error
    except psycopg.Error as error:
        # No DSNs, SQL or raw database diagnostics cross the API boundary.
        raise ProjectReadError(503, "business_write_unavailable", "业务写入暂不可用，请检查部署授权后重试") from error


def fingerprint(project_id, function_id, payload):
    data = [str(project_id), str(function_id), payload.model_dump(mode="json")]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def lock_request(db, key):
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(key),))


def feature(db, project_id, function_id):
    return lock_function(db, project_id, function_id)


def audit(db, project_id, actor, event, request_id, function_id, artifact_id=None):
    db.execute("""INSERT INTO ee.security_audit_events
        (id,project_id,actor_id,event_type,scope_kind,function_id,artifact_id,request_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
        (uuid4(), project_id, actor, event, "artifact" if artifact_id else "function", function_id, artifact_id, request_id))


def create_function(subject, project_id: UUID, payload: FunctionCreate, request_id):
    with write_transaction() as db:
        return create_function_in_db(db, subject, project_id, payload, request_id)


def create_function_in_db(db, subject, project_id, payload, request_id):
    actor = authorize(db, subject, project_id, "function.create")
    lock_project(db, project_id)
    actor = authorize(db, subject, project_id, "function.create")
    lock_request(db, payload.creation_id)
    digest = fingerprint(project_id, None, payload)
    old = db.execute("SELECT * FROM ee.business_functions WHERE id=%s", (payload.creation_id,)).fetchone()
    if old:
        if old["project_id"] != project_id or old["created_by"] != actor or old["archived_at"] or old["metadata"].get("manual_creation_hash") != digest:
            conflict("创建标识已使用，请核对功能列表")
        return WriteResult(function_id=old["id"], created=False)
    require_available_name(db, project_id, payload.name)
    # The UUID is stable across retries. A short-code collision retries only the code.
    for attempt in range(5):
        code = "FN-" + (payload.creation_id if attempt == 0 else uuid4()).hex[:12].upper()
        try:
            with db.transaction():
                db.execute("""INSERT INTO ee.business_functions(id,project_id,code,name,description,created_by,metadata)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                    (payload.creation_id, project_id, code, payload.name, payload.description or None, actor, Jsonb({"manual_creation_hash": digest})))
            break
        except psycopg.errors.UniqueViolation:
            if attempt == 4:
                raise
    audit(db, project_id, actor, "function.create", request_id, payload.creation_id)
    return WriteResult(function_id=payload.creation_id)


def current_source(db, project_id, function_id, kind, revision_id, artifact_id=None):
    row = db.execute("""SELECT a.id,a.accepted_revision_id FROM ee.artifacts a
        WHERE a.project_id=%s AND a.function_id=%s AND a.type_code=%s
          AND a.archived_at IS NULL AND a.accepted_revision_id=%s
        FOR SHARE""", (project_id, function_id, kind, revision_id)).fetchone()
    if not row or (artifact_id is not None and row["id"] != artifact_id):
        conflict("直接上游不是当前功能的已确认版本，请刷新并重新选择来源")
    return row["id"]


def type_version(db, kind):
    row = db.execute("""SELECT v.id FROM ee.artifact_type_versions v
        JOIN ee.artifact_types t ON t.code=v.type_code AND t.enabled
        WHERE v.type_code=%s AND v.retired_at IS NULL ORDER BY v.version_no DESC LIMIT 1""", (kind,)).fetchone()
    if not row:
        raise ProjectReadError(503, "artifact_type_unavailable", "对象类型定义尚未就绪")
    return row["id"]


def live_reference(db, project_id, function_id, kind, revision_id, artifact_id=None):
    row = db.execute("""SELECT a.id FROM ee.artifacts a JOIN ee.artifact_revisions r ON r.artifact_id=a.id
        WHERE a.project_id=%s AND a.function_id=%s AND a.type_code=%s AND a.archived_at IS NULL AND r.id=%s""",
        (project_id,function_id,kind,revision_id)).fetchone()
    if not row or (artifact_id is not None and row["id"] != artifact_id):
        conflict("直接上游已删除或不属于当前功能，不能继续引用")


def validate_sources(db, project_id, function_id, content, prd_id=None, base_revision_id=None):
    # Carrying the exact base reference is not selecting a new upstream. A stale
    # reference may remain in a draft; R2 D07 blocks acceptance until corrected.
    if isinstance(content, UcContent):
        live_reference(db, project_id, function_id, "PRD", content.prd_revision_id, prd_id)
        if base_revision_id and db.execute(
            "SELECT 1 FROM ee.uc_versions WHERE revision_id=%s AND prd_revision_id=%s",
            (base_revision_id, content.prd_revision_id),
        ).fetchone():
            return prd_id
        return current_source(db, project_id, function_id, "PRD", content.prd_revision_id, prd_id)
    if isinstance(content, FrContent):
        for source in sorted(content.sources, key=lambda s: str(s.uc_id)):
            live_reference(db, project_id, function_id, "UC", source.uc_revision_id, source.uc_id)
            if base_revision_id and db.execute(
                "SELECT 1 FROM ee.fr_uc_sources WHERE fr_revision_id=%s AND uc_id=%s AND uc_revision_id=%s",
                (base_revision_id, source.uc_id, source.uc_revision_id),
            ).fetchone():
                continue
            current_source(db, project_id, function_id, "UC", source.uc_revision_id, source.uc_id)
    return None


def append_revision(db, project_id, function_id, artifact_id, revision_id, actor, content, version_id, base_id, number, digest):
    metadata = {"manual_mutation_hash": digest}
    if isinstance(content, FrContent):
        metadata["acceptance_criteria"] = content.acceptance_criteria
    db.execute("""INSERT INTO ee.artifact_revisions
        (id,project_id,function_id,artifact_id,type_code,revision_no,base_revision_id,type_version_id,created_by,metadata)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (revision_id, project_id, function_id, artifact_id, content.kind, number, base_id, version_id, actor, Jsonb(metadata)))
    common = (revision_id, project_id, function_id, artifact_id)
    if isinstance(content, PrdContent):
        db.execute("INSERT INTO ee.prd_versions(revision_id,project_id,function_id,prd_id,markdown_body) VALUES(%s,%s,%s,%s,%s)", (*common, content.markdown_body))
    elif isinstance(content, UcContent):
        prd = db.execute("SELECT prd_id FROM ee.use_cases WHERE artifact_id=%s", (artifact_id,)).fetchone()["prd_id"]
        db.execute("""INSERT INTO ee.uc_versions(revision_id,project_id,function_id,uc_id,prd_revision_id,prd_id,
            name,description,actors_text,preconditions,main_flow,postconditions,extensions_text,hmi_logic,regulations,performance,nonfunctional)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (*common, content.prd_revision_id, prd, content.name, content.description, content.actors, content.preconditions,
             content.main_flow, content.postconditions, content.extensions, content.hmi, content.regulations, content.performance, content.nonfunctional))
        for kind, categories, bodies in (
            ("branch", ("a1", "a2", "a3", "a4"), content.branches),
            ("exception", ("vehicle_state", "mechanical", "environmental", "misuse", "power"), content.exceptions),
        ):
            for position, (category, body) in enumerate(zip(categories, bodies)):
                db.execute("""INSERT INTO ee.uc_scenario_items(project_id,function_id,uc_id,uc_revision_id,item_id,kind,category,position,body)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (project_id,function_id,artifact_id,revision_id,uuid4(),kind,category,position,body))
    else:
        db.execute("INSERT INTO ee.fr_versions(revision_id,project_id,function_id,fr_id,description) VALUES(%s,%s,%s,%s,%s)", (*common, content.description))
        for source in content.sources:
            db.execute("INSERT INTO ee.fr_uc_sources(project_id,function_id,fr_revision_id,uc_id,uc_revision_id) VALUES(%s,%s,%s,%s,%s)", (project_id,function_id,revision_id,source.uc_id,source.uc_revision_id))


def create_artifact(subject, project_id, function_id, payload, request_id, source_version=None):
    with write_transaction() as db:
        return create_artifact_in_db(db, subject, project_id, function_id, payload, request_id, source_version)

def create_artifact_in_db(db, subject, project_id, function_id, payload, request_id, source_version=None, *, initial_revision_id=None, creation_metadata=None, initial_prd_content=None):
    # Internal entry: metadata never comes from browser input.
    actor = authorize(db, subject, project_id, payload.kind + ".create", function_id=function_id)
    lock_project(db, project_id)
    actor = authorize(db, subject, project_id, payload.kind + ".create", function_id=function_id)
    feature(db, project_id, function_id)
    lock_request(db, payload.creation_id)
    digest = fingerprint(project_id, function_id, payload)
    old = db.execute("SELECT * FROM ee.artifacts WHERE id=%s", (payload.creation_id,)).fetchone()
    if old:
        if old["project_id"] != project_id or old["function_id"] != function_id or old["created_by"] != actor or old["archived_at"] or old["metadata"].get("manual_creation_hash") != digest:
            conflict("创建标识已使用，请核对对象列表")
        return WriteResult(function_id=function_id, artifact_id=old["id"], revision_id=old["head_revision_id"], created=False)
    if source_version is not None:
        from app.services.workflow_sources import context_in_db, digest as source_digest
        if payload.kind != 'FR' or source_digest(context_in_db(db,subject,project_id,function_id,'UC')) != source_version:
            conflict('生成来源已变化，请重新生成；候选未保存')
    if payload.kind == "PRD":
        lock_request(db, "prd/" + str(function_id))
        if db.execute("SELECT id FROM ee.artifacts WHERE project_id=%s AND function_id=%s AND type_code='PRD' AND archived_at IS NULL", (project_id,function_id)).fetchone():
            conflict("该功能已有 PRD，不能重复创建")
    if isinstance(payload, PrdCreate):
        content = initial_prd_content if initial_prd_content is not None else PrdContent(kind="PRD", markdown_body="# " + payload.name + "\n")
        if not isinstance(content, PrdContent):
            raise TypeError("Internal PRD content must be validated")
    else:
        body = payload.model_dump(exclude={"creation_id"})
        content = UcContent(**body) if payload.kind == "UC" else FrContent(**body)
    prd_id = validate_sources(db, project_id, function_id, content)
    version_id, revision_id = type_version(db, payload.kind), initial_revision_id or uuid4()
    for attempt in range(5):
        code = payload.kind + "-" + (payload.creation_id if attempt == 0 else uuid4()).hex[:12].upper()
        try:
            with db.transaction():
                # R2 FK to head is deferred, so initial identity can point to the revision created below.
                db.execute("""INSERT INTO ee.artifacts(id,project_id,function_id,type_code,code,head_revision_id,created_by,metadata)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""", (payload.creation_id,project_id,function_id,payload.kind,code,revision_id,actor,Jsonb({**(creation_metadata or {}), "manual_creation_hash":digest})))
            break
        except psycopg.errors.UniqueViolation:
            if attempt == 4:
                raise
    if payload.kind == "PRD":
        db.execute("INSERT INTO ee.prds(artifact_id,project_id,function_id) VALUES(%s,%s,%s)", (payload.creation_id,project_id,function_id))
    elif payload.kind == "UC":
        db.execute("INSERT INTO ee.use_cases(artifact_id,project_id,function_id,prd_id) VALUES(%s,%s,%s,%s)", (payload.creation_id,project_id,function_id,prd_id))
    else:
        db.execute("INSERT INTO ee.functional_requirements(artifact_id,project_id,function_id) VALUES(%s,%s,%s)", (payload.creation_id,project_id,function_id))
    append_revision(db,project_id,function_id,payload.creation_id,revision_id,actor,content,version_id,None,1,digest)
    audit(db,project_id,actor,payload.kind.lower()+".create",request_id,function_id,payload.creation_id)
    return WriteResult(function_id=function_id,artifact_id=payload.creation_id,revision_id=revision_id)

def artifact(db, project_id, artifact_id):
    identity = db.execute("SELECT function_id FROM ee.artifacts WHERE id=%s AND project_id=%s AND archived_at IS NULL", (artifact_id,project_id)).fetchone()
    if not identity:
        raise ProjectReadError(404,"artifact_not_found","对象不存在或已删除")
    feature(db,project_id,identity["function_id"])
    row = db.execute("SELECT * FROM ee.artifacts WHERE id=%s AND project_id=%s AND archived_at IS NULL FOR UPDATE", (artifact_id,project_id)).fetchone()
    if not row or row["type_code"] not in ("PRD", "UC", "FR"):
        raise ProjectReadError(404,"artifact_not_found","对象不存在或不支持此操作")
    return row


def save_revision(subject, project_id, artifact_id, payload: SaveRevision, request_id):
    with write_transaction() as db:
        lock_project(db,project_id)
        row = artifact(db,project_id,artifact_id)
        actor = authorize(db,subject,project_id,payload.content.kind+".update", function_id=row["function_id"])
        if row["type_code"] != payload.content.kind:
            conflict("对象类型不匹配")
        digest = fingerprint(project_id,row["function_id"],payload)
        old = db.execute("SELECT * FROM ee.artifact_revisions WHERE id=%s", (payload.mutation_id,)).fetchone()
        if old:
            if old["artifact_id"] != artifact_id or old["created_by"] != actor or old["metadata"].get("manual_mutation_hash") != digest:
                conflict("保存标识已被其他请求使用")
            return WriteResult(function_id=row["function_id"],artifact_id=artifact_id,revision_id=old["id"],created=False)
        if row["head_revision_id"] != payload.base_revision_id:
            conflict("对象已有新版本，不能覆盖；请保留草稿并刷新核对")
        prd = db.execute("SELECT prd_id FROM ee.use_cases WHERE artifact_id=%s", (artifact_id,)).fetchone() if payload.content.kind == "UC" else None
        validate_sources(db,project_id,row["function_id"],payload.content,prd["prd_id"] if prd else None,payload.base_revision_id)
        base = db.execute("SELECT revision_no,type_version_id FROM ee.artifact_revisions WHERE id=%s", (payload.base_revision_id,)).fetchone()
        append_revision(db,project_id,row["function_id"],artifact_id,payload.mutation_id,actor,payload.content,base["type_version_id"],payload.base_revision_id,base["revision_no"]+1,digest)
        db.execute("UPDATE ee.artifacts SET head_revision_id=%s WHERE id=%s", (payload.mutation_id,artifact_id))
        audit(db,project_id,actor,row["type_code"].lower()+".update",request_id,row["function_id"],artifact_id)
        return WriteResult(function_id=row["function_id"],artifact_id=artifact_id,revision_id=payload.mutation_id)


def accept_revision(subject, project_id, artifact_id, payload: AcceptRevision, request_id):
    with write_transaction() as db:
        # Require read access before looking up an object's type.
        authorize(db,subject,project_id,"project.read")
        lock_project(db,project_id)
        row = artifact(db,project_id,artifact_id)
        actor = authorize(db,subject,project_id,row["type_code"]+".accept", function_id=row["function_id"])
        old = db.execute("SELECT revision_id,actor_id FROM ee.review_events WHERE artifact_id=%s AND request_key=%s", (artifact_id,str(payload.mutation_id))).fetchone()
        if old:
            if old["revision_id"] != payload.revision_id or old["actor_id"] != actor:
                conflict("确认标识已被其他请求使用")
            return WriteResult(function_id=row["function_id"],artifact_id=artifact_id,revision_id=payload.revision_id,created=False)
        if row["head_revision_id"] != payload.revision_id:
            conflict("仅能确认当前工作版本，请刷新核对")
        # The narrow DB function runs existing R2 review/metadata/upstream guards atomically.
        db.execute("SELECT ee.accept_manual_revision(%s,%s,%s,%s,%s)", (project_id,artifact_id,payload.revision_id,actor,str(payload.mutation_id)))
        audit(db,project_id,actor,row["type_code"].lower()+".accept",request_id,row["function_id"],artifact_id)
        return WriteResult(function_id=row["function_id"],artifact_id=artifact_id,revision_id=payload.revision_id)

