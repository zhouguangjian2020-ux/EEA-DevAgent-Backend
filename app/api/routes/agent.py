import asyncio
import json
import secrets
import uuid
from typing import Literal

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.core.config import settings

from app.api.routes.auth import require_identity, require_user
from app.schemas.agent import (
    AgentErrorResponse,
    PrdExpandRequest,
    PrdExpandResponse,
    PrdGenerationRequest,
    UcDiagramRequest,
    UcDiagramResponse,
    UcDiagramSource,
    UcGenerationRequest,
    UcGenerationResponse,
    UcGenerationSource,
    UcTemplateResponse,
)
from app.services.agent import (
    AgentExpandError,
    expand_prd_prompt,
    stream_prd_expansion,
    validate_agent_configuration,
)
from app.services.projects import ProjectReadError
from app.services.prd_generation import (
    PrdGenerationError,
    read_prd_generation_context,
    stream_prd_generation,
    validate_prd_generation_configuration,
)
from app.services.uc_diagram import (
    UcDiagramError,
    generate_uc_diagram,
    read_accepted_ucs,
)
from app.services.uc_generation import (
    UcGenerationError,
    generate_uc_from_prd,
    read_confirmed_prd,
)
from app.services.uc_template import uc_template_response

router = APIRouter()


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _require_agent_callback_key(value: str | None) -> None:
    expected = settings.agent_callback_key.get_secret_value().strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Agent模板接口尚未配置服务间认证")
    if not value or not secrets.compare_digest(value.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="无效的Agent服务凭证")


@router.get(
    "/uc-template",
    response_model=UcTemplateResponse,
    summary="提供UC生成模板给Agent",
    description="仅允许Agent服务使用独立的服务间凭证调用；浏览器登录Cookie不能调用此接口。",
)
def get_uc_template(
    response: Response,
    scene: Literal["FROM_PRD_CREATE_UC"] = "FROM_PRD_CREATE_UC",
    agent_internal_key: str | None = Header(default=None, alias="X-Agent-Internal-Key"),
) -> UcTemplateResponse:
    request_id = uuid.uuid4().hex
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    try:
        _require_agent_callback_key(agent_internal_key)
    except HTTPException as error:
        raise HTTPException(status_code=error.status_code,
            detail={"code": "agent_template_not_configured" if error.status_code == 503 else "agent_unauthorized",
                    "message": error.detail, "request_id": request_id}, headers=headers) from error
    response.headers.update(headers)
    return uc_template_response()


@router.post(
    "/prd/expand",
    response_model=PrdExpandResponse,
    summary="PRD 自动扩写（完整结果，兼容缓冲上游）",
    description="需要登录 Cookie。仅调用 PROMPT_EXPAND；不生成 PRD、不写入业务数据库。",
    responses={code: {"model": AgentErrorResponse} for code in (502, 503, 504)},
)
async def expand_prd(
    payload: PrdExpandRequest,
    response: Response,
    ts_session: str | None = Cookie(default=None),
) -> PrdExpandResponse:
    require_user(ts_session)
    request_id = uuid.uuid4().hex
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    try:
        result = await expand_prd_prompt(payload.feature_name, request_id)
    except AgentExpandError as error:
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers=headers,
        ) from error
    response.headers.update(headers)
    return result


@router.post(
    "/prd/expand/stream",
    summary="PRD 自动扩写（浏览器 SSE 流式转发）",
    description="需要登录 Cookie。浏览器只连接业务后端；Agent API Key 始终留在服务端。",
    responses={code: {"model": AgentErrorResponse} for code in (502, 503, 504)},
)
async def expand_prd_stream(
    payload: PrdExpandRequest,
    ts_session: str | None = Cookie(default=None),
) -> StreamingResponse:
    require_user(ts_session)
    request_id = uuid.uuid4().hex
    try:
        # Configuration errors can still be returned as a normal HTTP error,
        # before the streaming response commits its headers.
        validate_agent_configuration()
    except AgentExpandError as error:
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
        ) from error

    async def event_stream():
        try:
            async for event in stream_prd_expansion(payload.feature_name, request_id):
                yield _sse_event(event)
        except AgentExpandError as error:
            yield _sse_event({"type": "error", "code": error.code, "message": error.message, "request_id": request_id})
        except Exception:
            # Do not send upstream exception details or private configuration.
            yield _sse_event({"type": "error", "code": "agent_bridge_error", "message": "自动扩写服务异常，原输入已保留，请稍后重试。", "request_id": request_id})

    response = StreamingResponse(event_stream(), media_type="text/event-stream")
    response.headers.update(
        {
            "X-Request-ID": request_id,
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        }
    )
    return response


@router.post(
    "/prd/generate/stream",
    summary="根据功能定义流式生成 PRD",
    description=(
        "需要登录 Cookie。浏览器只提交项目和功能身份；业务后端从只读项目数据库读取项目名称、功能名称和功能描述，"
        "调用 FROM_DEFINITION_CREATE_PRD，并向浏览器返回清洗后的 SSE token、done、error 事件。"
    ),
    responses={code: {"model": AgentErrorResponse} for code in (401, 404, 409, 413, 502, 503, 504)},
)
async def generate_prd_stream(
    payload: PrdGenerationRequest,
    request: Request,
    ts_session: str | None = Cookie(default=None),
) -> StreamingResponse:
    if payload.thread_id is not None:
        from app.api.routes.projects import _create_subject
        _create_subject(request, Response(), ts_session, request.headers.get('x-project-request'))
    identity = require_identity(ts_session)
    request_id = uuid.uuid4().hex
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}

    from app.services.task_progress import TaskProgress
    progress = None
    try:
        progress = await TaskProgress.begin(identity.subject, payload.project_id, payload, "PRD", payload.function_id)
        await progress.advance("generating", retry=True)
        context = read_prd_generation_context(
            identity.subject,
            project_id=payload.project_id,
            function_id=payload.function_id,
        )
        validate_prd_generation_configuration()
    except ProjectReadError as error:
        if progress: await progress.failed()
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers=headers,
        ) from error
    except PrdGenerationError as error:
        if progress: await progress.failed()
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers=headers,
        ) from error

    async def event_stream():
        try:
            async for event in stream_prd_generation(
                context,
                version=payload.version,
                request_id=request_id,
            ):
                if event.get("type") == "done":
                    await progress.preview(1, payload.client_request_id)
                yield _sse_event({**event, "request_id": request_id})
        except asyncio.CancelledError:
            await asyncio.shield(progress.failed(uncertain=True))
            raise
        except PrdGenerationError as error:
            await progress.failed()
            yield _sse_event({
                "type": "error",
                "code": error.code,
                "message": error.message,
                "request_id": request_id,
            })
        except Exception:
            await progress.failed()
            # Do not expose upstream exception details or private configuration.
            yield _sse_event({
                "type": "error",
                "code": "agent_bridge_error",
                "message": "PRD 生成服务异常，原输入已保留，请稍后重试。",
                "request_id": request_id,
            })

    response = StreamingResponse(event_stream(), media_type="text/event-stream")
    response.headers.update(
        {
            "X-Request-ID": request_id,
            # Prevent gzip/proxy transformation from buffering token events.
            "Cache-Control": "no-store, no-transform",
            "X-Accel-Buffering": "no",
        }
    )
    return response


@router.post(
    "/generations",
    response_model=UcGenerationResponse,
    summary="根据已确认PRD生成UC候选",
    description=(
        "需要登录 Cookie。浏览器只提交来源身份；业务后端从只读项目数据库读取当前已确认PRD，"
        "调用结构化UC生成场景并返回待审阅候选，不在本接口写入正式UC。"
    ),
    responses={code: {"model": AgentErrorResponse} for code in (400, 401, 409, 502, 503, 504)},
)
async def request_uc_generation(
    payload: UcGenerationRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ts_session: str | None = Cookie(default=None),
) -> UcGenerationResponse:
    identity = require_identity(ts_session)
    request_id = uuid.uuid4().hex
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    response.headers.update(headers)
    if idempotency_key is not None and idempotency_key != str(payload.client_request_id):
        raise HTTPException(
            status_code=400,
            detail={"code": "generation_request_id_mismatch", "message": "生成请求标识不一致，请重试。", "request_id": request_id},
            headers=headers,
        )

    try:
        prd = read_confirmed_prd(
            identity.subject,
            project_id=payload.project_id,
            function_id=payload.function_id,
            prd_id=payload.source.artifact_id,
            revision_id=payload.source.revision_id,
        )
        result = await generate_uc_from_prd(
            prd,
            client_request_id=payload.client_request_id,
            request_id=request_id,
        )
    except ProjectReadError as error:
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers=headers,
        ) from error
    except UcGenerationError as error:
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers=headers,
        ) from error

    return UcGenerationResponse(
        generation_id=payload.client_request_id,
        scene=payload.scene,
        status="succeeded",
        source=UcGenerationSource(
            project_id=prd.project_id,
            function_id=prd.function_id,
            prd_id=prd.prd_id,
            prd_revision_id=prd.revision_id,
        ),
        candidates=result.candidates,
        trace_id=result.trace_id,
    )


@router.post(
    "/uc-diagram",
    response_model=UcDiagramResponse,
    summary="根据当前功能已确认UC生成UC图代码",
    description=(
        "需要登录Cookie。业务后端只读取当前功能下所有已确认UC；来源版本未变化时不重复调用Agent。"
    ),
    responses={code: {"model": AgentErrorResponse} for code in (400, 401, 404, 413, 502, 503, 504)},
)
async def request_uc_diagram(
    payload: UcDiagramRequest,
    response: Response,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ts_session: str | None = Cookie(default=None),
) -> UcDiagramResponse:
    if payload.thread_id is not None:
        from app.api.routes.projects import _create_subject
        _create_subject(request, Response(), ts_session, request.headers.get('x-project-request'))
    identity = require_identity(ts_session)
    request_id = uuid.uuid4().hex
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    response.headers.update(headers)
    if idempotency_key is not None and idempotency_key != str(payload.client_request_id):
        raise HTTPException(
            status_code=400,
            detail={"code": "generation_request_id_mismatch", "message": "生成请求标识不一致，请重试。", "request_id": request_id},
            headers=headers,
        )

    from app.services.task_progress import TaskProgress
    progress = None
    try:
        progress = await TaskProgress.begin(identity.subject, payload.project_id, payload, 'UC_DIAGRAM', payload.function_id)
        await progress.advance('generating', retry=True)
        source = read_accepted_ucs(
            identity.subject,
            project_id=payload.project_id,
            function_id=payload.function_id,
        )
        if not source.ucs:
            await progress.empty()
            return UcDiagramResponse(
                generation_id=payload.client_request_id,
                scene=payload.scene,
                status="empty",
                source=UcDiagramSource(project_id=source.project_id, function_id=source.function_id, uc_count=0),
                source_version=source.source_version,
            )
        if payload.known_uc_source_version == source.source_version:
            await progress.preview(1, "unchanged:" + str(payload.client_request_id))
            return UcDiagramResponse(
                generation_id=payload.client_request_id,
                scene=payload.scene,
                status="unchanged",
                source=UcDiagramSource(project_id=source.project_id, function_id=source.function_id, uc_count=len(source.ucs)),
                source_version=source.source_version,
            )
        result = await generate_uc_diagram(
            source,
            client_request_id=payload.client_request_id,
            request_id=request_id,
        )
    except ProjectReadError as error:
        if progress: await progress.failed()
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers=headers,
        ) from error
    except UcDiagramError as error:
        if progress: await progress.failed()
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers=headers,
        ) from error

    except BaseException:
        if progress: await asyncio.shield(progress.failed(uncertain=True))
        raise

    await progress.preview(1, payload.client_request_id)
    return UcDiagramResponse(
        generation_id=payload.client_request_id,
        scene=payload.scene,
        status="succeeded",
        source=UcDiagramSource(project_id=source.project_id, function_id=source.function_id, uc_count=len(source.ucs)),
        source_version=source.source_version,
        diagram_format=result.diagram_format,
        diagram_code=result.diagram_code,
        trace_id=result.trace_id,
    )
