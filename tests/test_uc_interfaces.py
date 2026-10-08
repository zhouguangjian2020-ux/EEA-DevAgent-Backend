import asyncio
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes import agent as agent_route
from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.schemas.agent import UcCandidateContent
from app.services.uc_diagram import AcceptedUc, AcceptedUcSet, UcDiagramAgentResult, UcDiagramError, _parse_agent_diagram, generate_uc_diagram

PROJECT = uuid.UUID("10d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
FUNCTION = uuid.UUID("20d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
UC = uuid.UUID("60d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
UC_REVISION = uuid.UUID("70d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
PRD_REVISION = uuid.UUID("40d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
DIAGRAM_PATH = "/v1/agent/uc-diagram"
TEMPLATE_PATH = "/v1/agent/uc-template?scene=FROM_PRD_CREATE_UC"


def client() -> TestClient:
    result = TestClient(app)
    result.cookies.set("ts_session", create_session_token(settings.demo_username))
    return result


def accepted_uc() -> AcceptedUc:
    return AcceptedUc(
        uc_id=UC,
        uc_revision_id=UC_REVISION,
        prd_revision_id=PRD_REVISION,
        number="UC-01",
        name="记录事件",
        description="系统记录事件。",
        actors="驾驶员；系统",
        preconditions="设备已上电。",
        main_flow="1. 触发。\\n2. 记录。",
        branches=["主动触发", "自动触发"],
        exceptions=["车辆状态异常"],
        postconditions="事件可查询。",
        extensions="<待确认>",
        hmi="显示结果。",
        regulations="<待确认>",
        performance="<待标定>",
        nonfunctional="可追溯。",
    )


def source(*ucs: AcceptedUc) -> AcceptedUcSet:
    from app.services.uc_diagram import _source_version

    items = list(ucs)
    return AcceptedUcSet(PROJECT, FUNCTION, items, _source_version(PROJECT, FUNCTION, items))


def diagram_payload(code="flowchart LR\\n  A[\"驾驶员\"] --> B((\"记录事件\"))"):
    return {"scene": "FROM_UC_CREATE_UC_DIAGRAM", "structured": {"diagram_format": "mermaid", "diagram_code": code}, "trace_id": "trace-diagram"}


def test_template_requires_service_key_and_exposes_12_top_level_fields(monkeypatch):
    monkeypatch.setattr(settings, "agent_callback_key", SecretStr("template-key"))
    with TestClient(app) as http:
        missing = http.get(TEMPLATE_PATH)
        wrong = http.get(TEMPLATE_PATH, headers={"X-Agent-Internal-Key": "wrong"})
        ok = http.get(TEMPLATE_PATH, headers={"X-Agent-Internal-Key": "template-key"})
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["scene"] == "FROM_PRD_CREATE_UC"
    assert len(body["fields"]) == 12
    assert [field["name"] for field in body["fields"]] == [
        "name", "description", "number", "actors", "preconditions", "scenario",
        "postconditions", "extensions", "hmi", "regulations", "performance", "nonfunctional",
    ]
    scenario = next(field for field in body["fields"] if field["name"] == "scenario")
    assert [field["name"] for field in scenario["children"]] == ["main_flow", "branches", "exceptions"]
    scenario_schema = body["candidate_schema"]["$defs"]["UcScenario"]
    assert scenario_schema["properties"]["branches"]["type"] == "array"
    assert "X-Request-ID" in ok.headers


def test_template_not_configured_returns_structured_503(monkeypatch):
    monkeypatch.setattr(settings, "agent_callback_key", SecretStr(""))
    with TestClient(app) as http:
        response = http.get(TEMPLATE_PATH, headers={"X-Agent-Internal-Key": "anything"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "agent_template_not_configured"


def test_diagram_empty_uc_set_does_not_call_agent(monkeypatch):
    calls = 0

    async def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("empty UC source must not call Agent")

    monkeypatch.setattr(agent_route, "read_accepted_ucs", lambda *args, **kwargs: source())
    monkeypatch.setattr(agent_route, "generate_uc_diagram", forbidden)
    request_id = str(uuid.uuid4())
    with client() as http:
        response = http.post(DIAGRAM_PATH, json={
            "scene": "FROM_UC_CREATE_UC_DIAGRAM", "project_id": str(PROJECT), "function_id": str(FUNCTION),
            "known_uc_source_version": None, "client_request_id": request_id,
        }, headers={"Idempotency-Key": request_id})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "empty"
    assert response.json()["source"]["uc_count"] == 0
    assert calls == 0


def test_diagram_unchanged_source_does_not_call_agent(monkeypatch):
    current = source(accepted_uc())
    called = False

    async def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("unchanged UC source must not call Agent")

    monkeypatch.setattr(agent_route, "read_accepted_ucs", lambda *args, **kwargs: current)
    monkeypatch.setattr(agent_route, "generate_uc_diagram", forbidden)
    request_id = str(uuid.uuid4())
    with client() as http:
        response = http.post(DIAGRAM_PATH, json={
            "scene": "FROM_UC_CREATE_UC_DIAGRAM", "project_id": str(PROJECT), "function_id": str(FUNCTION),
            "known_uc_source_version": current.source_version, "client_request_id": request_id,
        }, headers={"Idempotency-Key": request_id})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unchanged"
    assert response.json()["source_version"] == current.source_version
    assert called is False


def test_diagram_changed_source_sends_all_accepted_ucs_and_returns_code(monkeypatch):
    current = source(accepted_uc())
    calls = []

    async def fake_generate(source_arg, **kwargs):
        calls.append((source_arg, kwargs))
        return UcDiagramAgentResult("flowchart LR\\n  A --> B", "mermaid", "trace-diagram")

    monkeypatch.setattr(agent_route, "read_accepted_ucs", lambda *args, **kwargs: current)
    monkeypatch.setattr(agent_route, "generate_uc_diagram", fake_generate)
    request_id = str(uuid.uuid4())
    with client() as http:
        response = http.post(DIAGRAM_PATH, json={
            "scene": "FROM_UC_CREATE_UC_DIAGRAM", "project_id": str(PROJECT), "function_id": str(FUNCTION),
            "known_uc_source_version": "0" * 64, "client_request_id": request_id,
        }, headers={"Idempotency-Key": request_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["diagram_format"] == "mermaid"
    assert body["diagram_code"] == "flowchart LR\\n  A --> B"
    assert len(calls) == 1
    assert len(calls[0][0].ucs) == 1
    assert calls[0][0].ucs[0].to_reference()["content"]["scenario"]["branches"] == ["主动触发", "自动触发"]


def test_diagram_agent_request_contains_nested_uc_content(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke")
    monkeypatch.setattr(settings, "agent_generation_invoke_url", "")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("unit-test-key"))

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["scene"] == "FROM_UC_CREATE_UC_DIAGRAM"
        assert body["reference_data"]["uc_list"][0]["content"]["scenario"]["branches"] == ["主动触发", "自动触发"]
        assert body["reference_data"]["uc_list"][0]["content"]["scenario"]["exceptions"] == ["车辆状态异常"]
        return httpx.Response(200, json=diagram_payload())

    import app.services.uc_diagram as service
    monkeypatch.setattr(service, "agent_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(generate_uc_diagram(source(accepted_uc()), client_request_id=uuid.uuid4(), request_id="request"))
    assert len(calls) == 1
    assert result.diagram_format == "mermaid"
    assert result.diagram_code.startswith("flowchart")


@pytest.mark.parametrize("payload", [
    {"scene": "WRONG", "structured": {"diagram_format": "mermaid", "diagram_code": "flowchart LR"}},
    {"scene": "FROM_UC_CREATE_UC_DIAGRAM", "structured": {"diagram_format": "svg", "diagram_code": "<svg/>"}},
    {"scene": "FROM_UC_CREATE_UC_DIAGRAM", "structured": {"diagram_format": "mermaid", "diagram_code": ""}},
    {"scene": "FROM_UC_CREATE_UC_DIAGRAM", "structured": {"diagram_format": "mermaid", "diagram_code": "flowchart LR", "extra": 1}},
])
def test_diagram_parser_rejects_invalid_agent_envelopes(payload):
    with pytest.raises(UcDiagramError) as error:
        _parse_agent_diagram(json.dumps(payload).encode("utf-8"))
    assert error.value.code == "agent_invalid_response"


def test_candidate_accepts_variable_branch_and_exception_counts():
    candidate = UcCandidateContent.model_validate({
        "name": "记录事件", "description": "系统记录事件。", "actors": "系统", "preconditions": "设备已上电。",
        "scenario": {"main_flow": "1. 触发。", "branches": ["主动触发"], "exceptions": []},
        "postconditions": "事件可查询。", "extensions": "<待确认>", "hmi": "显示结果。",
        "regulations": "<待确认>", "performance": "<待标定>", "nonfunctional": "可追溯。",
    })
    assert candidate.scenario.branches == ["主动触发"]
    assert candidate.scenario.exceptions == []
