"""Read-only plan review plus explicitly scoped durable creation endpoints."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Cookie, Response

from app.api.routes.projects import _create_subject, _authenticated_read
from app.schemas.prd_creation import FeaturePlanRequest, FeaturePlanReview, PrdCreationChoice, PrdCreationIntent, PrdCreationRequest
from app.services import prd_creation
from app.services.prd_creation_plan import plan_feature
from app.services.projects import ProjectReadError

router = APIRouter()


@router.post(
    "/{project_id}/prd-creations/plan", response_model=FeaturePlanReview,
    summary="读取Agent功能计划并检查当前项目重复候选（不创建对象）",
    description="S1仅供计划审阅；不会创建功能/PRD，也不会自动选择候选。不是生成成功或持久任务回执。",
)
async def plan_prd_creation(project_id: UUID, payload: FeaturePlanRequest, identity=Depends(_create_subject)):
    subject, request_id = identity
    try:
        return await plan_feature(subject, project_id, payload, request_id)
    except ProjectReadError as error:
        raise HTTPException(
            error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
        ) from error


@router.post("/{project_id}/prd-creations/intents", response_model=PrdCreationIntent,
             summary="建立持久PRD创建意图并读取计划（尚不创建功能/PRD）")
async def start_creation(project_id: UUID, payload: PrdCreationRequest, identity=Depends(_create_subject)):
    subject, request_id = identity
    try:
        return await prd_creation.start_intent(subject, project_id, payload, request_id)
    except ProjectReadError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message, "request_id": request_id},
                            headers={"Cache-Control": "no-store", "X-Request-ID": request_id}) from error


@router.get("/{project_id}/prd-creations/intents", response_model=PrdCreationIntent,
            summary="按原请求标识查回本人创建记录（首次响应丢失时使用）")
def find_creation(project_id: UUID, client_request_id: UUID, response: Response, ts_session: str | None = Cookie(default=None), thread_id: UUID | None = None):
    return _authenticated_read(response, ts_session, lambda subject: prd_creation.find_intent(subject, project_id, client_request_id, thread_id))


@router.get("/{project_id}/prd-creations/intents/{intent_id}", response_model=PrdCreationIntent,
            summary="读取本人创建回执；不自动重试、不调用Agent")
def get_creation(project_id: UUID, intent_id: UUID, response: Response, ts_session: str | None = Cookie(default=None), thread_id: UUID | None = None):
    return _authenticated_read(response, ts_session, lambda subject: prd_creation.read_intent(subject, project_id, intent_id, thread_id))


@router.post("/{project_id}/prd-creations/intents/{intent_id}/execute", response_model=PrdCreationIntent,
             summary="确认新建/复用选择或显式重试；仅保存待确认PRD")
async def execute_creation(project_id: UUID, intent_id: UUID, payload: PrdCreationChoice, identity=Depends(_create_subject)):
    subject, request_id = identity
    try:
        return await prd_creation.execute_intent(subject, project_id, intent_id, payload, request_id)
    except ProjectReadError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message, "request_id": request_id},
                            headers={"Cache-Control": "no-store", "X-Request-ID": request_id}) from error
