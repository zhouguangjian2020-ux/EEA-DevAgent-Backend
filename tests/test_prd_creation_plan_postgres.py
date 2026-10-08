"""S1 only: real isolated PostgreSQL authorization and zero-write guarantees."""
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from tests.test_manual_work_postgres import db, client, HEADERS
from app.core.config import settings
from app.core.security import create_session_token
from app.services import prd_creation_plan as service, workflow_agent


def new_project(client):
    response = client.post("/v1/projects", headers=HEADERS,
                           json={"creation_id": str(uuid4()), "name": "计划只读验收-" + uuid4().hex[:8]})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def new_function(client, project, name, description=""):
    response = client.post(f"/v1/projects/{project}/functions", headers=HEADERS,
                           json={"creation_id": str(uuid4()), "name": name, "description": description})
    assert response.status_code == 201, response.text
    return response.json()["function_id"]


def create_prd(client, project, fid):
    response = client.post(f"/v1/projects/{project}/functions/{fid}/artifacts", headers=HEADERS,
                           json={"creation_id": str(uuid4()), "kind": "PRD", "name": "现有PRD"})
    assert response.status_code == 201, response.text
    return response.json()["artifact_id"]


def plan(client, project, status=200):
    response = client.post(f"/v1/projects/{project}/prd-creations/plan", headers=HEADERS,
                           json={"client_request_id": str(uuid4()), "message": "用户原始功能需求"})
    assert response.status_code == status, response.text
    return response.json()


def snapshot(db):
    tables = db.execute("SELECT tablename FROM pg_tables WHERE schemaname='ee' ORDER BY tablename").fetchall()
    return {table: db.execute(sql.SQL(
        "SELECT count(*),md5(coalesce(string_agg(row_to_json(t)::text,'' ORDER BY row_to_json(t)::text),'')) FROM ee.{} t"
    ).format(sql.Identifier(table))).fetchone() for table, in tables}


@pytest.fixture
def agent(monkeypatch):
    captured = []
    state = {"scene": service.SCENE, "plan": {"feature_name": "计划新功能"}, "output": "这是Agent返回的功能定义正文。"}
    async def invoke(body):
        captured.append(body)
        return state
    monkeypatch.setattr(workflow_agent, "invoke_envelope", invoke)
    return state, captured


def test_plan_reads_low_privilege_snapshot_and_never_writes(client, db, agent, monkeypatch):
    state, calls = agent
    project = new_project(client)
    before = snapshot(db)
    observed = []
    real_read_db = service.read_db
    @contextmanager
    def readonly():
        with real_read_db() as connection:
            observed.append(connection.execute("SELECT current_user, current_setting('transaction_read_only') AS readonly").fetchone())
            yield connection
    monkeypatch.setattr(service, "read_db", readonly)
    result = plan(client, project)
    assert result["status"] == "ready_to_create" and result["candidates"] == []
    assert result["can_create_new_function"] and not result["exact_name_conflict"]
    assert result["definition"]["name"] == state["plan"]["feature_name"]
    assert result["definition"]["description"] == state["output"]
    assert len(calls) == 1 and calls[0]["response_format"] == "text"
    assert calls[0]["reference_data"] == {"project_id": project}
    assert len(observed) == 2 and all(row["current_user"] == "ee_project_api_ro" and row["readonly"] == "on" for row in observed)
    assert snapshot(db) == before


def test_only_current_active_functions_match_and_existing_prd_blocks_reuse(client, db, agent):
    state, calls = agent
    project, other = new_project(client), new_project(client)
    name = "DVR 循环录像"
    state["plan"]["feature_name"] = "  dvr  循环录像 "
    active = new_function(client, project, name)
    archived = new_function(client, project, "已归档旧功能")
    foreign = new_function(client, other, name)
    similar = new_function(client, project, name + "增强")
    db.execute("UPDATE ee.business_functions SET name=%s,archived_at=now() WHERE id=%s", (name, archived))
    prd = create_prd(client, project, active)
    before = snapshot(db)
    result = plan(client, project)
    assert result["status"] == "needs_selection" and result["exact_name_conflict"]
    rows = {row["function_id"]: row for row in result["candidates"]}
    assert set(rows) == {active, similar} and archived not in rows and foreign not in rows
    assert result["candidates"][0]["function_id"] == active
    assert rows[active]["prd_artifact_id"] == prd and rows[active]["blocked_reason"] == "prd_exists"
    assert not rows[active]["can_reuse_for_prd"] and rows[similar]["can_reuse_for_prd"]
    assert len(calls) == 1 and snapshot(db) == before


def test_invalid_plan_does_not_write_placeholders(client, db, agent):
    state, calls = agent
    project = new_project(client)
    state["plan"] = None
    before = snapshot(db)
    response = plan(client, project, 502)
    assert response["detail"]["code"] == "agent_plan_invalid"
    assert len(calls) == 1 and snapshot(db) == before


def peer(db, project):
    actor = db.execute("SELECT id FROM ee.app_users WHERE external_subject='engineer01'").fetchone()[0]
    user_id, subject = uuid4(), "planner-" + uuid4().hex
    db.execute("INSERT INTO ee.app_users(id,external_subject,display_name) VALUES(%s,%s,%s)", (user_id, subject, "只读计划测试"))
    db.execute("INSERT INTO ee.project_members(project_id,user_id,added_by) VALUES(%s,%s,%s)", (project, user_id, actor))
    role = db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_reader'").fetchone()[0]
    db.execute("""INSERT INTO ee.access_bindings(id,project_id,user_id,role_id,scope_kind,created_by,updated_by)
        VALUES(%s,%s,%s,%s,'project',%s,%s)""", (uuid4(), project, user_id, role, actor, actor))
    return actor, user_id, subject


def test_reader_foreign_project_and_write_disabled_never_call_agent(client, db, agent, monkeypatch):
    _, calls = agent
    project = new_project(client)
    _, _, subject = peer(db, project)
    client.cookies.set("ts_session", create_session_token(subject))
    before = snapshot(db)
    assert plan(client, project, 403)["detail"]["code"] == "prd_creation_forbidden"
    assert plan(client, str(uuid4()), 404)["detail"]["code"] == "project_not_found"
    monkeypatch.setattr(settings, "business_writes_enabled", False)
    assert plan(client, project, 503)["detail"]["code"] == "business_writes_disabled"
    assert calls == [] and snapshot(db) == before


def test_function_scoped_editor_may_reuse_without_project_create(client, db, agent):
    state, calls = agent
    project = new_project(client)
    target = new_function(client, project, "目标功能")
    other = new_function(client, project, "目标功能增强")
    actor, user_id, subject = peer(db, project)
    role = db.execute("SELECT id FROM ee.access_roles WHERE code='function_editor'").fetchone()[0]
    db.execute("""INSERT INTO ee.access_bindings(id,project_id,user_id,role_id,scope_kind,function_id,created_by,updated_by)
        VALUES(%s,%s,%s,%s,'function',%s,%s,%s)""", (uuid4(), project, user_id, role, target, actor, actor))
    client.cookies.set("ts_session", create_session_token(subject))
    state["plan"]["feature_name"] = "目标功能"
    before = snapshot(db)
    result = plan(client, project)
    rows = {row["function_id"]: row for row in result["candidates"]}
    assert not result["can_create_new_function"] and rows[target]["can_reuse_for_prd"]
    assert not rows[other]["can_reuse_for_prd"] and rows[other]["blocked_reason"] == "prd_create_forbidden"
    state["plan"]["feature_name"] = "毫不相关的新建项目名称"
    result = plan(client, project)
    assert result["status"] == "blocked" and not result["candidates"]
    assert len(calls) == 2 and snapshot(db) == before


def test_catalog_limit_cannot_misreport_no_duplicates(client, db, agent, monkeypatch):
    _, calls = agent
    project = new_project(client)
    for i in range(3):
        new_function(client, project, "功能" + str(i))
    monkeypatch.setattr(service, "MAX_FUNCTIONS", 2)
    before = snapshot(db)
    result = plan(client, project, 413)
    assert result["detail"]["code"] == "feature_catalog_too_large"
    assert calls == [] and snapshot(db) == before
