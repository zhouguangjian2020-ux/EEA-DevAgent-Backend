"""Single-scene Agent adapter for the server-side PRD expansion bridge."""
from collections.abc import AsyncIterator
import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.schemas.agent import PrdExpandResponse

logger = logging.getLogger(__name__)
AGENT_SCENE = "PROMPT_EXPAND"
MAX_RESPONSE_BYTES = 1_000_000
MAX_OUTPUT_CHARACTERS = 20_000


class AgentExpandError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(code)


def agent_client() -> httpx.AsyncClient:
    # Do not leak the key through proxy environment variables or redirects.
    from app.core.diagnostics import current_capture
    from app.core.diagnostic_http import ObservedTransport
    capture = current_capture.get()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.agent_timeout_seconds, connect=5),
        follow_redirects=False,
        trust_env=False,
        transport=ObservedTransport() if capture and capture.enabled else None,
    )


def validate_agent_configuration() -> None:
    """Validate server-only Agent configuration without revealing its value."""
    try:
        target = urlsplit(settings.agent_invoke_url)
    except ValueError as error:
        raise AgentExpandError(503, "agent_not_configured", "PRD 扩写服务地址配置无效，请联系管理员。") from error
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username
        or target.password
        or target.query
        or target.fragment
        or not settings.agent_api_key.get_secret_value()
    ):
        raise AgentExpandError(503, "agent_not_configured", "PRD 扩写服务尚未配置，请联系管理员。")


def _decode_event_line(line: str) -> dict[str, Any]:
    """Decode one JSON line from the Agent stream.

    The deployed Agent emits JSON lines with a text/event-stream content type.
    Accepting an optional SSE ``data:`` prefix keeps the bridge compatible with
    standard SSE framing without exposing the upstream stream directly.
    """
    payload = line.strip()
    if payload.startswith("data:"):
        payload = payload[5:].lstrip()
    if not payload:
        raise ValueError("empty Agent event")
    event = json.loads(payload)
    if not isinstance(event, dict):
        raise ValueError("Agent event is not an object")
    return event


async def _iter_stream_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type and "application/x-ndjson" not in content_type:
        raise AgentExpandError(502, "agent_invalid_response", "Agent 返回格式异常，原输入已保留。")

    response_bytes = 0
    async for line in response.aiter_lines():
        response_bytes += len(line.encode("utf-8")) + 1
        if response_bytes > MAX_RESPONSE_BYTES:
            raise AgentExpandError(502, "agent_invalid_response", "Agent 返回内容过大，原输入已保留。")
        if not line.strip():
            continue
        try:
            yield _decode_event_line(line)
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise AgentExpandError(502, "agent_invalid_response", "Agent 返回格式异常，原输入已保留。") from error


def _safe_trace_id(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 200 else None


def _parse_json_response(raw: bytes) -> tuple[str, str | None]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AgentExpandError(502, "agent_invalid_response", "Agent 返回内容过大，原输入已保留。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AgentExpandError(502, "agent_invalid_response", "Agent 返回格式异常，原输入已保留。") from error
    if not isinstance(payload, dict) or payload.get("scene") != AGENT_SCENE:
        raise AgentExpandError(502, "agent_invalid_response", "Agent 返回场景不匹配，原输入已保留。")
    output = payload.get("output")
    if not isinstance(output, str) or not output.strip() or len(output) > MAX_OUTPUT_CHARACTERS:
        raise AgentExpandError(502, "agent_invalid_response", "Agent 未返回有效扩写文本，原输入已保留。")
    return output.strip(), _safe_trace_id(payload.get("trace_id"))


def _raise_upstream_status(response: httpx.Response, request_id: str) -> None:
    if response.status_code == 200:
        return
    logger.warning(
        "agent_expand_upstream_error request_id=%s status=%s",
        request_id,
        response.status_code,
    )
    if response.status_code == 429:
        raise AgentExpandError(503, "agent_busy", "Agent 当前繁忙，请稍后重试。")
    raise AgentExpandError(502, "agent_upstream_error", "Agent 扩写失败，请稍后重试或联系管理员。")


async def stream_prd_expansion(feature_name: str, request_id: str) -> AsyncIterator[dict[str, Any]]:
    """Yield sanitized browser-safe events from either Agent response mode.

    ``/invoke/stream`` produces token events. ``/invoke`` produces one JSON
    object, so it is exposed to the browser as one token followed by ``done``.
    This fallback preserves compatibility but does not create token-level
    streaming when the configured upstream is the buffered endpoint.
    """
    validate_agent_configuration()
    token_parts: list[str] = []
    try:
        # Covers the whole upstream call, not just inactivity between chunks.
        async with asyncio.timeout(settings.agent_timeout_seconds):
            async with agent_client() as client:
                async with client.stream(
                    "POST",
                    settings.agent_invoke_url,
                    headers={
                        "Accept": "text/event-stream, application/json",
                        "X-API-Key": settings.agent_api_key.get_secret_value(),
                    },
                    json={"message": feature_name, "scene": AGENT_SCENE},
                ) as response:
                    if response.status_code != 200:
                        from app.core.diagnostic_http import capture_rejected_response
                        await capture_rejected_response(response)
                    _raise_upstream_status(response, request_id)
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" in content_type or "application/x-ndjson" in content_type:
                        done_event: dict[str, Any] | None = None
                        async for event in _iter_stream_events(response):
                            event_type = event.get("type")
                            if event_type == "token":
                                content = event.get("content")
                                if not isinstance(content, str):
                                    raise AgentExpandError(502, "agent_invalid_response", "Agent 返回格式异常，原输入已保留。")
                                token_parts.append(content)
                                if len("".join(token_parts)) > MAX_OUTPUT_CHARACTERS:
                                    raise AgentExpandError(502, "agent_invalid_response", "Agent 返回内容过大，原输入已保留。")
                                yield {"type": "token", "content": content}
                            elif event_type == "done":
                                done_event = event
                                break
                            elif event_type == "interrupt":
                                raise AgentExpandError(502, "agent_interrupted", "Agent 需要人工确认，本接口暂不支持续接；原输入已保留。")
                            elif event_type == "error":
                                raise AgentExpandError(502, "agent_upstream_error", "Agent 扩写失败，请稍后重试或联系管理员。")
                            # Other progress metadata is intentionally ignored.
                        if done_event is None:
                            raise AgentExpandError(502, "agent_invalid_response", "Agent 未正常结束扩写，原输入已保留。")
                        if done_event.get("scene") != AGENT_SCENE:
                            raise AgentExpandError(502, "agent_invalid_response", "Agent 返回场景不匹配，原输入已保留。")
                        output = done_event.get("output")
                        if not isinstance(output, str) or not output.strip():
                            output = "".join(token_parts)
                        if not output.strip() or len(output) > MAX_OUTPUT_CHARACTERS:
                            raise AgentExpandError(502, "agent_invalid_response", "Agent 未返回有效扩写文本，原输入已保留。")
                        yield {
                            "type": "done",
                            "scene": AGENT_SCENE,
                            "output": output.strip(),
                            "trace_id": _safe_trace_id(done_event.get("trace_id")),
                        }
                    else:
                        raw = await response.aread()
                        output, trace_id = _parse_json_response(raw)
                        yield {"type": "token", "content": output}
                        yield {
                            "type": "done",
                            "scene": AGENT_SCENE,
                            "output": output,
                            "trace_id": trace_id,
                        }
    except AgentExpandError:
        raise
    except (TimeoutError, httpx.TimeoutException) as error:
        raise AgentExpandError(504, "agent_timeout", "Agent 扩写超时，原输入已保留，请稍后重试。") from error
    except httpx.RequestError as error:
        raise AgentExpandError(502, "agent_unreachable", "暂时无法连接 Agent，原输入已保留。") from error
    except (ValueError, UnicodeError) as error:
        raise AgentExpandError(502, "agent_invalid_response", "Agent 返回格式异常，原输入已保留。") from error

    logger.info("agent_expand_completed request_id=%s", request_id)


async def expand_prd_prompt(feature_name: str, request_id: str) -> PrdExpandResponse:
    output: str | None = None
    trace_id: str | None = None
    async for event in stream_prd_expansion(feature_name, request_id):
        if event.get("type") == "done":
            output = event["output"]
            trace_id = event.get("trace_id")
    if not output:
        # stream_prd_expansion always emits done or raises; retain a defensive
        # guard so future adapter changes cannot return an empty successful body.
        raise AgentExpandError(502, "agent_invalid_response", "Agent 未返回有效扩写文本，原输入已保留。")
    return PrdExpandResponse(
        text=output,
        request_id=request_id,
        trace_id=trace_id,
    )
