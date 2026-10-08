"""S1 contract/regression tests; no company endpoints or databases."""
import json
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.schemas.prd_creation import FeatureDefinition, FeaturePlanRequest
from app.services import prd_creation_plan as service, workflow_agent
from app.services.projects import ProjectReadError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def envelope(**patch):
    return {"scene": service.SCENE, "plan": {"feature_name": "自动循环录像"},
            "output": "车辆上电后自动开始录像；空间不足时覆盖最早的未保护视频。", **patch}


def function(name="自动循环录像", **patch):
    return {"id": uuid4(), "name": name, "description": "", "prd_artifact_id": None,
            "can_create_prd": True, **patch}


def test_plan_name_and_text_description_are_only_trusted_sources():
    response = envelope(plan={"feature_name": "  DVR   循环录像 ", "feature_description": "not contracted"},
                        structured={"name": "do not use"})
    result = service.parse_definition(response)
    assert result.name == "DVR 循环录像"
    assert result.description == response["output"]
    assert result.description_source == "agent_output"


@pytest.mark.parametrize("patch", [
    {"plan": None}, {"plan": []}, {"plan": "auto"}, {"plan": {}},
    {"plan": {"feature_name": None}}, {"plan": {"feature_name": 12}},
    {"plan": {"feature_name": "  "}}, {"plan": {"feature_name": "字" * 101}},
    {"plan": {"feature_name": "含\x00字符"}}, {"plan": {"feature_name": "隐\u200b藏"}},
    {"plan": {"feature": {"name": "不猜层级"}}},
    {"output": None}, {"output": []}, {"output": " "}, {"output": "字" * 2001},
    {"output": "含\x00字符"}, {"scene": "FROM_DEFINITION_CREATE_PRD"},
    {"error": {"message": "raw upstream details must not escape"}},
])
def test_invalid_plan_does_not_guess_fallbacks(patch):
    with pytest.raises(ProjectReadError) as caught:
        service.parse_definition(envelope(**patch))
    assert caught.value.status == 502 and caught.value.code == "agent_plan_invalid"
    assert "raw upstream" not in caught.value.message


def test_interrupted_plan_is_not_a_success():
    with pytest.raises(ProjectReadError) as caught:
        service.parse_definition(envelope(interrupt={"reason": "approval"}))
    assert caught.value.code == "agent_plan_interrupted"


@pytest.mark.parametrize("message", ["", "  ", "字" * 4001, "a\x00b", None, 12])
def test_request_rejects_empty_oversized_and_wrong_types(message):
    with pytest.raises(ValidationError):
        FeaturePlanRequest(client_request_id=uuid4(), message=message)


def test_request_does_not_accept_browser_plan_or_foreign_function_id():
    with pytest.raises(ValidationError):
        FeaturePlanRequest(client_request_id=uuid4(), message="需求", function_id=uuid4())
    with pytest.raises(ValidationError):
        FeaturePlanRequest(client_request_id=uuid4(), message="需求", plan={"feature_name": "伪造"})


def test_comparison_normalizes_whitespace_and_case_not_punctuation_or_numbers():
    assert service.normalize_name("  DVR  Test\tA ") == "dvr test a"
    assert service.normalize_name("DVR-1") != service.normalize_name("DVR1")
    assert service.normalize_name("DVR1") != service.normalize_name("DVR2")
    assert service.normalize_name("Straße") == service.normalize_name("STRASSE")


def test_exact_rank_existing_prd_and_permission_are_not_auto_merge():
    definition = FeatureDefinition(name="DVR 循环录像", description="车辆录像")
    exact = function("  dvr  循环录像 ", prd_artifact_id=uuid4())
    similar = function("DVR 循环录像增强", can_create_prd=False)
    result = service.match_candidates(definition, [similar, function("门锁"), exact])
    assert [x.match for x in result] == ["exact_name", "similar_name"]
    assert result[0].blocked_reason == "prd_exists" and not result[0].can_reuse_for_prd
    assert result[1].blocked_reason == "prd_create_forbidden"
    assert result[0].name == exact["name"]  # no write/normalization of stored names
    assert exact["prd_artifact_id"] is not None


def test_qualified_similar_name_is_a_suggestion_not_an_exact_collision():
    definition = FeatureDefinition(name="自动循环录像", description="循环记录")
    results = service.match_candidates(definition, [function("行车记录仪自动循环录制")])
    assert len(results) == 1 and results[0].match == "similar_name"
    assert results[0].can_reuse_for_prd


def test_description_similarity_is_conservative_and_deterministic():
    description = "车辆上电后系统自动开始分段循环录像，存储不足时覆盖最早的未保护视频。"
    definition = FeatureDefinition(name="自动循环录制", description=description)
    related = function("车辆视频记录", description=description)
    result = service.match_candidates(definition, [related, function("门锁", description="无")])
    assert len(result) == 1 and result[0].match == "similar_description"
    assert result[0].can_reuse_for_prd
    assert service.match_candidates(FeatureDefinition(name="门锁", description="通用功能"),
                                    [function("空调", description="通用功能")]) == []


@pytest.fixture
def protocol(monkeypatch):
    monkeypatch.setattr(settings, "agent_generation_invoke_url", "http://127.0.0.1:8419/api/v1/agent/invoke")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("isolated-plan-test-key"))
    def install(handler):
        monkeypatch.setattr(workflow_agent, "agent_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return install


@pytest.mark.anyio
async def test_non_streaming_text_contract_keeps_plan_and_no_database_spans_agent(monkeypatch, protocol):
    reads = []
    open_snapshot = False
    @contextmanager
    def snapshot_scope():
        nonlocal open_snapshot
        assert not open_snapshot
        open_snapshot = True
        try:
            yield
        finally:
            open_snapshot = False
    existing = function()
    def read(subject, project):
        with snapshot_scope():
            reads.append(subject)
            # Simulate a concurrent function appearing while the Agent runs.
            return service.PlanningSnapshot(True, [] if len(reads) == 1 else [existing])
    monkeypatch.setattr(service, "read_planning_snapshot", read)
    requests = []
    def handler(request):
        assert not open_snapshot
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/api/v1/agent/invoke"
        assert request.headers["X-API-Key"] == "isolated-plan-test-key"
        assert body["scene"] == service.SCENE and body["response_format"] == "text"
        assert set(body["reference_data"]) == {"project_id"}
        return httpx.Response(200, json=envelope())
    protocol(handler)
    payload = FeaturePlanRequest(client_request_id=uuid4(), message="用户原始需求")
    result = await service.plan_feature("engineer01", uuid4(), payload, "trace-plan")
    assert len(requests) == 1 and reads == ["engineer01", "engineer01"]
    assert result.status == "needs_selection" and result.exact_name_conflict
    assert result.candidates[0].function_id == existing["id"]
    assert result.phase == "plan_review" and requests[0]["trace_id"] == "trace-plan"


@pytest.mark.anyio
async def test_revoked_access_after_agent_is_not_returned_as_a_valid_plan(monkeypatch):
    reads = 0
    def read(*args):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise ProjectReadError(403, "prd_creation_forbidden", "已撤销")
        return service.PlanningSnapshot(True, [])
    async def invoke(body):
        return envelope()
    monkeypatch.setattr(service, "read_planning_snapshot", read)
    monkeypatch.setattr(workflow_agent, "invoke_envelope", invoke)
    with pytest.raises(ProjectReadError) as caught:
        await service.plan_feature("engineer01", uuid4(), FeaturePlanRequest(client_request_id=uuid4(), message="需求"), "trace")
    assert caught.value.status == 403 and reads == 2


@pytest.mark.anyio
async def test_idempotency_key_is_stable_and_scoped_not_a_durable_receipt(monkeypatch):
    monkeypatch.setattr(service, "read_planning_snapshot", lambda *a: service.PlanningSnapshot(True, []))
    calls = []
    async def invoke(body):
        calls.append(body)
        return envelope()
    monkeypatch.setattr(workflow_agent, "invoke_envelope", invoke)
    payload = FeaturePlanRequest(client_request_id=uuid4(), message="需求")
    project = uuid4()
    for subject, project_id in [("one", project), ("one", project), ("two", project), ("one", uuid4())]:
        result = await service.plan_feature(subject, project_id, payload, "trace")
        assert result.status == "ready_to_create" and not result.candidates
    assert calls[0]["idempotency_key"] == calls[1]["idempotency_key"]
    assert len({x["idempotency_key"] for x in calls}) == 3


@pytest.mark.anyio
async def test_candidate_limit_does_not_suppress_exact_conflict(monkeypatch):
    rows = [function("自动循环录像" + chr(0x4e00 + i)) for i in range(24)] + [function()]
    monkeypatch.setattr(service, "read_planning_snapshot", lambda *a: service.PlanningSnapshot(True, rows))
    async def invoke(body):
        return envelope()
    monkeypatch.setattr(workflow_agent, "invoke_envelope", invoke)
    result = await service.plan_feature("one", uuid4(), FeaturePlanRequest(client_request_id=uuid4(), message="需求"), "t")
    assert result.candidate_count == 25 and result.has_more
    assert len(result.candidates) == 20 and result.candidates[0].match == "exact_name"


@pytest.mark.anyio
@pytest.mark.parametrize("status,body,code", [
    (409, {}, "agent_upstream_rejected"), (200, {"scene": "wrong"}, "agent_invalid_response"),
    (200, [], "agent_invalid_response"),
])
async def test_envelope_rejects_upstream_errors_once(protocol, status, body, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)
    protocol(handler)
    with pytest.raises(ProjectReadError) as caught:
        await workflow_agent.invoke_envelope(workflow_agent.request_body("需求", {}, "x", service.SCENE, "text"))
    assert caught.value.code == code and len(calls) == 1


@pytest.mark.anyio
async def test_envelope_keeps_size_bound(protocol, monkeypatch):
    monkeypatch.setattr(workflow_agent, "MAX_BYTES", 64)
    protocol(lambda request: httpx.Response(200, content=b"x" * 128))
    with pytest.raises(ProjectReadError) as caught:
        await workflow_agent.invoke_envelope({"scene": service.SCENE})
    assert caught.value.code == "agent_invalid_response"


def test_http_auth_origin_and_payload_boundary(monkeypatch):
    path = "/v1/projects/" + str(uuid4()) + "/prd-creations/plan"
    body = {"client_request_id": str(uuid4()), "message": "需求"}
    headers = {"Origin": "http://127.0.0.1:3000", "X-Project-Request": "1"}
    monkeypatch.setattr(settings, "projects_write_origins", [headers["Origin"]])
    with TestClient(app) as client:
        assert client.post(path, json=body, headers=headers).status_code == 401
        client.cookies.set("ts_session", create_session_token("engineer01"))
        assert client.post(path, json=body).status_code == 403
        assert client.post(path, json={**body, "plan": {}}, headers=headers).status_code == 422
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 503 and response.json()["detail"]["code"] == "business_writes_disabled"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Request-ID"]
