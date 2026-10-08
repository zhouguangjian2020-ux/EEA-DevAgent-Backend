"""Real disposable PostgreSQL S2 tests, with simulated Agent envelopes/PRD streams."""
import os
from uuid import uuid4

import pytest

from app.core.config import settings
from app.core.security import create_session_token
from psycopg.rows import dict_row
from app.services.projects import ProjectReadError
from app.services import prd_creation, prd_generation, workflow_agent
from tests.test_manual_work_postgres import db, client, HEADERS
from tests.test_prd_creation_plan_postgres import new_project, new_function, create_prd, peer


@pytest.fixture(autouse=True)
def intent_schema_required():
    if os.getenv("TEST_PRD_CREATION_ENABLED") != "1":
        pytest.skip("Disposable local S2 intent increment required")


@pytest.fixture
def agents(monkeypatch):
    calls = []
    state = {"plan": {"feature_name": "计划新功能"}, "output": "车辆上电开始循环录像。", "prd_body": "# 测试PRD\n\n待确认业务条款。", "fail": False}

    async def plan(body):
        calls.append(("plan", body))
        return {"scene": "FEATURE_DEFINITION", "plan": state["plan"], "output": state["output"]}

    async def stream(context, *, version, request_id, original_prompt=None, idempotency_key=None, strict_completion=False):
        calls.append(("prd", context, original_prompt, idempotency_key))
        if state["fail"]:
            raise prd_generation.PrdGenerationError(502, "agent_upstream_error", "本地模拟Agent失败")
        yield {"type": "done", "scene": "FROM_DEFINITION_CREATE_PRD", "output": state["prd_body"]}

    monkeypatch.setattr(workflow_agent, "invoke_envelope", plan)
    monkeypatch.setattr(prd_generation, "stream_prd_generation", stream)
    return state, calls


def start(client, project, request_id=None, message="原始产品需求", status=200):
    response = client.post(f"/v1/projects/{project}/prd-creations/intents", headers=HEADERS,
                           json={"client_request_id": str(request_id or uuid4()), "message": message})
    assert response.status_code == status, response.text
    return response.json()


def read(client, project, intent_id, status=200):
    response = client.get(f"/v1/projects/{project}/prd-creations/intents/{intent_id}")
    assert response.status_code == status, response.text
    return response.json()


def choose(client, project, intent, action="create", function_id=None, name=None, status=200):
    response = client.post(f"/v1/projects/{project}/prd-creations/intents/{intent['id']}/execute", headers=HEADERS,
                           json={"expected_version": intent["version"], "action": action,
                                 **({"function_id": function_id} if function_id else {}),
                                 **({"name": name} if name is not None else {})})
    assert response.status_code == status, response.text
    return response.json()


def counts(db, project):
    return [db.execute(f"SELECT count(*) FROM ee.{table} WHERE project_id=%s", (project,)).fetchone()[0]
            for table in ("business_functions", "artifacts", "prd_creation_intents")]


def test_new_intent_plan_choice_prd_pending_replay_and_manual_name_guard(client, db, agents):
    _, calls = agents
    project = new_project(client)
    intent_id = uuid4()
    intent = start(client, project, intent_id)
    assert intent["status"] == "planned" and intent["version"] == 2
    assert intent["review"]["status"] == "ready_to_create" and intent["function_id"] is None
    assert counts(db, project) == [0, 0, 1]
    assert start(client, project, intent_id) == intent
    assert len(calls) == 1 and calls[0][1]["response_format"] == "text"
    assert start(client, project, intent_id, message="不同文本", status=409)["detail"]["code"] == "prd_creation_input_conflict"
    invalid = choose(client, project, intent, action="reuse", function_id=str(uuid4()), status=409)
    assert invalid["detail"]["code"] == "prd_candidate_changed"
    assert counts(db, project) == [0, 0, 1]

    saved = choose(client, project, intent)
    assert saved["status"] == "saved" and saved["saved_as"] == "pending"
    assert saved["revision_id"] and saved["artifact_id"] and saved["function_id"]
    assert counts(db, project) == [1, 1, 1]
    body = db.execute("SELECT markdown_body FROM ee.prd_versions WHERE revision_id=%s", (saved["revision_id"],)).fetchone()[0]
    assert body == agents[0]["prd_body"]
    assert db.execute("SELECT accepted_revision_id FROM ee.artifacts WHERE id=%s", (saved["artifact_id"],)).fetchone()[0] is None
    assert db.execute("SELECT name,description FROM ee.business_functions WHERE id=%s", (saved["function_id"],)).fetchone() == ("计划新功能", agents[0]["output"])
    assert calls[1][2] == "原始产品需求" and calls[1][3]
    assert choose(client, project, intent) == saved
    assert choose(client, project, saved, action="continue") == saved
    assert counts(db, project) == [1, 1, 1] and len(calls) == 2

    duplicate = client.post(f"/v1/projects/{project}/functions", headers=HEADERS,
                            json={"creation_id": str(uuid4()), "name": "  计划新功能 ", "description": "other"})
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "function_name_conflict"
    another = new_function(client, project, "暂定名")
    rename = client.patch(f"/v1/projects/{project}/functions/{another}", headers=HEADERS,
                          json={"mutation_id": str(uuid4()), "base_lock_version": "0", "name": "计划新功能"})
    assert rename.status_code == 409 and rename.json()["detail"]["code"] == "function_name_conflict"
    assert read(client, project, intent["id"]) == saved


def test_existing_prd_blocks_reuse_without_agent_or_new_prd(client, db, agents):
    state, calls = agents
    project = new_project(client)
    fid = new_function(client, project, "计划新功能")
    old_prd = create_prd(client, project, fid)
    intent = start(client, project)
    assert intent["review"]["candidate_count"] == 1
    assert intent["review"]["candidates"][0]["blocked_reason"] == "prd_exists"
    blocked = choose(client, project, intent, action="reuse", function_id=fid)
    assert blocked["status"] == "blocked" and blocked["artifact_id"] == old_prd
    assert blocked["error_code"] == "prd_exists" and counts(db, project) == [1, 1, 1]
    assert len(calls) == 1
    assert choose(client, project, blocked, action="reuse", function_id=fid) == blocked


def test_name_conflict_requires_change_and_similar_new_name_allowed(client, db, agents):
    project = new_project(client)
    existing = new_function(client, project, "计划新功能")
    intent = start(client, project)
    conflict = choose(client, project, intent, status=409)
    assert conflict["detail"]["code"] == "function_name_conflict"
    assert counts(db, project) == [1, 0, 1]
    renamed = choose(client, project, intent, name="计划新功能增强")
    assert renamed["status"] == "saved" and renamed["function_id"] != existing
    assert db.execute("SELECT name FROM ee.business_functions WHERE id=%s", (renamed["function_id"],)).fetchone()[0] == "计划新功能增强"
    assert counts(db, project) == [2, 1, 1]


def test_failure_retains_function_then_retries_without_duplicate(client, db, agents):
    state, calls = agents
    project = new_project(client)
    intent = start(client, project)
    state["fail"] = True
    result = choose(client, project, intent, status=502)
    assert result["detail"]["code"] == "agent_upstream_error"
    failed = read(client, project, intent["id"])
    assert failed["status"] == "failed" and failed["function_id"]
    assert counts(db, project) == [1, 0, 1]
    state["fail"] = False
    saved = choose(client, project, failed, action="continue")
    assert saved["status"] == "saved" and saved["function_id"] == failed["function_id"]
    assert counts(db, project) == [1, 1, 1]
    assert calls[1][3] == calls[2][3]  # stable upstream idempotency key for the same source


def test_function_only_editor_reuse_does_not_require_project_create(client, db, agents):
    project = new_project(client)
    target = new_function(client, project, "计划新功能", "保留已有功能的原始定义")
    actor, user, subject = peer(db, project)
    role = db.execute("SELECT id FROM ee.access_roles WHERE code='function_editor'").fetchone()[0]
    db.execute("""INSERT INTO ee.access_bindings(id,project_id,user_id,role_id,scope_kind,function_id,created_by,updated_by)
        VALUES(%s,%s,%s,%s,'function',%s,%s,%s)""", (uuid4(), project, user, role, target, actor, actor))
    client.cookies.set("ts_session", create_session_token(subject))
    intent = start(client, project)
    assert not intent["review"]["can_create_new_function"]
    assert choose(client, project, intent, action="create", status=403)["detail"]["code"] == "business_write_forbidden"
    saved = choose(client, project, intent, action="reuse", function_id=target)
    assert saved["status"] == "saved" and saved["function_id"] == target
    assert counts(db, project) == [1, 1, 1]


def test_unauthorized_cross_user_or_project_cannot_read_or_execute(client, db, agents):
    project, other = new_project(client), new_project(client)
    intent = start(client, project)
    _, _, subject = peer(db, project)
    client.cookies.set("ts_session", create_session_token(subject))
    assert read(client, project, intent["id"], status=404)["detail"]["code"] == "prd_creation_not_found"
    assert read(client, other, intent["id"], status=404)
    assert choose(client, project, intent, status=404)["detail"]["code"] == "prd_creation_not_found"


def test_late_agent_completion_does_not_override_newer_attempt(client, db, agents):
    project = new_project(client)
    intent = start(client, project)
    state, calls = agents
    state["fail"] = True
    choose(client, project, intent, status=502)
    failed = read(client, project, intent["id"])
    # Distinct attempt token is required; a late output from the old attempt must not persist.
    with db.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM ee.prd_creation_intents WHERE id=%s", (intent["id"],)).fetchone()
    original_token = row["lease_token"]
    assert original_token is None
    state["fail"] = False
    saved = choose(client, project, failed, action="continue")
    assert saved["status"] == "saved"
    with pytest.raises(ProjectReadError) as caught:
        prd_creation.finish_generation("engineer01", {**row, "state": "generating", "lease_token": uuid4()}, "late", "late-request")
    assert caught.value.code == "prd_creation_attempt_replaced"
    assert counts(db, project) == [1, 1, 1]


def test_auto_creation_waits_for_new_similar_candidate_and_cross_project_id_is_rejected(client, db, agents):
    project, foreign = new_project(client), new_project(client)
    intent = start(client, project)
    new_function(client, project, "计划新功能增强", "existing details")
    foreign_id = new_function(client, foreign, "计划新功能", "foreign")
    reviewed = choose(client, project, intent, action="auto")
    assert reviewed["status"] == "planned" and reviewed["review"]["status"] == "needs_selection"
    assert counts(db, project) == [1, 0, 1] and len(agents[1]) == 1
    assert choose(client, project, intent, action="reuse", function_id=foreign_id, status=409)["detail"]["code"] == "prd_candidate_changed"
    saved = choose(client, project, reviewed, action="create")
    assert saved["status"] == "saved" and counts(db, project) == [2, 1, 1]


def test_plan_invalid_receipt_is_recoverable_but_replay_does_not_retry(client, db, agents):
    project, request = new_project(client), uuid4()
    state, calls = agents
    state["plan"] = None
    assert start(client, project, request, status=502)["detail"]["code"] == "agent_plan_invalid"
    failed = start(client, project, request)
    assert failed["status"] == "failed" and failed["function_id"] is None
    assert counts(db, project) == [0, 0, 1] and len(calls) == 1
    state["plan"] = {"feature_name": "计划新功能"}
    resumed = choose(client, project, failed, action="continue")
    assert resumed["status"] == "planned" and counts(db, project) == [0, 0, 1]
    assert calls[0][1]["idempotency_key"] == calls[1][1]["idempotency_key"]
    assert read(client, project, failed["id"]) == resumed


@pytest.mark.parametrize("change,expected", [("revoke", "business_write_forbidden"), ("rename", "prd_source_changed"), ("archive", "function_not_found")])
def test_permissions_and_sources_rechecked_after_agent_and_no_transaction_held(client, db, agents, monkeypatch, change, expected):
    project = new_project(client)
    intent = start(client, project)
    reader_role = db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_reader'").fetchone()[0]
    async def stream(context, **kwargs):
        # A DB connection/transaction is not retained while waiting on the Agent.
        assert db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name IN ('ts-business-write-api','ts-project-read-api') AND xact_start IS NOT NULL").fetchone()[0] == 0
        if change == "revoke":
            db.execute("UPDATE ee.access_bindings SET role_id=%s WHERE project_id=%s AND role_id=(SELECT id FROM ee.access_roles WHERE code='project_api_author')", (reader_role, project))
        elif change == "rename":
            db.execute("UPDATE ee.business_functions SET name='定义已变更',lock_version=lock_version+1 WHERE id=%s", (context.function_id,))
        else:
            db.execute("UPDATE ee.business_functions SET archived_at=now() WHERE id=%s", (context.function_id,))
        yield {"type": "done", "scene": "FROM_DEFINITION_CREATE_PRD", "output": "# 过期输出"}
    monkeypatch.setattr(prd_generation, "stream_prd_generation", stream)
    response = choose(client, project, intent, status=403 if change == "revoke" else 404 if change == "archive" else 409)
    assert response["detail"]["code"] == expected
    assert counts(db, project) == [1, 0, 1]
    assert db.execute("SELECT state,error_code FROM ee.prd_creation_intents WHERE id=%s", (intent["id"],)).fetchone() == ("failed", expected)


def test_new_manual_prd_during_agent_is_not_overwritten(client, db, agents, monkeypatch):
    from app.services import manual_work
    from app.schemas.manual_work import PrdCreate
    from uuid import UUID
    project = new_project(client)
    intent = start(client, project)
    created = []
    async def stream(context, **kwargs):
        result = manual_work.create_artifact("engineer01", UUID(project), context.function_id,
            PrdCreate(kind="PRD", creation_id=uuid4(), name="人工插入的PRD"), "local-race")
        created.append(str(result.artifact_id))
        yield {"type": "done", "scene": "FROM_DEFINITION_CREATE_PRD", "output": "# 不允许覆盖人工PRD"}
    monkeypatch.setattr(prd_generation, "stream_prd_generation", stream)
    blocked = choose(client, project, intent)
    assert blocked["status"] == "blocked" and blocked["artifact_id"] == created[0]
    assert counts(db, project) == [1, 1, 1]
    assert db.execute("SELECT markdown_body FROM ee.prd_versions WHERE revision_id=%s", (blocked["revision_id"],)).fetchone()[0] == "# 人工插入的PRD\n"


@pytest.mark.parametrize("body", [None, "", "x\x00y", "x" * 200001], ids=["null","empty","nul","oversize"])
def test_invalid_prd_never_creates_placeholder(client, db, agents, body):
    project = new_project(client)
    intent = start(client, project)
    agents[0]["prd_body"] = body
    assert choose(client, project, intent, status=502)["detail"]["code"] == "agent_invalid_response"
    assert counts(db, project) == [1, 0, 1]


def test_no_complete_event_no_partial_prd(client, db, agents, monkeypatch):
    project = new_project(client)
    intent = start(client, project)
    async def stream(context, **kwargs):
        yield {"type": "token", "content": "partial"}
    monkeypatch.setattr(prd_generation, "stream_prd_generation", stream)
    assert choose(client, project, intent, status=502)["detail"]["code"] == "agent_invalid_response"
    assert counts(db, project) == [1, 0, 1]


def test_concurrent_same_intent_runs_agent_once_and_returns_shared_receipt(client, db, agents, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    project = new_project(client)
    intent = start(client, project)
    entered, release = Event(), Event()
    calls = []
    async def stream(context, **kwargs):
        calls.append(context.function_id)
        entered.set()
        assert await asyncio.to_thread(release.wait, 15)
        yield {"type": "done", "scene": "FROM_DEFINITION_CREATE_PRD", "output": "# 一次结果"}
    monkeypatch.setattr(prd_generation, "stream_prd_generation", stream)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(choose, client, project, intent)
        try:
            assert entered.wait(10)
            duplicate = choose(client, project, intent)
            assert duplicate["status"] == "generating" and counts(db, project) == [1, 0, 1]
        finally:
            release.set()
        saved = first.result(timeout=20)
    assert len(calls) == 1 and saved["status"] == "saved"
    assert counts(db, project) == [1, 1, 1]


def test_expired_attempt_read_is_passive_and_late_completion_is_fenced(client, db, agents, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    project = new_project(client)
    intent = start(client, project)
    entered, release = Event(), Event()
    calls = []
    async def stream(context, **kwargs):
        calls.append(context.function_id)
        if len(calls) == 1:
            entered.set()
            assert await asyncio.to_thread(release.wait, 15)
            output = "# 旧超时结果"
        else:
            output = "# 新重试结果"
        yield {"type": "done", "scene": "FROM_DEFINITION_CREATE_PRD", "output": output}
    monkeypatch.setattr(prd_generation, "stream_prd_generation", stream)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(choose, client, project, intent, status=409)
        try:
            assert entered.wait(10)
            db.execute("UPDATE ee.prd_creation_intents SET lease_until=now()-interval '1 second' WHERE id=%s", (intent["id"],))
            expired = read(client, project, intent["id"])
            assert expired["status"] == "interrupted" and len(calls) == 1
            assert db.execute("SELECT state FROM ee.prd_creation_intents WHERE id=%s", (intent["id"],)).fetchone()[0] == "generating"
            saved = choose(client, project, expired, action="continue")
            assert saved["status"] == "saved"
        finally:
            release.set()
        assert old.result(timeout=20)["detail"]["code"] == "prd_creation_attempt_replaced"
    assert counts(db, project) == [1, 1, 1]
    assert db.execute("SELECT markdown_body FROM ee.prd_versions WHERE revision_id=%s", (saved["revision_id"],)).fetchone()[0] == "# 新重试结果"
    assert read(client, project, intent["id"]) == saved


def test_two_create_intents_same_name_are_serialized(client, db, agents):
    from concurrent.futures import ThreadPoolExecutor
    project = new_project(client)
    intents = [start(client, project), start(client, project)]
    def execute_raw(intent):
        return client.post(f"/v1/projects/{project}/prd-creations/intents/{intent['id']}/execute", headers=HEADERS,
                           json={"expected_version": intent["version"], "action": "create"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute_raw, intents))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert counts(db, project) == [1, 1, 2]
    assert len([c for c in agents[1] if c[0] == "prd"]) == 1


def test_manual_name_normalization_and_archived_recreation(client, db):
    project, other = new_project(client), new_project(client)
    fid = new_function(client, project, "Straße   DVR")
    data = {"creation_id": str(uuid4()), "name": "STRASSE DVR"}
    response = client.post(f"/v1/projects/{project}/functions", headers=HEADERS, json=data)
    assert response.status_code == 409
    assert client.post(f"/v1/projects/{other}/functions", headers=HEADERS, json=data).status_code == 201
    db.execute("UPDATE ee.business_functions SET archived_at=now() WHERE id=%s", (fid,))
    data["creation_id"] = str(uuid4())
    created = client.post(f"/v1/projects/{project}/functions", headers=HEADERS, json=data)
    assert created.status_code == 201
    replay = client.post(f"/v1/projects/{project}/functions", headers=HEADERS, json=data)
    assert replay.status_code == 200 and replay.json()["function_id"] == created.json()["function_id"]


def test_schema_is_narrowly_granted_and_reads_cannot_mutate(db):
    import psycopg
    reader, writer = os.environ["TEST_MANUAL_READER_DSN"], os.environ["TEST_MANUAL_WRITER_DSN"]
    with psycopg.connect(reader) as conn:
        assert conn.execute("SELECT count(*) FROM ee.prd_creation_intents").fetchone()[0] >= 0
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)):
            with conn.transaction():
                conn.execute("UPDATE ee.prd_creation_intents SET state='failed' WHERE false")
    with psycopg.connect(writer) as conn:
        for sql in ("DELETE FROM ee.prd_creation_intents WHERE false", "UPDATE ee.prd_creation_intents SET prompt='tampered' WHERE false",
                    "UPDATE ee.prd_creation_intents SET user_id=gen_random_uuid() WHERE false"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(sql)


def test_disabled_write_guard_preserves_durable_intent_and_no_agent(client, db, agents, monkeypatch):
    project = new_project(client)
    intent = start(client, project)
    monkeypatch.setattr(settings, "business_writes_enabled", False)
    assert choose(client, project, intent, status=503)["detail"]["code"] == "business_writes_disabled"
    assert len(agents[1]) == 1 and counts(db, project) == [0, 0, 1]
    assert read(client, project, intent["id"])["review"]["status"] == "blocked"


def test_plan_concurrent_replay_claims_only_one_agent_call(client, db, agents, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    project, request_id = new_project(client), uuid4()
    entered, release, seen = Event(), Event(), []
    async def plan(body):
        seen.append(body)
        entered.set()
        assert await asyncio.to_thread(release.wait, 15)
        return {"scene": "FEATURE_DEFINITION", "plan": {"feature_name": "单次计划"}, "output": "功能定义"}
    monkeypatch.setattr(workflow_agent, "invoke_envelope", plan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start, client, project, request_id)
        try:
            assert entered.wait(10)
            pending = start(client, project, request_id)
            assert pending["status"] == "planning" and counts(db, project) == [0, 0, 1]
        finally:
            release.set()
        planned = first.result(timeout=20)
    assert planned["id"] == pending["id"] and planned["status"] == "planned" and len(seen) == 1


def test_save_rollback_removes_partial_prd_but_preserves_function(client, db, agents, monkeypatch):
    from app.services import manual_work
    project = new_project(client)
    intent = start(client, project)
    original = manual_work.append_revision
    def failing_append(*args, **kwargs):
        original(*args, **kwargs)
        raise ProjectReadError(409, "simulated_save_conflict", "local save rollback")
    monkeypatch.setattr(manual_work, "append_revision", failing_append)
    assert choose(client, project, intent, status=409)["detail"]["code"] == "simulated_save_conflict"
    assert counts(db, project) == [1, 0, 1]
    assert db.execute("SELECT count(*) FROM ee.prd_versions WHERE project_id=%s", (project,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ee.artifact_revisions WHERE project_id=%s", (project,)).fetchone()[0] == 0
    failed = read(client, project, intent["id"])
    assert failed["status"] == "failed" and failed["function_id"]
    monkeypatch.setattr(manual_work, "append_revision", original)
    saved = choose(client, project, failed, action="continue")
    assert saved["status"] == "saved" and saved["function_id"] == failed["function_id"]


def test_deleted_saved_prd_does_not_recreate_on_replay(client, db, agents):
    project = new_project(client)
    intent = start(client, project)
    saved = choose(client, project, intent)
    db.execute("UPDATE ee.artifacts SET archived_at=now() WHERE id=%s", (saved["artifact_id"],))
    assert choose(client, project, intent, status=404)["detail"]["code"] == "artifact_not_found"
    assert read(client, project, intent["id"], status=404)["detail"]["code"] == "artifact_not_found"
    assert counts(db, project) == [1, 1, 1] and len(agents[1]) == 2


def test_replay_different_bound_choice_is_rejected(client, db, agents):
    project = new_project(client)
    intent = start(client, project)
    saved = choose(client, project, intent)
    assert choose(client, project, intent, name="different", status=409)["detail"]["code"] == "prd_creation_choice_conflict"
    assert choose(client, project, saved, action="reuse", function_id=saved["function_id"], status=409)["detail"]["code"] == "prd_creation_choice_conflict"
    assert counts(db, project) == [1, 1, 1]


@pytest.mark.parametrize("competitor", ["create", "rename"])
def test_manual_create_or_rename_race_with_automatic_creation(client, db, agents, competitor):
    from concurrent.futures import ThreadPoolExecutor
    project = new_project(client)
    other = new_function(client, project, "人工功能") if competitor == "rename" else None
    intent = start(client, project)
    def automatic():
        return client.post(f"/v1/projects/{project}/prd-creations/intents/{intent['id']}/execute", headers=HEADERS,
                           json={"expected_version": intent["version"], "action": "create"})
    def manual():
        if other:
            return client.patch(f"/v1/projects/{project}/functions/{other}", headers=HEADERS,
                                json={"mutation_id": str(uuid4()), "base_lock_version": "0", "name": "计划新功能"})
        return client.post(f"/v1/projects/{project}/functions", headers=HEADERS,
                           json={"creation_id": str(uuid4()), "name": "计划新功能"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(automatic), pool.submit(manual)
        results = [one.result(timeout=20), two.result(timeout=20)]
    assert sum(r.status_code in (200,201) for r in results) == 1
    assert sum(r.status_code == 409 for r in results) == 1
    assert db.execute("SELECT count(*) FROM ee.business_functions WHERE project_id=%s AND name=%s AND archived_at IS NULL", (project,"计划新功能")).fetchone()[0] == 1


def test_generation_response_lost_replays_committed_receipt_not_agent(client, db, agents, monkeypatch):
    project = new_project(client)
    intent = start(client, project)
    original = prd_creation.read_intent
    def lost(*args):
        # S5 reads the original intent first to recover its bound thread.
        # Lose only the post-commit response, not the pre-execution ownership read.
        receipt = original(*args)
        if receipt.status == "saved":
            raise ProjectReadError(503, "simulated_receipt_read_failure", "local response lost")
        return receipt
    monkeypatch.setattr(prd_creation, "read_intent", lost)
    assert choose(client, project, intent, status=503)["detail"]["code"] == "simulated_receipt_read_failure"
    assert counts(db, project) == [1, 1, 1]
    monkeypatch.setattr(prd_creation, "read_intent", original)
    saved = choose(client, project, intent)
    assert saved["status"] == "saved" and len(agents[1]) == 2


def test_missing_intent_schema_fails_before_agent_or_business_write(client, db, agents):
    project = new_project(client)
    db.execute("ALTER TABLE ee.prd_creation_intents RENAME TO local_hidden_intents")
    try:
        assert start(client, project, status=503)["detail"]["code"] == "business_write_unavailable"
        assert agents[1] == []
        assert db.execute("SELECT count(*) FROM ee.business_functions WHERE project_id=%s", (project,)).fetchone()[0] == 0
    finally:
        db.execute("ALTER TABLE ee.local_hidden_intents RENAME TO prd_creation_intents")


@pytest.mark.parametrize("mode", ["buffered", "stream", "interrupt", "broken-stream"])
def test_real_agent_adapters_with_db_preserve_text_plan_and_prd_contract(client, db, monkeypatch, mode):
    import json
    import httpx
    from pydantic import SecretStr
    requests = []
    project = new_project(client)
    monkeypatch.setattr(settings, "agent_generation_invoke_url", "http://127.0.0.1:1/api/v1/agent/invoke")
    monkeypatch.setattr(settings, "agent_invoke_url", "http://127.0.0.1:1/api/v1/agent/invoke/stream")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("isolated-transport-key"))
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers['X-API-Key'] == 'isolated-transport-key'
        assert body['response_format'] == 'text'
        if body['scene'] == 'FEATURE_DEFINITION':
            assert request.url.path == '/api/v1/agent/invoke'
            return httpx.Response(200, json={'scene': 'FEATURE_DEFINITION', 'plan': {'feature_name': '协议生成名'}, 'output': '协议功能定义'})
        assert request.url.path == '/api/v1/agent/invoke/stream'
        assert body['scene'] == 'FROM_DEFINITION_CREATE_PRD'
        assert body['inputs']['feature_name'] == '协议生成名'
        assert body['reference_data']['original_prompt'] == '原始产品需求'
        assert body['reference_data']['function_definition'] == '协议功能定义'
        assert db.execute('SELECT id FROM ee.business_functions WHERE project_id=%s', (project,)).fetchone()[0].hex == body['reference_data']['function']['id'].replace('-','')
        assert db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name IN ('ts-business-write-api','ts-project-read-api') AND xact_start IS NOT NULL").fetchone()[0] == 0
        done = {'type': 'done', 'scene': 'FROM_DEFINITION_CREATE_PRD', 'output': '# 协议PRD'}
        if mode == 'interrupt':
            return httpx.Response(200, json={**done, 'interrupt': {'reason': 'pending'}})
        if mode == 'buffered':
            return httpx.Response(200, json=done)
        events = [{'type': 'token', 'content': '# 协议PRD'}]
        if mode == 'stream':
            events.append(done)
        return httpx.Response(200, content='\n'.join(json.dumps(x,ensure_ascii=False) for x in events)+'\n',
                              headers={'content-type': 'text/event-stream'})
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(workflow_agent, 'agent_client', factory)
    monkeypatch.setattr(prd_generation, 'agent_client', factory)
    intent = start(client, project)
    result = choose(client, project, intent, action='auto', status=502 if mode in ('interrupt','broken-stream') else 200)
    assert len(requests) == 2
    if mode in ('interrupt','broken-stream'):
        assert result['detail']['code'] == 'agent_invalid_response'
        assert counts(db, project) == [1,0,1]
    else:
        assert result['status'] == 'saved' and counts(db, project) == [1,1,1]
        assert requests[1]['idempotency_key'] and requests[0]['idempotency_key'] != requests[1]['idempotency_key']
        assert db.execute('SELECT markdown_body FROM ee.prd_versions WHERE revision_id=%s',(result['revision_id'],)).fetchone()[0] == '# 协议PRD'


def test_disabled_writer_does_not_advertise_scoped_reuse(client, db, agents, monkeypatch):
    project = new_project(client)
    fid = new_function(client, project, "计划新功能", "定义")
    actor, user, subject = peer(db, project)
    role = db.execute("SELECT id FROM ee.access_roles WHERE code='function_editor'").fetchone()[0]
    db.execute("""INSERT INTO ee.access_bindings(id,project_id,user_id,role_id,scope_kind,function_id,created_by,updated_by)
        VALUES(%s,%s,%s,%s,'function',%s,%s,%s)""", (uuid4(), project, user, role, fid, actor, actor))
    client.cookies.set("ts_session", create_session_token(subject))
    intent = start(client, project)
    assert intent['review']['candidates'][0]['can_reuse_for_prd']
    monkeypatch.setattr(settings,'business_writes_enabled',False)
    receipt = read(client, project, intent['id'])
    assert not receipt['review']['can_create_new_function']
    assert not receipt['review']['candidates'][0]['can_reuse_for_prd']
    assert len(agents[1]) == 1


def test_find_initial_response_by_request_key_is_readonly_and_owner_scoped(client, db, agents):
    project, request = new_project(client), uuid4()
    url = f'/v1/projects/{project}/prd-creations/intents'
    absent = client.get(url,params={'client_request_id':str(request)})
    assert absent.status_code == 404 and counts(db,project) == [0,0,0] and agents[1] == []
    intent = start(client,project,request)
    found = client.get(url,params={'client_request_id':str(request)})
    assert found.status_code == 200 and found.json() == intent and len(agents[1]) == 1
    assert found.headers['Cache-Control'] == 'no-store'
    _,_,subject = peer(db,project)
    client.cookies.set('ts_session',create_session_token(subject))
    assert client.get(url,params={'client_request_id':str(request)}).status_code == 404
    assert counts(db,project) == [0,0,1] and len(agents[1]) == 1
