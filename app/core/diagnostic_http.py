"""Observe bytes already consumed/sent. Never drain or replace a business stream."""
import asyncio
import codecs
import json
import secrets
import uuid

import httpx

from app.core.config import settings
from app.core.diagnostics import Capture, alias_trace, callback_capture, create_run, current_capture, emit, finish, warning
from app.core.security import decode_session_token

PATHS = {"/v1/agent/generations", "/v1/agent/uc-template", "/v1/agent/uc-diagram", "/v1/agent/prd/expand/stream", "/v1/agent/prd/generate/stream"}
PROJECT_PATHS = {"/v1/agent/generations", "/v1/agent/uc-diagram", "/v1/agent/prd/generate/stream"}
LIMIT = 1_500_000


def key_valid(value: str) -> bool:
    expected = settings.diagnostics_key.get_secret_value()
    return settings.diagnostics_enabled and len(expected) >= 32 and secrets.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))


def cookie_subject(headers: dict) -> str | None:
    from http.cookies import SimpleCookie
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("cookie", ""))
        return decode_session_token(cookie["ts_session"].value) if "ts_session" in cookie else None
    except Exception:
        return None


class WireRecorder:
    def __init__(self, channel, span_id, content_type="", capture=None, limit=LIMIT):
        self.channel, self.span_id, self.content_type = channel, span_id, content_type.lower()
        self.capture = capture or current_capture.get()
        self.limit = limit
        self.buffer = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.truncated = False
        self.finalized = False
        self.seen_bytes = 0
        self.streaming = "text/event-stream" in self.content_type or "ndjson" in self.content_type

    def _event(self, text):
        if not text.strip():
            return
        data = "\n".join(line[5:].lstrip(" ") for line in text.splitlines() if line.startswith("data:")) if "event-stream" in self.content_type else text
        try:
            payload = json.loads(data) if data else {"transport_metadata": text}
        except ValueError:
            payload = {"unparsed": data}
        # Store parsed application payload, not an unsanitized duplicate raw string.
        emit("stream.event", self.channel, capture=self.capture, span_id=self.span_id, event=payload, framing="SSE" if "event-stream" in self.content_type else "NDJSON")

    def feed(self, data: bytes):
        try:
            self.seen_bytes += len(data)
            if self.truncated:
                return
            self.buffer += self.decoder.decode(data)
            if len(self.buffer.encode("utf-8")) > self.limit:
                self.truncated = True
                self.buffer = ""
                warning("body_or_stream_frame_exceeded_capture_limit", self.capture)
                return
            if self.streaming:
                self.buffer = self.buffer.replace("\r\n", "\n")
                separator = "\n\n" if "event-stream" in self.content_type else "\n"
                while separator in self.buffer:
                    text, self.buffer = self.buffer.split(separator, 1)
                    self._event(text)
        except Exception:
            warning("wire_observer_failed", self.capture)

    def close(self, termination="eof"):
        if self.finalized:
            return
        self.finalized = True
        try:
            self.buffer += self.decoder.decode(b"", final=True)
            if self.streaming:
                if self.buffer.strip():
                    if "ndjson" in self.content_type:
                        self._event(self.buffer)
                    else:
                        warning("incomplete_sse_frame_at_close", self.capture)
            elif not self.truncated and (self.seen_bytes or termination == "eof"):
                try:
                    body = json.loads(self.buffer)
                except ValueError:
                    body = self.buffer
                emit("http.response.body", self.channel, capture=self.capture, span_id=self.span_id, body=body)
            elif not self.seen_bytes and termination != "eof":
                warning("upstream_body_not_consumed_by_business_code", self.capture)
            emit("http.stream.closed", self.channel, capture=self.capture, span_id=self.span_id, termination=termination, truncated=self.truncated)
        except Exception:
            warning("wire_observer_close_failed", self.capture)


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, original, recorder):
        self.original, self.recorder = original, recorder

    async def __aiter__(self):
        try:
            async for chunk in self.original:
                self.recorder.feed(chunk)
                yield chunk
            self.recorder.close()
        except BaseException:
            self.recorder.close("interrupted")
            raise

    async def aclose(self):
        try:
            await self.original.aclose()
        finally:
            self.recorder.close("closed_by_consumer")


class ObservedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner=None):
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        span = uuid.uuid4().hex
        try:
            body = json.loads(request.content)
        except (ValueError, httpx.RequestNotRead):
            body = "[non-JSON request body not observed]"
        if isinstance(body, dict):
            alias_trace(str(body.get("trace_id", "")))
        emit("http.request", "backend.agent", span_id=span, method=request.method, path=request.url.path, headers=dict(request.headers), body=body)
        try:
            response = await self.inner.handle_async_request(request)
        except Exception as error:
            emit("http.error", "backend.agent", span_id=span, error_type=type(error).__name__)
            raise
        emit("http.response.headers", "backend.agent", span_id=span, status=response.status_code, headers=dict(response.headers))
        if response.is_stream_consumed:
            recorder = WireRecorder("backend.agent", span, response.headers.get("content-type", ""), limit=100_000 if response.status_code != 200 else LIMIT)
            recorder.feed(response.content)
            recorder.close()
            return response
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding not in {"", "identity"}:
            warning("compressed_upstream_body_not_observed:" + encoding)
        else:
            response.stream = ObservedStream(response.stream, WireRecorder("backend.agent", span, response.headers.get("content-type", ""), limit=100_000 if response.status_code != 200 else LIMIT))
        return response

    async def aclose(self):
        await self.inner.aclose()


async def capture_rejected_response(response: httpx.Response) -> None:
    """Only diagnostic requests: bounded read of otherwise discarded error body.

    Adds at most 200ms waiting; never changes the adapter's original error mapping.
    Successful/streaming responses are not pre-read. Cancellation still propagates.
    """
    cap = current_capture.get()
    if not cap or not cap.enabled or response.status_code == 200:
        return
    size = 0
    try:
        async with asyncio.timeout(.2):
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 100_000:
                    warning("upstream_error_body_capture_limit")
                    break
    except Exception:
        warning("upstream_error_body_capture_failed_or_timeout")


def workflow_project(path):
    parts=path.strip('/').split('/')
    if len(parts)<4 or parts[:2]!=['v1','projects']:return None
    try:
        project=str(uuid.UUID(parts[2]))
        if len(parts)==6 and parts[3]=='functions' and parts[5] in {'fr-generations','allocation-generations'}:
            uuid.UUID(parts[4]);return project
        if len(parts)==4 and parts[3]=='conversations':return project
        if len(parts)==7 and parts[3]=='conversations' and parts[5:] == ['messages','stream']:
            uuid.UUID(parts[4]);return project
    except ValueError:return None
    return None

class DiagnosticsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        workflow = workflow_project(scope.get('path',''))
        if scope['type'] != 'http' or (scope['path'] not in PATHS and workflow is None) or not settings.diagnostics_enabled:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        cap, root = None, False
        project = headers.get("x-ts-lab-project-id", "")
        if key_valid(headers.get("x-ts-lab-key", "")):
            owner = cookie_subject(headers)
            try:
                run_id = str(uuid.UUID(headers.get("x-ts-lab-run-id", "")))
            except ValueError:
                run_id = ""
            project_ok = scope["path"] not in PROJECT_PATHS or project in settings.diagnostics_project_ids
            if workflow is not None:project_ok=project==workflow and project in settings.diagnostics_project_ids
            if owner and run_id and project_ok and create_run(run_id, owner):
                cap, root = Capture(run_id, uuid.uuid4().hex), True
        elif scope["path"] == "/v1/agent/uc-template":
            expected = settings.agent_callback_key.get_secret_value()
            if expected and secrets.compare_digest(headers.get("x-agent-internal-key", "").encode("utf-8"), expected.encode("utf-8")):
                cap = callback_capture(headers.get("x-request-id", ""))
        if not cap:
            return await self.app(scope, receive, send)
        token = current_capture.set(cap)
        channel = "browser.backend" if root else "agent.backend.callback"
        request_data = bytearray()
        recorder = None
        ended = False
        if scope["method"] == "GET":
            emit("http.request", channel, method="GET", path=scope["path"], headers=headers, query=scope.get("query_string", b"").decode("utf-8", "replace"))

        async def observed_receive():
            message = await receive()
            if message["type"] == "http.request":
                if len(request_data) <= 65536:
                    request_data.extend(message.get("body", b""))
                if not message.get("more_body", False) and scope["method"] != "GET":
                    try:
                        body = json.loads(request_data) if len(request_data) <= 65536 else None
                    except ValueError:
                        body = None
                    if scope["path"] in PROJECT_PATHS and (not isinstance(body, dict) or body.get("project_id") != project):
                        warning("project_body_mismatch_capture_disabled")
                        cap.enabled = False
                    elif body is None:
                        warning("request_not_captured")
                    else:
                        emit("http.request", channel, method=scope["method"], path=scope["path"], headers=headers, body=body)
            return message

        async def observed_send(message):
            nonlocal recorder, ended
            if message["type"] == "http.response.start":
                response_headers = {k.decode("latin1"): v.decode("latin1") for k, v in message.get("headers", [])}
                emit("http.response.headers", channel, status=message["status"], headers=response_headers)
                recorder = WireRecorder(channel, cap.span_id, response_headers.get("content-type", ""))
                message = {**message, "headers": [*message.get("headers", []), (b"x-ts-lab-capture", b"enabled" if cap.enabled else b"incomplete")]}
            elif message["type"] == "http.response.body" and recorder:
                recorder.feed(message.get("body", b""))
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                ended = True
                if recorder:
                    recorder.close()

        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            if recorder:
                recorder.close("eof" if ended else "interrupted")
            if root:
                finish(cap, "eof" if ended else "interrupted")
            current_capture.reset(token)
