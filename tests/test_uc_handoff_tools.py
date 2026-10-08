"""Offline-only tests for the portable engineer handoff probes."""
import importlib.util
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.services.uc_template import uc_template_response

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("uc_handoff_probe", ROOT / "tools" / "probe_uc_service.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_health_is_one_read_only_request_without_auth(capsys):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/health"
        assert "cookie" not in request.headers
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"status": "ok"})
    probe.execute("health", transport=httpx.MockTransport(handler),
                  prompt=lambda _: pytest.fail("Health must not request a secret"))
    assert "HEALTH_OK" in capsys.readouterr().out


def test_health_does_not_accept_arbitrary_200():
    with pytest.raises(probe.ProbeFailure, match="HEALTH_INVALID_STATUS"):
        probe.execute("health", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "not-ready"})))


def test_session_retains_httponly_cookie_without_printing_credentials(capsys):
    calls = []
    password = "test-only-login-password"
    cookie = "test-only-session-secret"
    def handler(request):
        calls.append(request.url.path)
        assert request.headers["origin"] == "http://192.168.64.3:7100"
        if request.url.path == "/v1/auth/login":
            assert request.method == "POST"
            assert json.loads(request.content) == {"username": "engineer01", "password": password}
            return httpx.Response(200, json={"username": "engineer01"},
                                  headers={"set-cookie": f"ts_session={cookie}; Path=/; HttpOnly; SameSite=Lax"})
        assert request.method == "GET"
        assert request.headers["cookie"] == "ts_session=" + cookie
        return httpx.Response(200, json={"username": "engineer01"})
    probe.execute("session", prompt=lambda _: password, transport=httpx.MockTransport(handler))
    out = capsys.readouterr().out
    assert calls == ["/v1/auth/login", "/v1/auth/me"]
    assert "SESSION_OK" in out and password not in out and cookie not in out


def test_template_uses_separate_callback_header_and_validates_12_and_11_fields(capsys):
    key = "test-only-callback-key"
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/agent/uc-template"
        assert request.url.params["scene"] == "FROM_PRD_CREATE_UC"
        assert request.headers["X-Agent-Internal-Key"] == key
        assert "x-api-key" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, json=uc_template_response().model_dump(mode="json"))
    probe.execute("template", prompt=lambda _: key, show_template=True,
                  transport=httpx.MockTransport(handler))
    out = capsys.readouterr().out
    assert "formal_fields=12 candidate_fields=11" in out and '"candidate_schema"' in out
    assert key not in out


@pytest.mark.parametrize("status", [301, 401, 403, 404, 409, 422, 500, 503])
def test_error_bodies_and_headers_are_not_dumped(status, capsys):
    secret = "test-only-secret-never-print"
    def handler(request):
        return httpx.Response(status, json={"detail": {
            "code": "agent_unauthorized", "message": secret, "request_id": "trace-test-123"}},
            headers={"set-cookie": secret, "location": "http://example.invalid"})
    with pytest.raises(probe.ProbeFailure):
        probe.execute("template", prompt=lambda _: secret, transport=httpx.MockTransport(handler))
    out = capsys.readouterr().out
    assert "ERROR_CODE=agent_unauthorized" in out and secret not in out


def test_template_rejects_wrong_contract():
    with pytest.raises(probe.ProbeFailure, match="UC_TEMPLATE_CONTRACT_MISMATCH"):
        probe.execute("template", prompt=lambda _: "test-key", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"scene": "FROM_PRD_CREATE_UC", "fields": []})))


@pytest.mark.parametrize("name", ["uc-agent-generation.outbound.example.json", "uc-agent-diagram.outbound.example.json"])
def test_local_agent_uses_fixture_fresh_ids_and_x_api_key(name, capsys):
    fixture = ROOT / "tools" / "request_examples" / name
    original = json.loads(fixture.read_text(encoding="utf-8"))
    key = "test-only-local-agent-key"
    def handler(request):
        assert request.method == "POST"
        assert request.url == "http://127.0.0.1:8300/api/v1/agent/invoke"
        assert request.headers["X-API-Key"] == key
        assert "X-Agent-Internal-Key" not in request.headers and "cookie" not in request.headers
        data = json.loads(request.content)
        assert data["reference_data"] == original["reference_data"]
        assert UUID(data["idempotency_key"]).version == 4
        assert data["idempotency_key"] != original["idempotency_key"]
        assert data["trace_id"].startswith("uc-handoff-")
        return httpx.Response(200, json={"scene": data["scene"], "structured": {}})
    probe.execute("agent-local", agent_url="http://127.0.0.1:8300/api/v1/agent/invoke",
                  request_file=str(fixture), prompt=lambda _: key, transport=httpx.MockTransport(handler))
    out = capsys.readouterr().out
    assert "LOCAL_AGENT_HTTP_STATUS=200" in out and key not in out
    assert json.loads(fixture.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("url", ["http://192.168.64.3:8300/api/v1/agent/invoke", "https://example.org/invoke", "file:///tmp/example", "http://user:pass@localhost/invoke"])
def test_local_probe_cannot_target_company_or_credential_bearing_urls(url):
    with pytest.raises(probe.ProbeFailure, match="INVALID_LOCAL_AGENT_URL"):
        probe.execute("agent-local", agent_url=url, request_file="not-read.json",
                      prompt=lambda _: pytest.fail("Must reject before requesting a key"),
                      transport=httpx.MockTransport(lambda _: pytest.fail("No HTTP request allowed")))


def test_invalid_json_is_not_reported_as_success():
    with pytest.raises(probe.ProbeFailure, match="HEALTH_INVALID_JSON_OBJECT"):
        probe.execute("health", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="not JSON")))


def test_empty_secret_is_not_sent():
    with pytest.raises(probe.ProbeFailure, match="EMPTY_LOGIN_PASSWORD"):
        probe.execute("session", prompt=lambda _: "", transport=httpx.MockTransport(
            lambda _: pytest.fail("No request allowed")))
