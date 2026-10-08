import asyncio
import json
import uuid

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.services import uc_generation
from app.services.uc_generation import ConfirmedPrd, UcGenerationError, generate_uc_from_prd


PRD = ConfirmedPrd(
    project_id=uuid.UUID("10d4c8a0-ff3a-56c3-abf6-18dc3b531e62"),
    function_id=uuid.UUID("20d4c8a0-ff3a-56c3-abf6-18dc3b531e62"),
    prd_id=uuid.UUID("30d4c8a0-ff3a-56c3-abf6-18dc3b531e62"),
    revision_id=uuid.UUID("40d4c8a0-ff3a-56c3-abf6-18dc3b531e62"),
    markdown_body="# 已确认PRD\n## 目标\n记录事件",
)


def structured_result():
    return {
        "use_cases": [
            {
                "name": "记录事件",
                "description": "系统记录事件。",
                "actors": "驾驶员；系统",
                "preconditions": "设备已上电。",
                "scenario": {
                    "main_flow": "1. 触发。\n2. 记录。",
                    "branches": ["主动触发", "自动触发", "重复事件", "<待确认>"],
                    "exceptions": ["车辆状态异常", "机械异常", "环境异常", "误用", "电源异常"],
                },
                "postconditions": "事件可查询。",
                "extensions": "<待确认>",
                "hmi": "显示结果。",
                "regulations": "<待确认>",
                "performance": "<待标定>",
                "nonfunctional": "可追溯。",
            }
        ]
    }


def mock_agent(monkeypatch, handler):
    monkeypatch.setattr(
        uc_generation,
        "agent_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_structured_generation_uses_buffered_invoke_and_reference_data(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke/stream")
    monkeypatch.setattr(settings, "agent_generation_invoke_url", "")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("unit-test-key"))
    monkeypatch.setattr(settings, "agent_callback_key", SecretStr("unit-test-callback-key"))
    monkeypatch.setattr(settings, "agent_timeout_seconds", 1)

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url == "http://agent.test/api/v1/agent/invoke"
        assert request.headers["X-API-Key"] == "unit-test-key"
        assert body["scene"] == "FROM_PRD_CREATE_UC"
        assert body["response_format"] == "json"
        assert body["reference_data"]["prd_markdown"] == PRD.markdown_body
        assert body["reference_data"]["uc_template"]["template_id"] == "uc-default"
        assert body["reference_data"]["uc_template"]["auth_header"] == "X-Agent-Internal-Key"
        assert PRD.markdown_body not in body["message"]
        assert body["idempotency_key"] == "50d4c8a0-ff3a-56c3-abf6-18dc3b531e62"
        return httpx.Response(
            200,
            json={
                "scene": "FROM_PRD_CREATE_UC",
                "output": None,
                "structured": structured_result(),
                "trace_id": "trace-uc",
            },
        )

    mock_agent(monkeypatch, handler)
    result = asyncio.run(
        generate_uc_from_prd(
            PRD,
            client_request_id=uuid.UUID("50d4c8a0-ff3a-56c3-abf6-18dc3b531e62"),
            request_id="backend-request",
        )
    )
    assert len(calls) == 1
    assert result.trace_id == "trace-uc"
    assert result.candidates[0].name == "记录事件"
    assert result.candidates[0].scenario.branches == ["主动触发", "自动触发", "重复事件", "<待确认>"]
    assert result.candidates[0].ordinal == 1
    assert result.candidates[0].candidate_id


def test_structured_generation_rejects_markdown_as_formal_uc_result(monkeypatch):
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke")
    monkeypatch.setattr(settings, "agent_generation_invoke_url", "")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("unit-test-key"))
    monkeypatch.setattr(settings, "agent_callback_key", SecretStr("unit-test-callback-key"))

    mock_agent(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "scene": "FROM_PRD_CREATE_UC",
                "output": "# 这是 Markdown，不是结构化 UC",
                "structured": None,
            },
        ),
    )
    with pytest.raises(UcGenerationError) as error:
        asyncio.run(
            generate_uc_from_prd(
                PRD,
                client_request_id=uuid.uuid4(),
                request_id="backend-request",
            )
        )
    assert error.value.code == "agent_invalid_response"


def test_generation_endpoint_configuration_overrides_derived_url(monkeypatch):
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke/stream")
    monkeypatch.setattr(settings, "agent_generation_invoke_url", "http://structured.test/api/v1/agent/invoke")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("unit-test-key"))
    assert uc_generation.validate_generation_agent_configuration() == "http://structured.test/api/v1/agent/invoke"
