"""Read a project/function definition and stream Markdown PRD generation."""
from collections.abc import AsyncIterator
from dataclasses import dataclass
import asyncio
import json
import logging
from typing import Any
from uuid import UUID
from urllib.parse import urlsplit

import httpx
import psycopg

from app.core.config import settings
from app.core.agent_limits import generation_timeout_seconds, generation_timeout_budget
from app.core.database import (
    ProjectDatabaseNotConfigured,
    UnsafeProjectDatabaseRole,
    project_connection,
)
from app.services.agent import (
    AgentExpandError,
    _iter_stream_events,
    agent_client,
    validate_agent_configuration,
)
from app.services.projects import ProjectReadError, _SELECT

logger = logging.getLogger(__name__)

PRD_GENERATION_SCENE = "FROM_DEFINITION_CREATE_PRD"
MAX_PRD_OUTPUT_CHARACTERS = 200_000
MAX_REFERENCE_DATA_BYTES = 256 * 1024


@dataclass(frozen=True)
class PrdGenerationContext:
    project_id: UUID
    project_name: str
    project_description: str | None
    function_id: UUID
    function_name: str
    function_description: str


class PrdGenerationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(code)


def _database_error(error: Exception) -> ProjectReadError:
    if isinstance(error, ProjectDatabaseNotConfigured):
        return ProjectReadError(503, "projects_not_configured", "项目数据库尚未配置")
    if isinstance(error, UnsafeProjectDatabaseRole):
        return ProjectReadError(503, "projects_unsafe_database_role", "项目接口需要专用低权限数据库账号")
    if isinstance(error, (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable)):
        return ProjectReadError(504, "projects_query_timeout", "项目查询超时，请稍后重试")
    return ProjectReadError(503, "projects_database_unavailable", "项目数据库暂不可用")


def read_prd_generation_context(
    subject: str,
    *,
    project_id: UUID,
    function_id: UUID,
) -> PrdGenerationContext:
    """Read the project and function server-side; the browser sends identities only."""
    try:
        with project_connection(repeatable_read=True) as connection:
            project = connection.execute(
                _SELECT + " AND p.id = %s",
                (subject, project_id),
            ).fetchone()
            if not project:
                raise ProjectReadError(404, "project_not_found", "项目不存在或无权访问")

            function = connection.execute(
                """
                SELECT id, project_id, name, description
                FROM ee.business_functions
                WHERE id = %s AND project_id = %s AND archived_at IS NULL
                """,
                (function_id, project_id),
            ).fetchone()
            if not function:
                raise ProjectReadError(404, "function_not_found", "功能不存在或不属于当前项目")

            definition = (function.get("description") or "").strip()
            if not definition:
                raise ProjectReadError(409, "function_definition_missing", "请先完善功能描述后再生成 PRD")

            return PrdGenerationContext(
                project_id=project_id,
                project_name=str(project["name"]).strip(),
                project_description=project.get("description"),
                function_id=function_id,
                function_name=str(function["name"]).strip(),
                function_description=definition,
            )
    except ProjectReadError:
        raise
    except (ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole, psycopg.Error, ValueError) as error:
        raise _database_error(error) from error


def _safe_trace_id(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 200 else None


def _generation_message(context: PrdGenerationContext, version: str | None) -> str:
    version_line = (
        f"版本号：{version}"
        if version
        else "版本号：未指定；不要自行编造具体版本号，必要时使用待确认标记。"
    )
    return "\n".join(
        [
            "请根据以下功能定义生成完整的产品需求文档（PRD）。",
            "",
            f"功能名称：{context.function_name}",
            f"适用项目：{context.project_name}",
            version_line,
            "",
            "功能定义：",
            context.function_description,
            "",
            "输出要求：",
            "1. 只输出 PRD Markdown 正文。",
            "2. 不要输出解释、生成说明、JSON 或代码围栏。",
            "3. 不要编造功能定义中没有给出的具体事实；无法确定的内容使用待确认标记。",
            "4. 保留完整、清晰的 PRD 章节结构。",
        ]
    )


def build_prd_generation_request(
    context: PrdGenerationContext,
    *,
    version: str | None,
    request_id: str,
    original_prompt: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build the stable Agent contract shared by future generation adapters."""
    reference_data = {
        "source": {
            "kind": "FUNCTION_DEFINITION",
            "project_id": str(context.project_id),
            "function_id": str(context.function_id),
        },
        "project": {
            "id": str(context.project_id),
            "name": context.project_name,
            "description": context.project_description,
        },
        "function": {
            "id": str(context.function_id),
            "name": context.function_name,
            "description": context.function_description,
        },
        "function_definition": context.function_description,
        "version": version,
    }
    if original_prompt is not None:
        reference_data["original_prompt"] = original_prompt
    if len(json.dumps(reference_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_REFERENCE_DATA_BYTES:
        raise PrdGenerationError(413, "function_definition_too_large", "功能定义过大，超过 Agent 参考资料限制")

    result = {
        "message": _generation_message(context, version) + ("\n\n用户本次原始需求：\n" + original_prompt if original_prompt is not None else ""),
        "scene": PRD_GENERATION_SCENE,
        "inputs": {
            "feature_name": context.function_name,
            "project": context.project_name,
            "version": version,
        },
        "reference_data": reference_data,
        "response_format": "text",
        "trace_id": request_id,
        "timeout_seconds": generation_timeout_budget(),
    }

    if idempotency_key is not None:
        result["idempotency_key"] = idempotency_key
    return result


def _upstream_error(response: httpx.Response) -> PrdGenerationError:
    if response.status_code == 429:
        return PrdGenerationError(503, "agent_busy", "Agent 当前繁忙，请稍后重试。")
    return PrdGenerationError(502, "agent_upstream_error", "Agent 生成 PRD 失败，请稍后重试或联系管理员。")


def _validate_buffered_output(raw: bytes, *, strict_completion=False) -> tuple[str, str | None]:
    if len(raw) > 1_000_000:
        raise PrdGenerationError(502, "agent_invalid_response", "Agent 返回内容过大，原输入已保留。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PrdGenerationError(502, "agent_invalid_response", "Agent 返回格式异常，原输入已保留。") from error
    if not isinstance(payload, dict) or payload.get("scene") != PRD_GENERATION_SCENE:
        raise PrdGenerationError(502, "agent_invalid_response", "Agent 返回场景不匹配，原输入已保留。")
    if strict_completion and (payload.get("error") is not None or payload.get("interrupt") is not None):
        raise PrdGenerationError(502, "agent_invalid_response", "Agent返回错误或未完成的PRD，未保存")
    output = payload.get("output")
    if not isinstance(output, str) or not output.strip() or len(output) > MAX_PRD_OUTPUT_CHARACTERS:
        raise PrdGenerationError(502, "agent_invalid_response", "Agent 未返回有效 PRD Markdown，原输入已保留。")
    return output.strip(), _safe_trace_id(payload.get("trace_id"))


def _translate_agent_error(error: AgentExpandError) -> PrdGenerationError:
    message = error.message.replace("扩写", "生成 PRD")
    return PrdGenerationError(error.status, error.code, message)


def _stream_agent_url() -> str:
    """Use only the documented stream sibling; never change private settings."""
    target = urlsplit(settings.agent_invoke_url.strip())
    path = target.path.rstrip("/")
    if path.endswith("/invoke"):
        path += "/stream"
    elif not path.endswith("/invoke/stream"):
        raise PrdGenerationError(503, "agent_not_configured", "PRD流式服务地址不符合调用契约，请核对服务端配置。")
    return target._replace(path=path).geturl()


async def stream_prd_generation(
    context: PrdGenerationContext,
    *,
    version: str | None,
    request_id: str,
    original_prompt: str | None = None,
    idempotency_key: str | None = None,
    strict_completion: bool = False,
    require_stream: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Read PRD token/done events without writing a business object.

    Durable creation opts into require_stream and strict_completion: no ordinary
    invoke fallback, no token-only success, and persistence only after valid done.
    Legacy read-only preview callers retain their existing response compatibility.
    """
    try:
        validate_agent_configuration()
    except AgentExpandError as error:
        raise _translate_agent_error(error) from error

    target_url = _stream_agent_url() if require_stream else settings.agent_invoke_url
    request_body = build_prd_generation_request(context, version=version, request_id=request_id, original_prompt=original_prompt, idempotency_key=idempotency_key)
    token_parts: list[str] = []
    try:
        async with asyncio.timeout(generation_timeout_seconds()):
            async with agent_client() as client:
                async with client.stream(
                    "POST",
                    target_url,
                    headers={
                        "Accept": "text/event-stream" if require_stream else "text/event-stream, application/json",
                        "X-API-Key": settings.agent_api_key.get_secret_value(),
                    },
                    json=request_body,
                    timeout=httpx.Timeout(generation_timeout_seconds(), connect=5),
                ) as response:
                    if response.status_code != 200:
                        from app.core.diagnostic_http import capture_rejected_response
                        await capture_rejected_response(response)
                        raise _upstream_error(response)
                    content_type = response.headers.get("content-type", "").lower()
                    is_stream = "text/event-stream" in content_type or "application/x-ndjson" in content_type
                    if require_stream and not is_stream:
                        raise PrdGenerationError(502, "agent_invalid_response", "Agent未返回约定的PRD流式响应，未保存结果。")
                    if is_stream:
                        done_event: dict[str, Any] | None = None
                        try:
                            async for event in _iter_stream_events(response):
                                event_type = event.get("type")
                                if event_type == "token":
                                    content = event.get("content")
                                    if not isinstance(content, str):
                                        raise PrdGenerationError(502, "agent_invalid_response", "Agent 返回格式异常，原输入已保留。")
                                    token_parts.append(content)
                                    accumulated = "".join(token_parts)
                                    if len(accumulated) > MAX_PRD_OUTPUT_CHARACTERS:
                                        raise PrdGenerationError(502, "agent_invalid_response", "Agent 返回内容过大，原输入已保留。")
                                    yield {"type": "token", "content": content}
                                elif event_type == "done":
                                    done_event = event
                                    break
                                elif event_type == "interrupt":
                                    raise PrdGenerationError(502, "agent_interrupted", "Agent 需要人工确认，本接口暂不支持续接；原输入已保留。")
                                elif event_type == "error":
                                    raise PrdGenerationError(502, "agent_upstream_error", "Agent 生成 PRD 失败，请稍后重试或联系管理员。")
                            if done_event is None:
                                raise PrdGenerationError(502, "agent_invalid_response", "Agent 未正常结束 PRD 生成，原输入已保留。")
                        except AgentExpandError as error:
                            raise _translate_agent_error(error) from error

                        if done_event.get("scene") != PRD_GENERATION_SCENE:
                            raise PrdGenerationError(502, "agent_invalid_response", "Agent 返回场景不匹配，原输入已保留。")
                        if strict_completion and (done_event.get("error") is not None or done_event.get("interrupt") is not None):
                            raise PrdGenerationError(502, "agent_invalid_response", "Agent完成事件无效，PRD未保存")
                        output = done_event.get("output")
                        if not isinstance(output, str) or not output.strip():
                            if strict_completion:
                                raise PrdGenerationError(502, "agent_invalid_response", "Agent未返回完整PRD完成正文，未保存流式片段。")
                            output = "".join(token_parts)
                        if not output.strip() or len(output) > MAX_PRD_OUTPUT_CHARACTERS:
                            raise PrdGenerationError(502, "agent_invalid_response", "Agent 未返回有效 PRD Markdown，原输入已保留。")
                        yield {
                            "type": "done",
                            "scene": PRD_GENERATION_SCENE,
                            "output": output.strip(),
                            "trace_id": _safe_trace_id(done_event.get("trace_id")),
                        }
                    else:
                        output, trace_id = _validate_buffered_output(await response.aread(), strict_completion=strict_completion)
                        yield {"type": "token", "content": output}
                        yield {
                            "type": "done",
                            "scene": PRD_GENERATION_SCENE,
                            "output": output,
                            "trace_id": trace_id,
                        }
    except PrdGenerationError:
        raise
    except AgentExpandError as error:
        raise _translate_agent_error(error) from error
    except (TimeoutError, httpx.TimeoutException) as error:
        raise PrdGenerationError(504, "agent_timeout", "Agent 生成 PRD 超时，原输入已保留，请稍后重试。") from error
    except httpx.RequestError as error:
        raise PrdGenerationError(502, "agent_unreachable", "暂时无法连接 Agent，原输入已保留。") from error
    except (UnicodeError, ValueError) as error:
        raise PrdGenerationError(502, "agent_invalid_response", "Agent 返回格式异常，原输入已保留。") from error

    logger.info("agent_prd_generation_completed request_id=%s", request_id)


def validate_prd_generation_configuration() -> None:
    try:
        validate_agent_configuration()
    except AgentExpandError as error:
        raise _translate_agent_error(error) from error
