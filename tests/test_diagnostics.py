import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.core import diagnostics as diag
from app.core.diagnostic_http import ObservedTransport, WireRecorder
from app.core.security import create_session_token
from app.main import app
from app.api.routes import agent as route
from app.services.uc_generation import ConfirmedPrd

PROJECT = "10d4c8a0-ff3a-56c3-abf6-18dc3b531e62"
KEY = "diagnostic-test-key-not-real-0123456789"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "diagnostics_enabled", True)
    monkeypatch.setattr(settings, "diagnostics_key", SecretStr(KEY))
    monkeypatch.setattr(settings, "diagnostics_project_ids", [PROJECT])
    monkeypatch.setattr(settings, "agent_callback_key", SecretStr("callback-test-secret"))
    with diag._lock:
        diag._runs.clear()
        diag._aliases.clear()
    yield
    with diag._lock:
        diag._runs.clear()
        diag._aliases.clear()


def client():
    c = TestClient(app)
    c.cookies.set("ts_session", create_session_token(settings.demo_username))
    return c


def headers(run, **extra):
    return {"X-TS-Lab-Key": KEY, "X-TS-Lab-Run-ID": run, **extra}


def test_disabled_does_not_capture_or_expose(monkeypatch):
    monkeypatch.setattr(settings, "diagnostics_enabled", False)
    c = client()
    r = c.get("/v1/agent/uc-template", params={"scene": "FROM_PRD_CREATE_UC"}, headers=headers(str(uuid4()), **{"X-Agent-Internal-Key": "bad"}))
    assert "x-ts-lab-capture" not in r.headers
    assert c.get("/v1/agent/diagnostics/runs/unknown", headers={"X-TS-Lab-Key": KEY}).status_code == 404


def test_template_capture_secrets_and_owner_isolation(enabled):
    run = str(uuid4())
    c = client()
    r = c.get("/v1/agent/uc-template?scene=FROM_PRD_CREATE_UC", headers=headers(run, **{"X-Agent-Internal-Key": "callback-test-secret"}))
    assert r.status_code == 200
    snapshot = c.get("/v1/agent/diagnostics/runs/" + run, headers={"X-TS-Lab-Key": KEY}).json()
    assert snapshot["finished"] and snapshot["capture_complete"]
    serialized = json.dumps(snapshot)
    assert KEY not in serialized and "callback-test-secret" not in serialized
    assert "Cookie" not in serialized or "[REDACTED]" in serialized
    assert len(snapshot["events"]) >= 3
    c.cookies.set("ts_session", create_session_token("another-account"))
    assert c.get("/v1/agent/diagnostics/runs/" + run, headers={"X-TS-Lab-Key": KEY}).status_code == 404


def test_invalid_key_and_no_session_no_capture(enabled):
    c = TestClient(app)
    r = c.get("/v1/agent/uc-template", headers=headers(str(uuid4()), **{"X-Agent-Internal-Key": "callback-test-secret"}))
    assert "x-ts-lab-capture" not in r.headers
    assert not diag._runs


def test_project_not_allowlisted_is_not_captured(enabled):
    c = client()
    r = c.post("/v1/agent/uc-diagram", json={}, headers=headers(str(uuid4()), **{"X-TS-Lab-Project-ID": "other"}))
    assert r.status_code == 422
    assert not diag._runs


def test_project_body_mismatch_disables_capture(enabled):
    run = str(uuid4())
    c = client()
    c.post("/v1/agent/uc-diagram", json={"project_id": "other-project", "sensitive": "must-not-record"}, headers=headers(run, **{"X-TS-Lab-Project-ID": PROJECT}))
    snapshot = diag.snapshot(run, settings.demo_username)
    assert snapshot["dropped"] > 0
    assert "must-not-record" not in json.dumps(snapshot)


def test_cursor_does_not_consume_results_or_repeat_sql(enabled):
    run = str(uuid4())
    diag.create_run(run, "owner")
    token = diag.current_capture.set(diag.Capture(run, "span"))
    class Cursor:
        rowcount = 1
        reads = 0
        def fetchone(self):
            self.reads += 1
            return {"markdown_body": "正文"}
    class Connection:
        executes = 0
        cursor = Cursor()
        def execute(self, *args, **kwargs):
            self.executes += 1
            return self.cursor
    try:
        db = Connection()
        cursor = diag.observe_connection(db).execute("SELECT markdown_body WHERE id=%s", ("uuid",))
        assert db.cursor.reads == 0
        assert cursor.fetchone() == {"markdown_body": "正文"}
        assert db.executes == db.cursor.reads == 1
    finally:
        diag.current_capture.reset(token)
    assert [e["kind"] for e in diag.snapshot(run, "owner")["events"]] == ["sql.execute", "sql.executed", "sql.result"]


def test_stream_fragment_split_and_no_preconsumption(enabled):
    run = str(uuid4())
    diag.create_run(run, "owner")
    token = diag.current_capture.set(diag.Capture(run, "span"))
    chunks = ['data: {"type":"token","content":"中文"}\n\n'.encode(), b'data: {"type":"done","output":"ok"}\n\n']
    consumed = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                consumed.append(chunk)
                yield chunk
    async def run_call():
        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        async with httpx.AsyncClient(transport=ObservedTransport(httpx.MockTransport(handler))) as c:
            async with c.stream("POST", "http://agent.test/invoke", json={"trace_id": "trace1"}) as r:
                assert not consumed
                assert await r.aread() == b"".join(chunks)
    try:
        asyncio.run(run_call())
    finally:
        diag.current_capture.reset(token)
    events = diag.snapshot(run, "owner")["events"]
    assert [e["event"]["type"] for e in events if e["kind"] == "stream.event"] == ["token", "done"]
    assert diag.callback_capture("trace1").run_id == run


def test_wire_parser_multibyte_and_multiline(enabled):
    run = str(uuid4())
    diag.create_run(run, "owner")
    cap = diag.Capture(run, "span")
    recorder = WireRecorder("backend.agent", "span", "text/event-stream", cap)
    wire = 'data: {"type":"token",\r\ndata: "content":"中文"}\r\n\r\n'.encode()
    for byte in wire:
        recorder.feed(bytes([byte]))
    recorder.close()
    assert diag.snapshot(run, "owner")["events"][0]["event"]["content"] == "中文"


def test_limits_mark_incomplete_without_throwing(enabled, monkeypatch):
    monkeypatch.setattr(diag, "MAX_EVENT_BYTES", 40)
    run = str(uuid4())
    diag.create_run(run, "owner")
    cap = diag.Capture(run, "span")
    diag.emit("body", "test", capture=cap, content="a" * 100)
    diag.finish(cap, "eof")
    snapshot = diag.snapshot(run, "owner")
    assert snapshot["dropped"] == 1 and not snapshot["capture_complete"]


def test_unconsumed_body_not_faked_as_empty(enabled):
    run = str(uuid4())
    diag.create_run(run, "owner")
    cap = diag.Capture(run, "span")
    recorder = WireRecorder("backend.agent", "span", "application/json", cap)
    recorder.close("closed_by_consumer")
    snapshot = diag.snapshot(run, "owner")
    assert snapshot["dropped"] == 1
    assert not any(e["kind"] == "http.response.body" for e in snapshot["events"])


@pytest.mark.parametrize("value", [{"password": "never-log", "normal": "text"}, '{"api_key":"never-log"}', {"Cookie": "never-log", "Authorization": "Bearer value"}])
def test_redaction(value):
    assert "never-log" not in json.dumps(diag.scrub(value))


def test_serialization_failure_isolated_from_business(enabled, monkeypatch):
    run = str(uuid4())
    diag.create_run(run, "owner")
    cap = diag.Capture(run, "span")
    def broken(*args, **kwargs):
        raise RuntimeError("observer-only-fault")
    monkeypatch.setattr(diag, "scrub", broken)
    diag.emit("http.request", "backend.agent", capture=cap, body={"content": "ok"})
    assert diag.snapshot(run, "owner")["dropped"] == 1


def test_rejected_read_timeout_is_bounded(enabled):
    from app.core.diagnostic_http import capture_rejected_response
    import time
    run = str(uuid4())
    diag.create_run(run, "owner")
    token = diag.current_capture.set(diag.Capture(run, "span"))
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(5)
            yield b'{"error":"late"}'
    async def call():
        async with httpx.AsyncClient(transport=ObservedTransport(httpx.MockTransport(lambda r: httpx.Response(409, stream=Slow(), headers={"content-type": "application/json"})))) as c:
            async with c.stream("POST", "http://agent.test/invoke", json={}) as r:
                await capture_rejected_response(r)
                assert r.status_code == 409
    start = time.monotonic()
    try:
        asyncio.run(call())
    finally:
        diag.current_capture.reset(token)
    assert time.monotonic() - start < .7
    assert diag.snapshot(run, "owner")["dropped"] >= 1


def test_concurrent_run_contexts_do_not_mix(enabled):
    ids = [str(uuid4()), str(uuid4())]
    for run in ids:
        diag.create_run(run, "owner")
    async def worker(run):
        token = diag.current_capture.set(diag.Capture(run, run))
        try:
            await asyncio.sleep(.01)
            diag.emit("test", "test", body={"own": run})
        finally:
            diag.current_capture.reset(token)
    async def both():
        await asyncio.gather(*(worker(run) for run in ids))
    asyncio.run(both())
    for run in ids:
        assert diag.snapshot(run, "owner")["events"][0]["body"]["own"] == run
