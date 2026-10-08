import uuid

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import agent as agent_route
from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.services.uc_generation import ConfirmedPrd, UcGenerationAgentResult
from app.schemas.agent import UcGenerationCandidate

PATH = "/v1/agent/generations"
PROJECT = "10d4c8a0-ff3a-56c3-abf6-18dc3b531e62"
FUNCTION = "20d4c8a0-ff3a-56c3-abf6-18dc3b531e62"
PRD = "30d4c8a0-ff3a-56c3-abf6-18dc3b531e62"
REVISION = "40d4c8a0-ff3a-56c3-abf6-18dc3b531e62"


def payload(client_request_id=None):
    return {
        "scene": "FROM_PRD_CREATE_UC",
        "project_id": PROJECT,
        "function_id": FUNCTION,
        "source": {"kind": "PRD", "artifact_id": PRD, "revision_id": REVISION},
        "client_request_id": client_request_id or str(uuid.uuid4()),
    }


def client():
    result = TestClient(app)
    result.cookies.set("ts_session", create_session_token(settings.demo_username))
    return result


def candidate():
    return UcGenerationCandidate(
        candidate_id=uuid.uuid4(),
        ordinal=1,
        name="自动记录事件",
        description="系统根据已确认PRD记录一次事件。",
        actors="驾驶员；系统",
        preconditions="设备已上电且自检通过。",
        scenario={
            "main_flow": "1. 参与者触发事件。\n2. 系统校验条件并完成记录。",
            "branches": ["用户主动触发", "系统自动触发", "重复事件合并", "<待确认>"],
            "exceptions": ["车辆状态异常", "执行器异常", "环境异常", "误用或非法操作", "电源异常"],
        },
        postconditions="事件记录可被查询。",
        extensions="<待确认>",
        hmi="显示处理结果。",
        regulations="<待确认>",
        performance="<待标定>",
        nonfunctional="数据一致性和可追溯性。",
    )


def test_uc_generation_requires_project_read_database_before_agent(monkeypatch):
    request_id = str(uuid.uuid4())
    called = False

    async def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Agent must not be called before PRD read succeeds")

    monkeypatch.setattr(agent_route, "generate_uc_from_prd", forbidden)
    with client() as http:
        response = http.post(PATH, json=payload(request_id), headers={"Idempotency-Key": request_id})
    assert response.status_code == 503
    assert response.headers["X-Request-ID"]
    assert response.json()["detail"]["code"] == "projects_not_configured"
    assert called is False


def test_uc_generation_reads_server_side_prd_and_returns_transient_candidates(monkeypatch):
    request_id = str(uuid.uuid4())
    read_calls = []
    generate_calls = []
    prd = ConfirmedPrd(
        project_id=uuid.UUID(PROJECT),
        function_id=uuid.UUID(FUNCTION),
        prd_id=uuid.UUID(PRD),
        revision_id=uuid.UUID(REVISION),
        markdown_body="# 已确认PRD\n不能由浏览器提交",
    )

    def fake_read(subject, **kwargs):
        read_calls.append((subject, kwargs))
        return prd

    async def fake_generate(prd_arg, **kwargs):
        generate_calls.append((prd_arg, kwargs))
        return UcGenerationAgentResult(candidates=[candidate()], trace_id="trace-uc")

    monkeypatch.setattr(agent_route, "read_confirmed_prd", fake_read)
    monkeypatch.setattr(agent_route, "generate_uc_from_prd", fake_generate)
    with client() as http:
        response = http.post(PATH, json=payload(request_id), headers={"Idempotency-Key": request_id})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["source"] == {
        "project_id": PROJECT,
        "function_id": FUNCTION,
        "prd_id": PRD,
        "prd_revision_id": REVISION,
    }
    assert body["candidates"][0]["name"] == "自动记录事件"
    assert body["trace_id"] == "trace-uc"
    assert "不能由浏览器提交" not in response.text
    assert read_calls == [(settings.demo_username, {
        "project_id": uuid.UUID(PROJECT),
        "function_id": uuid.UUID(FUNCTION),
        "prd_id": uuid.UUID(PRD),
        "revision_id": uuid.UUID(REVISION),
    })]
    assert generate_calls[0][0] == prd
    assert generate_calls[0][1]["client_request_id"] == uuid.UUID(request_id)


def test_uc_generation_contract_rejects_mismatched_idempotency_key():
    request_id = str(uuid.uuid4())
    with client() as http:
        response = http.post(PATH, json=payload(request_id), headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "generation_request_id_mismatch"


def test_uc_generation_contract_requires_authentication():
    with client() as http:
        http.cookies.clear()
        response = http.post(PATH, json=payload())
    assert response.status_code == 401


def test_uc_generation_contract_forbids_unknown_fields():
    body = payload()
    body["prd_content"] = "不允许由浏览器直接提交PRD正文"
    with client() as http:
        response = http.post(PATH, json=body)
    assert response.status_code == 422
