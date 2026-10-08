import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.services import agent

PATH = "/v1/agent/prd/expand"
STREAM_PATH = "/v1/agent/prd/expand/stream"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke/stream")
    monkeypatch.setattr(settings, "agent_api_key", SecretStr("unit-test-key"))
    monkeypatch.setattr(settings, "agent_timeout_seconds", 1)
    with TestClient(app) as result:
        result.cookies.set("ts_session", create_session_token(settings.demo_username))
        yield result


def mock_upstream(monkeypatch, handler):
    monkeypatch.setattr(agent, "agent_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def stream_body(*events):
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode("utf-8")


def token_event(content):
    return {"type": "token", "content": content}


def done_event(output="扩写后的正文", **changes):
    return {"type": "done", "scene": "PROMPT_EXPAND", "output": output, "trace_id": "trace-test", **changes}


def stream_response(*events, content_type="text/event-stream; charset=utf-8"):
    return httpx.Response(200, content=stream_body(*events), headers={"content-type": content_type})


def test_auth_required_and_no_upstream_call(client, monkeypatch):
    def forbidden(_):
        pytest.fail("Unauthenticated request reached Agent")

    mock_upstream(monkeypatch, forbidden)
    client.cookies.clear()
    assert client.post(PATH, json={"feature_name": "AEB"}).status_code == 401


def test_success_forwards_stream_scene_and_message(client, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url == "http://agent.test/api/v1/agent/invoke/stream"
        assert request.headers["X-API-Key"] == "unit-test-key"
        assert request.headers["Accept"] == "text/event-stream, application/json"
        assert json.loads(request.content) == {"message": "AEB", "scene": "PROMPT_EXPAND"}
        return stream_response(token_event("扩写后的"), token_event("正文"), done_event())

    mock_upstream(monkeypatch, handler)
    response = client.post(PATH, json={"feature_name": "  AEB  "})
    assert response.status_code == 200
    assert response.json()["text"] == "扩写后的正文"
    assert response.json()["response_mode"] == "buffered"
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "unit-test-key" not in response.text
    assert len(calls) == 1


def test_buffered_json_upstream_is_supported(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke")

    def handler(request):
        assert request.url == "http://agent.test/api/v1/agent/invoke"
        assert json.loads(request.content) == {"message": "AEB", "scene": "PROMPT_EXPAND"}
        return httpx.Response(200, json={"scene": "PROMPT_EXPAND", "output": "JSON 扩写", "trace_id": "trace-json"})

    mock_upstream(monkeypatch, handler)
    response = client.post(PATH, json={"feature_name": "AEB"})
    assert response.status_code == 200
    assert response.json()["text"] == "JSON 扩写"
    assert response.json()["trace_id"] == "trace-json"


def test_sse_data_prefix_and_token_fallback(client, monkeypatch):
    def handler(_):
        body = b'data: {"type":"token","content":"AEB"}\n\ndata: {"type":"done","scene":"PROMPT_EXPAND","output":""}\n'
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    mock_upstream(monkeypatch, handler)
    response = client.post(PATH, json={"feature_name": "AEB"})
    assert response.status_code == 200
    assert response.json()["text"] == "AEB"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"feature_name": "   "},
        {"feature_name": "a" * 4001},
        {"feature_name": 3},
        {"feature_name": "AEB", "scene": "OTHER"},
        {"feature_name": "AEB", "url": "http://other.test"},
    ],
)
def test_invalid_input_never_reaches_upstream(client, monkeypatch, body):
    mock_upstream(monkeypatch, lambda _: pytest.fail("Invalid input reached Agent"))
    assert client.post(PATH, json=body).status_code == 422


def test_missing_private_configuration(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_api_key", SecretStr(""))
    response = client.post(PATH, json={"feature_name": "AEB"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "agent_not_configured"


@pytest.mark.parametrize("upstream_status,expected", [(401, 502), (404, 502), (429, 503), (500, 502), (302, 502)])
def test_upstream_errors_sanitized_no_retries(client, monkeypatch, upstream_status, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            upstream_status,
            json={"error": "unit-test-key private failure"},
            headers={"Location": "http://other.test"},
        )

    mock_upstream(monkeypatch, handler)
    response = client.post(PATH, json={"feature_name": "AEB"})
    assert response.status_code == expected
    assert "unit-test-key" not in response.text
    assert "private failure" not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        stream_response(done_event(output="")),
        stream_response(done_event(scene="OTHER")),
        stream_response(token_event("没有结束事件")),
        stream_response({"type": "error", "message": "private failure"}),
        httpx.Response(200, text="<html>error</html>"),
    ],
)
def test_incomplete_or_mismatched_stream_is_not_success(client, monkeypatch, response):
    mock_upstream(monkeypatch, lambda _: response)
    assert client.post(PATH, json={"feature_name": "AEB"}).status_code == 502


def test_unreachable_and_timeout(client, monkeypatch):
    def unreachable(request):
        raise httpx.ConnectError("private connection detail", request=request)

    mock_upstream(monkeypatch, unreachable)
    response = client.post(PATH, json={"feature_name": "AEB"})
    assert response.status_code == 502
    assert "private connection detail" not in response.text

    def timeout(request):
        raise httpx.ReadTimeout("private timeout", request=request)

    mock_upstream(monkeypatch, timeout)
    assert client.post(PATH, json={"feature_name": "AEB"}).status_code == 504


def test_total_deadline(client, monkeypatch):
    async def slow(_):
        await asyncio.sleep(0.1)
        return stream_response(token_event("太慢"), done_event(output="太慢"))

    monkeypatch.setattr(settings, "agent_timeout_seconds", 0.01)
    mock_upstream(monkeypatch, slow)
    assert client.post(PATH, json={"feature_name": "AEB"}).status_code == 504


def test_stream_route_forwards_sanitized_sse(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_invoke_url", "http://agent.test/api/v1/agent/invoke/stream")
    calls = []

    def handler(request):
        calls.append(request)
        return stream_response(token_event("扩写后的"), token_event("正文"), done_event())

    mock_upstream(monkeypatch, handler)
    with client.stream("POST", STREAM_PATH, json={"feature_name": "AEB"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["X-Request-ID"]
        assert response.headers["X-Accel-Buffering"] == "no"
        lines = [line for line in response.iter_lines() if line]

    events = [json.loads(line.removeprefix("data: ").strip()) for line in lines if line.startswith("data:")]
    assert [event["type"] for event in events] == ["token", "token", "done"]
    assert events[-1]["output"] == "扩写后的正文"
    assert events[-1]["scene"] == "PROMPT_EXPAND"
    assert "unit-test-key" not in "".join(lines)
    assert len(calls) == 1


def test_stream_route_sends_sanitized_error_event(client, monkeypatch):
    def handler(_):
        return httpx.Response(429, json={"error": "unit-test-key private failure"})

    mock_upstream(monkeypatch, handler)
    with client.stream("POST", STREAM_PATH, json={"feature_name": "AEB"}) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]

    events = [json.loads(line.removeprefix("data: ").strip()) for line in lines if line.startswith("data:")]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["code"] == "agent_busy"
    assert events[0]["message"] == "Agent 当前繁忙，请稍后重试。"
    assert events[0]["request_id"]
    assert "unit-test-key" not in "".join(lines)


def test_stream_route_requires_auth(client, monkeypatch):
    mock_upstream(monkeypatch, lambda _: pytest.fail("Unauthenticated request reached Agent"))
    client.cookies.clear()
    assert client.post(STREAM_PATH, json={"feature_name": "AEB"}).status_code == 401


def test_openapi_contains_agent_operations(client):
    paths = client.get("/openapi.json").json()["paths"]
    prd_generation_path = "/v1/agent/prd/generate/stream"
    generation_path = "/v1/agent/generations"
    template_path = "/v1/agent/uc-template"
    diagram_path = "/v1/agent/uc-diagram"
    assert [path for path in paths if path.startswith("/v1/agent")] == [template_path, PATH, STREAM_PATH, prd_generation_path, generation_path, diagram_path]
    assert list(paths[template_path]) == ["get"]
    assert list(paths[PATH]) == ["post"]
    assert list(paths[STREAM_PATH]) == ["post"]
    assert list(paths[prd_generation_path]) == ["post"]
    assert list(paths[generation_path]) == ["post"]
    assert list(paths[diagram_path]) == ["post"]
