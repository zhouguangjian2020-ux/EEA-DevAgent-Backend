"""Versioned default catalogue; installation never grants general catalogue writes."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from app.services.projects import ProjectReadError

SIGNATURE = "ee.initialize_default_subsystems_v1(uuid,uuid,text)"

@lru_cache(maxsize=1)
def default_catalogue():
    payload = json.loads((Path(__file__).resolve().parents[1] / "data/default_subsystems_v1.json").read_text(encoding="utf-8"))
    raw = json.dumps(payload["items"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(raw).hexdigest() != payload["items_sha256"] or len(payload["items"]) != 43:
        raise ProjectReadError(503, "project_catalogue_template_invalid", "默认子系统模板校验失败，请联系维护人员")
    return payload


def require_default_catalogue(db):
    default_catalogue()
    row = db.execute("SELECT has_function_privilege(current_user, to_regprocedure(%s), 'EXECUTE') AS ready", (SIGNATURE,)).fetchone()
    if not row or row["ready"] is not True:
        raise ProjectReadError(503, "project_catalogue_not_installed", "默认子系统目录初始化尚未安装；请先安装数据库增量后再创建项目")


def initialize_default_catalogue(db, project_id, actor_id, request_id):
    template = default_catalogue()
    row = db.execute("SELECT design_version_id,item_count,template_sha256 FROM ee.initialize_default_subsystems_v1(%s,%s,%s)",
                     (project_id, actor_id, request_id)).fetchone()
    if not row or not row["design_version_id"] or row["item_count"] != len(template["items"]) or row["template_sha256"] != template["items_sha256"]:
        raise ProjectReadError(503, "project_catalogue_version_mismatch", "默认子系统目录版本不一致，本次项目创建未提交")
    return {"template_version": template["template_version"], "template_sha256": template["items_sha256"],
            "count": row["item_count"], "design_version_id": str(row["design_version_id"])}


def prepare_allocation_catalogue(subject, project_id, function_id, request_id):
    """Initialize a legacy empty catalogue only on an explicit allocation command.

    Existing catalogues stay untouched. The installed, creator-only SQL function
    remains the authority for empty/history guards, locking, audit and idempotency.
    Never call from GET endpoints or extend the reader's database permissions.
    """
    import psycopg
    from app.services.workflow_sources import generation_context, context_in_db
    from app.services.manual_work import write_transaction
    from app.services.resource_locks import lock_project
    from app.services.business_access import authorize

    try:
        generation_context(subject, project_id, function_id, 'FR')
        return False
    except ProjectReadError as error:
        if error.code != 'subsystem_catalogue_empty':
            raise

    with write_transaction() as db:
        lock_project(db, project_id)
        actor = authorize(db, subject, project_id, 'FR_ALLOCATION.create', function_id=function_id)
        # Recheck permissions, confirmed sources and catalogue after taking the lock.
        # A simultaneous initializer may have completed while this request waited.
        try:
            context_in_db(db, subject, project_id, function_id, 'FR')
            return False
        except ProjectReadError as error:
            if error.code != 'subsystem_catalogue_empty':
                raise
        require_default_catalogue(db)
        try:
            initialize_default_catalogue(db, project_id, actor, request_id)
        except psycopg.errors.InsufficientPrivilege as error:
            raise ProjectReadError(403, 'project_catalogue_creator_required',
                '该项目尚无子系统目录，请由项目创建者首次发起FR分配以初始化；不会代用其他账号') from error
        except psycopg.errors.ObjectNotInPrerequisiteState as error:
            raise ProjectReadError(409, 'project_catalogue_existing_conflict',
                '项目已有或不完整的子系统目录，未自动覆盖；请联系维护人员核对目录版本') from error
        # Fail closed and roll back if the new catalogue is not usable in this scope.
        context_in_db(db, subject, project_id, function_id, 'FR')
    return True
