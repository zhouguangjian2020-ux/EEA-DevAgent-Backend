"""Durable PRD creation, with short transactions around (never across) Agent calls.

No job queue, implicit approval or browser-authored plan. Expired attempts can be
explicitly retried; their lease token fences late replies from writing objects.
"""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.core.config import settings
from app.schemas.manual_work import FunctionCreate, PrdContent, PrdCreate
from app.schemas.prd_creation import FeatureDefinition, FeaturePlanReview, PrdCreationIntent
from app.services import manual_work, prd_creation_plan as planning, prd_generation, workflow_agent
from app.services.business_access import authorize
from app.services.projects import ProjectReadError
from app.services.resource_locks import lock_project, lock_function
from app.services.workflow_sources import digest, fail, read_db, visible


SELECT_INTENT = "SELECT * FROM ee.prd_creation_intents WHERE id=%s AND project_id=%s AND user_id=%s"


def lease_seconds():
    return max(1, float(settings.agent_timeout_seconds)) + 60


def active(row):
    return row["state"] in ("planning", "generating") and row["lease_until"] > datetime.now(timezone.utc)


def owned(db, subject, project_id, intent_id, *, locking=False):
    actor = visible(db, subject, project_id)
    row = db.execute(SELECT_INTENT + (" FOR UPDATE" if locking else ""), (intent_id, project_id, actor)).fetchone()
    if not row:
        fail(404, "prd_creation_not_found", "创建记录不存在或无权查看")
    return row


def review(db, subject, row):
    definition = FeatureDefinition.model_validate(row["definition"])
    snapshot = planning.planning_snapshot_in_db(db, subject, row["project_id"], require_creation_access=False)
    candidates = planning.match_candidates(definition, snapshot.functions)
    return FeaturePlanReview(
        client_request_id=row["client_request_id"], project_id=row["project_id"], definition=definition,
        status="needs_selection" if candidates else "ready_to_create" if snapshot.can_create_new else "blocked",
        candidates=candidates[:planning.MAX_CANDIDATES], candidate_count=len(candidates),
        has_more=len(candidates) > planning.MAX_CANDIDATES,
        can_create_new_function=snapshot.can_create_new,
        exact_name_conflict=any(c.match == "exact_name" for c in candidates),
    )


def view(db, subject, row):
    state = row["state"]
    if state in ("planning", "generating") and not active(row):
        state = "interrupted"  # Read never reclaims a lease or re-invokes the Agent.
    if row["function_id"]:
        if not db.execute("SELECT 1 FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL",
                          (row["function_id"], row["project_id"])).fetchone():
            fail(404, "function_not_found", "创建记录对应功能已删除，不能继续或重新创建")
    if row["artifact_id"]:
        if not db.execute("SELECT 1 FROM ee.artifacts WHERE id=%s AND project_id=%s AND function_id=%s AND archived_at IS NULL",
                          (row["artifact_id"], row["project_id"], row["function_id"])).fetchone():
            fail(404, "artifact_not_found", "记录对应PRD已删除，不会自动重新创建")
    return PrdCreationIntent(
        id=row["id"], project_id=row["project_id"], client_request_id=row["client_request_id"],
        version=row["version"], status=state,
        next_phase="open" if state in ("saved", "blocked") else "generate" if row["function_id"] else "choose" if row["definition"] else "plan",
        review=review(db, subject, row) if row["definition"] and not row["function_id"] else None,
        function_id=row["function_id"], artifact_id=row["artifact_id"], revision_id=row["revision_id"],
        error_code="attempt_interrupted" if state == "interrupted" else row["error_code"],
        saved_as="pending" if state == "saved" else None,
    )


def read_intent(subject, project_id, intent_id):
    with read_db() as db:
        return view(db, subject, owned(db, subject, project_id, intent_id))


def find_intent(subject, project_id, client_request_id):
    """Recover a lost initial response using the caller's existing request key."""
    with read_db() as db:
        actor = visible(db, subject, project_id)
        row = db.execute("SELECT * FROM ee.prd_creation_intents WHERE project_id=%s AND user_id=%s AND client_request_id=%s",
                         (project_id, actor, client_request_id)).fetchone()
        if not row:
            fail(404, "prd_creation_not_found", "尚未找到本次请求的创建记录，请保留原请求标识")
        return view(db, subject, row)


def prepare_plan(subject, project_id, payload):
    with manual_work.write_transaction() as db:
        actor = visible(db, subject, project_id)
        lock_project(db, project_id)
        visible(db, subject, project_id)
        request_hash = digest(payload.model_dump(mode="json", exclude={"thread_id"}))
        prior = db.execute("SELECT * FROM ee.prd_creation_intents WHERE project_id=%s AND user_id=%s AND client_request_id=%s FOR UPDATE",
                           (project_id, actor, payload.client_request_id)).fetchone()
        if prior:
            if prior["request_hash"] != request_hash:
                fail(409, "prd_creation_input_conflict", "同一创建标识不能用于不同需求，请查回原记录")
            return prior, False
        planning.planning_snapshot_in_db(db, subject, project_id)
        row = db.execute("""INSERT INTO ee.prd_creation_intents
            (id,project_id,user_id,client_request_id,request_hash,prompt,state,
             reserved_function_id,reserved_artifact_id,reserved_revision_id,lease_token,lease_until)
            VALUES(%s,%s,%s,%s,%s,%s,'planning',%s,%s,%s,%s,now()+(%s * interval '1 second')) RETURNING *""",
            (uuid4(), project_id, actor, payload.client_request_id, request_hash, payload.message,
             uuid4(), uuid4(), uuid4(), uuid4(), lease_seconds())).fetchone()
        return row, True


def attempt_row(db, subject, original):
    lock_project(db, original["project_id"])
    current = owned(db, subject, original["project_id"], original["id"], locking=True)
    if current["lease_token"] != original["lease_token"] or current["state"] != original["state"]:
        fail(409, "prd_creation_attempt_replaced", "此请求已被较新的重试替代，旧结果未保存")
    return current


def finish_plan(subject, original, definition):
    with manual_work.write_transaction() as db:
        row = attempt_row(db, subject, original)
        planning.planning_snapshot_in_db(db, subject, row["project_id"])
        db.execute("""UPDATE ee.prd_creation_intents SET definition=%s,state='planned',version=version+1,
            lease_token=NULL,lease_until=NULL,error_code=NULL,updated_at=now() WHERE id=%s""",
            (Jsonb(definition.model_dump()), row["id"]))


def fail_attempt(original, code):
    # This only stores a safe machine code, even after permission revocation.
    # It cannot update a newer attempt or any business object.
    with manual_work.write_transaction() as db:
        lock_project(db, original["project_id"], include_archived=True)
        db.execute("""UPDATE ee.prd_creation_intents SET state='failed',version=version+1,
            error_code=%s,lease_token=NULL,lease_until=NULL,updated_at=now()
            WHERE id=%s AND project_id=%s AND user_id=%s AND lease_token=%s AND state=%s""",
            (code, original["id"], original["project_id"], original["user_id"], original["lease_token"], original["state"]))


async def record_failure(original, code):
    try:
        await asyncio.to_thread(fail_attempt, original, code)
    except ProjectReadError:
        # DB/writes unavailable: leave a truthful expiring in-progress receipt,
        # never turn an unrecorded error into a claimed success.
        pass


async def run_plan(subject, row, request_id):
    try:
        key = digest({"intent_id": str(row["id"]), "phase": "feature-plan-v1"})
        body = workflow_agent.request_body(row["prompt"], {"project_id": str(row["project_id"])}, key, planning.SCENE, "text")
        body["trace_id"] = request_id
        definition = planning.parse_definition(await workflow_agent.invoke_envelope(body))
        await asyncio.to_thread(finish_plan, subject, row, definition)
    except asyncio.CancelledError:
        await asyncio.shield(record_failure(row, "attempt_interrupted"))
        raise
    except ProjectReadError as error:
        await record_failure(row, error.code)
        raise
    return await asyncio.to_thread(read_intent, subject, row["project_id"], row["id"])


async def start_intent(subject, project_id, payload, request_id):
    from app.services.task_progress import TaskProgress
    progress = await TaskProgress.begin(subject, project_id, payload, 'PRD')
    await progress.advance('planning')
    try:
        row, claimed = await asyncio.to_thread(prepare_plan, subject, project_id, payload)
        result = (await run_plan(subject, row, request_id) if claimed
                  else await asyncio.to_thread(read_intent, subject, project_id, row['id']))
    except asyncio.CancelledError:
        await asyncio.shield(progress.failed(uncertain=True))
        raise
    except Exception:
        await progress.failed(uncertain=bool(progress.row and progress.row["stage"] == "saving"))
        raise
    await progress.prd_result(result)
    return result


def existing_prd(db, project_id, function_id):
    return db.execute("SELECT id,head_revision_id FROM ee.artifacts WHERE project_id=%s AND function_id=%s AND type_code='PRD' AND archived_at IS NULL",
                      (project_id, function_id)).fetchone()


def source_context(db, project_id, function_id):
    project = db.execute("SELECT name,description,lock_version FROM ee.projects WHERE id=%s AND archived_at IS NULL", (project_id,)).fetchone()
    function = db.execute("SELECT name,description,lock_version FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL",
                          (function_id, project_id)).fetchone()
    if not project or not function:
        fail(404, "function_not_found", "功能或所属项目已删除")
    if not (function["description"] or "").strip():
        fail(409, "function_definition_missing", "复用功能缺少描述，请先完善功能描述")
    context = prd_generation.PrdGenerationContext(project_id, project["name"], project["description"],
                                                 function_id, function["name"], function["description"])
    return context, digest({"project": project, "function": function})


def canonical_choice(payload, definition):
    if payload.action == "continue":
        return None
    if payload.action == "reuse":
        return {"action": "reuse", "function_id": str(payload.function_id)}
    return {"action": "create", "name": payload.name or definition["name"]}


def block_existing(db, row, function_id, prd, choice=None):
    db.execute("""UPDATE ee.prd_creation_intents SET state='blocked',version=version+1,function_id=%s,
        choice=coalesce(choice,%s),artifact_id=%s,revision_id=%s,error_code='prd_exists',lease_token=NULL,
        lease_until=NULL,updated_at=now() WHERE id=%s""",
        (function_id, Jsonb(choice) if choice else None, prd["id"], prd["head_revision_id"], row["id"]))


def prepare_generation(subject, project_id, intent_id, payload, request_id):
    with manual_work.write_transaction() as db:
        visible(db, subject, project_id)
        lock_project(db, project_id)
        row = owned(db, subject, project_id, intent_id, locking=True)
        if not row["definition"]:
            if payload.action != "continue":
                fail(409, "prd_plan_not_ready", "功能计划尚未完成，不能创建对象")
            if active(row) or payload.expected_version != row["version"]:
                return row, None
            planning.planning_snapshot_in_db(db, subject, project_id)
            row = db.execute("""UPDATE ee.prd_creation_intents SET state='planning',version=version+1,
                error_code=NULL,lease_token=%s,lease_until=now()+(%s * interval '1 second'),updated_at=now()
                WHERE id=%s RETURNING *""", (uuid4(), lease_seconds(), row["id"])).fetchone()
            return row, "plan"
        choice = canonical_choice(payload, row["definition"])
        if row["choice"] and choice is not None and choice != row["choice"]:
            fail(409, "prd_creation_choice_conflict", "此创建记录已绑定原功能，不能更换目标或名称")
        if row["state"] in ("saved", "blocked") or active(row):
            return row, None
        if payload.expected_version != row["version"]:
            return row, None  # lost response replay: only return current receipt
        function_id = row["function_id"]
        if function_id is None:
            if choice is None:
                fail(409, "prd_choice_required", "请明确选择新建或复用功能")
            snapshot = planning.planning_snapshot_in_db(db, subject, project_id)
            definition = FeatureDefinition.model_validate(row["definition"])
            if payload.action == "auto" and planning.match_candidates(definition, snapshot.functions):
                return row, None  # concurrent new candidates require explicit human selection
            if choice["action"] == "reuse":
                candidates = planning.match_candidates(definition, snapshot.functions)
                candidate = next((c for c in candidates if str(c.function_id) == choice["function_id"]), None)
                if not candidate:
                    fail(409, "prd_candidate_changed", "候选已变化或不属于当前项目，请重新核对，未创建对象")
                function_id = candidate.function_id
                authorize(db, subject, project_id, "PRD.create", function_id=function_id)
                lock_function(db, project_id, function_id)
                if candidate.prd_artifact_id:
                    block_existing(db, row, function_id, existing_prd(db, project_id, function_id), choice)
                    return row, None
            else:
                authorize(db, subject, project_id, "PRD.create")
                result = manual_work.create_function_in_db(db, subject, project_id,
                    FunctionCreate(creation_id=row["reserved_function_id"], name=choice["name"], description=definition.description), request_id)
                function_id = result.function_id
        authorize(db, subject, project_id, "PRD.create", function_id=function_id)
        lock_function(db, project_id, function_id)
        prd = existing_prd(db, project_id, function_id)
        if prd:
            block_existing(db, row, function_id, prd, choice)
            return row, None
        context, source_hash = source_context(db, project_id, function_id)
        row = db.execute("""UPDATE ee.prd_creation_intents SET state='generating',version=version+1,
            choice=coalesce(choice,%s),function_id=%s,source_hash=%s,error_code=NULL,lease_token=%s,
            lease_until=now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s RETURNING *""",
            (Jsonb(choice) if choice else None, function_id, source_hash, uuid4(), lease_seconds(), row["id"])).fetchone()
        return row, context


def finish_generation(subject, original, markdown, request_id):
    content = PrdContent(kind="PRD", markdown_body=markdown)
    with manual_work.write_transaction() as db:
        row = attempt_row(db, subject, original)
        authorize(db, subject, row["project_id"], "PRD.create", function_id=row["function_id"])
        lock_function(db, row["project_id"], row["function_id"])
        _, current_hash = source_context(db, row["project_id"], row["function_id"])
        if current_hash != row["source_hash"]:
            fail(409, "prd_source_changed", "生成功能资料已变化，旧结果未保存；可使用同一功能重新生成")
        prd = existing_prd(db, row["project_id"], row["function_id"])
        if prd:
            block_existing(db, row, row["function_id"], prd)
            return
        result = manual_work.create_artifact_in_db(db, subject, row["project_id"], row["function_id"],
            PrdCreate(kind="PRD", creation_id=row["reserved_artifact_id"], name=row["definition"]["name"]), request_id,
            initial_revision_id=row["reserved_revision_id"], initial_prd_content=content,
            creation_metadata={"prd_creation_intent_id": str(row["id"])})
        db.execute("""UPDATE ee.prd_creation_intents SET state='saved',version=version+1,artifact_id=%s,
            revision_id=%s,error_code=NULL,lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=%s""",
            (result.artifact_id, result.revision_id, row["id"]))


async def _execute_intent(subject, project_id, intent_id, payload, request_id, progress):
    row, work = await asyncio.to_thread(prepare_generation, subject, project_id, intent_id, payload, request_id)
    if work == "plan":
        await progress.advance("planning", retry=True)
        return await run_plan(subject, row, request_id)
    if work is None:
        return await asyncio.to_thread(read_intent, subject, project_id, intent_id)
    await progress.advance("generating", function_id=row["function_id"], retry=True)
    try:
        # Same intent+source uses a stable upstream idempotency key on explicit retry.
        # Local persistence safety does not depend on upstream honoring it.
        key = digest({"intent_id": str(row["id"]), "source_hash": row["source_hash"], "phase": "prd-v1"})
        output = None
        async for event in prd_generation.stream_prd_generation(work, version=None, request_id=request_id,
                                                               original_prompt=row["prompt"], idempotency_key=key, strict_completion=True):
            if event.get("type") == "done":
                if event.get("scene") != prd_generation.PRD_GENERATION_SCENE:
                    fail(502, "agent_invalid_response", "PRD结果场景不匹配，未保存")
                output = event.get("output")
        if not isinstance(output, str) or not output.strip() or "\x00" in output or len(output) > 200000:
            fail(502, "agent_invalid_response", "Agent未返回完整有效PRD，功能已保留")
        await progress.advance("saving")
        await asyncio.to_thread(finish_generation, subject, row, output.strip(), request_id)
    except asyncio.CancelledError:
        await asyncio.shield(record_failure(row, "attempt_interrupted"))
        raise
    except (ProjectReadError, prd_generation.PrdGenerationError) as error:
        await record_failure(row, error.code)
        if isinstance(error, prd_generation.PrdGenerationError):
            raise ProjectReadError(error.status, error.code, error.message) from error
        raise
    return await asyncio.to_thread(read_intent, subject, project_id, intent_id)


async def execute_intent(subject, project_id, intent_id, payload, request_id):
    from app.services.task_progress import TaskProgress
    prior = await asyncio.to_thread(read_intent, subject, project_id, intent_id)
    progress = await TaskProgress.original(subject, project_id, prior.client_request_id)
    # Never accept a current UI thread as a replacement for the initial one.
    if prior.status not in {'planning', 'generating', 'saved', 'blocked'}:
        await progress.advance('creating_function' if prior.next_phase == 'choose' else 'generating', retry=True)
    try:
        result = await _execute_intent(subject, project_id, intent_id, payload, request_id, progress)
    except asyncio.CancelledError:
        await asyncio.shield(progress.failed(uncertain=True))
        raise
    except Exception:
        await progress.failed(uncertain=bool(progress.row and progress.row["stage"] == "saving"))
        raise
    await progress.prd_result(result)
    return result
