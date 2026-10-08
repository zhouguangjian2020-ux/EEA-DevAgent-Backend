import asyncio
from contextlib import contextmanager
import json
import uuid

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.api.routes import agent as agent_route
from app.services import prd_generation

PROJECT = uuid.UUID("10d4c8a0-ff3a-56c3-abf6-18dc3b531e62")
FUNCTION = uuid.UUID("20d4c8a0-ff3a-56c3-abf6-18dc3b531e62")


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, *, project_name="测试项目1", function_description="记录行车视频并支持事件锁定"):
        self.project_name = project_name
        self.function_description = function_description
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement, params))
        if "FROM ee.business_functions" in statement:
            return Result({
                "id": FUNCTION,
                "project_id": PROJECT,
                "name": "行车记录仪DVR",
                "description": self.function_description,
            })
        return Result({
            "id": PROJECT,
            "name": self.project_name,
            "description": "车辆影像记录项目",
        })


@contextmanager
def database(connection):
    yield connection


def stream_body(*events):
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode("utf-8")


def stream_response(*events):
    return httpx.Response(
        200,
        content=stream_body(*events),
        headers={"content-type": "text/event-stream; charset=utf-8"},
    )


def token_event(content):
    return {"type": "token", "content": content}


def done_event(output="# PRD\n\n正文", **changes):
    return {
        "type": "done",
        "scene": "FROM_DEFINITION_CREATE_PRD",
        "output": output,
        "trace_id": "trace-prd",
        **changes,
    }


def mock_agent(monkeypatch, handler):
    monkeypatch.setattr(
        prd_generation,
        "agent_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_read_context_uses_visible_project_and_function_definition(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(
        prd_generation,
        "project_connection",
        lambda repeatable_read=False: database(connection),
    )

    result = prd_generation.read_prd_generation_context(
        "engineer01",
        project_id=PROJECT,
        function_id=FUNCTION,
    )

    assert result.project_name == "测试项目1"
    assert result.function_name == "行车记录仪DVR"
    assert result.function_description == "记录行车视频并支持事件锁定"
    assert connection.calls[0][1] == ("engineer01", PROJECT)
    assert connection.calls[1][1] == (FUNCTION, PROJECT)


def test_build_request_does_not_hardcode_project_name():
    context = prd_generation.PrdGenerationContext(
        project_id=PROJECT,
        project_name="客户项目A",
        project_description="项目简介",
        function_id=FUNCTION,
        function_name="行车记录仪DVR",
        function_description="记录行车视频",
    )

    body = prd_generation.build_prd_generation_request(
        context,
        version="V1.0",
        request_id="request-1",
    )

    assert body["scene"] == "FROM_DEFINITION_CREATE_PRD"
    assert body["inputs"]["project"] == "客户项目A"
    assert body["inputs"]["project"] != "通用"
    assert "适用项目：客户项目A" in body["message"]
    assert body["reference_data"]["function_definition"] == "记录行车视频"
    assert body["trace_id"] == "request-1"


def test_stream_generation_forwards_dynamic_context_and_sanitizes_events(monkeypatch):
    context = prd_generation.PrdGenerationContext(
        project_id=PROJECT,
        project_name="客户项目A",
        project_description="项目简介",
        function_id=FUNCTION,
        function_name="行车记录仪DVR",
        function_description="记录行车视频",
    )
    calls = []
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke/stream")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("unit-test-key"))
    monkeypatch.setattr(settings, "agent_timeout_seconds", 1)

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.headers["X-API-Key"] == "unit-test-key"
        assert body["scene"] == "FROM_DEFINITION_CREATE_PRD"
        assert body["inputs"]["project"] == "客户项目A"
        assert body["inputs"]["feature_name"] == "行车记录仪DVR"
        assert body["reference_data"]["function_definition"] == "记录行车视频"
        assert "适用项目：客户项目A" in body["message"]
        return stream_response(token_event("# PRD"), token_event("\\n正文"), done_event())

    mock_agent(monkeypatch, handler)
    events = asyncio.run(
        _collect(
            prd_generation.stream_prd_generation(
                context,
                version="V1.0",
                request_id="request-1",
            )
        )
    )

    assert [event["type"] for event in events] == ["token", "token", "done"]
    assert events[-1]["scene"] == "FROM_DEFINITION_CREATE_PRD"
    assert events[-1]["output"] == "# PRD\n\n正文"
    assert events[-1]["trace_id"] == "trace-prd"
    assert len(calls) == 1


async def _collect(iterator):
    return [event async for event in iterator]


def test_generation_route_returns_business_sse(monkeypatch):
    context = prd_generation.PrdGenerationContext(
        project_id=PROJECT,
        project_name="测试项目1",
        project_description=None,
        function_id=FUNCTION,
        function_name="行车记录仪DVR",
        function_description="记录行车视频",
    )
    monkeypatch.setattr(agent_route, "read_prd_generation_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(agent_route, "validate_prd_generation_configuration", lambda: None)
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke/stream")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("unit-test-key"))
    monkeypatch.setattr(settings, "agent_timeout_seconds", 1)

    def handler(request):
        body = json.loads(request.content)
        assert body["inputs"]["project"] == "测试项目1"
        return stream_response(token_event("# PRD"), done_event(output="# PRD"))

    mock_agent(monkeypatch, handler)
    with TestClient(app) as client:
        client.cookies.set("ts_session", create_session_token(settings.demo_username))
        with client.stream(
            "POST",
            "/v1/agent/prd/generate/stream",
            json={"project_id": str(PROJECT), "function_id": str(FUNCTION), "version": "V1.0"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["X-Accel-Buffering"] == "no"
            assert response.headers["Cache-Control"] == "no-store, no-transform"
            lines = [line for line in response.iter_lines() if line]

    events = [json.loads(line.removeprefix("data: ").strip()) for line in lines if line.startswith("data:")]
    assert [event["type"] for event in events] == ["token", "done"]
    assert events[-1]["scene"] == "FROM_DEFINITION_CREATE_PRD"
    assert events[-1]["request_id"]
    assert events[-1]["output"] == "# PRD"
