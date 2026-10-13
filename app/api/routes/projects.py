from collections.abc import Callable
from uuid import UUID, uuid4

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response

from app.api.routes.auth import require_identity
from app.core.config import settings
from app.schemas.metadata_edit import FunctionRead, MetadataPatch
from app.services import metadata_edit
from app.schemas.projects import ProjectCreate, ProjectErrorResponse, ProjectListResponse, ProjectRead
from app.services.project_creation import create_project
from app.schemas.project_workspace import ProjectWorkspaceResponse
from app.services.projects import ProjectReadError, get_project, list_projects
from app.services.project_workspace import get_project_workspace

router = APIRouter()


def _authenticated_read(response: Response, session: str | None, read: Callable):
    request_id = uuid4().hex
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    try:
        identity = require_identity(session)
    except HTTPException as error:
        raise HTTPException(error.status_code, error.detail, headers=headers) from error
    try:
        # Never accept subject/user_id from query parameters or browser payloads.
        result = read(identity.subject)
    except ProjectReadError as error:
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers=headers,
        ) from error
    response.headers.update(headers)
    return result


@router.get(
    "", response_model=ProjectListResponse, summary="读取当前用户可访问的项目列表",
    description="只读；现有Cookie登录+active业务用户/项目成员+project.read项目授权。未授权项目不返回。",
    responses={401: {"description": "需要现有Cookie登录"}, 503: {"model": ProjectErrorResponse}, 504: {"model": ProjectErrorResponse}},
)
def read_projects(
    response: Response,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    ts_session: str | None = Cookie(default=None),
) -> ProjectListResponse:
    return _authenticated_read(response, ts_session, lambda subject: list_projects(subject, limit, offset))


@router.get(
    "/{project_id}", response_model=ProjectRead, summary="读取有权限的单个项目",
    description="只读；不存在、已归档和无访问权限统一返回404，不泄露其他项目。",
    responses={401: {"description": "需要现有Cookie登录"}, **{code: {"model": ProjectErrorResponse} for code in (404, 503, 504)}},
)
def read_project(project_id: UUID, response: Response, ts_session: str | None = Cookie(default=None)) -> ProjectRead:
    return _authenticated_read(response, ts_session, lambda subject: get_project(subject, project_id))
@router.get(
    "/{project_id}/workspace",
    response_model=ProjectWorkspaceResponse,
    summary="读取项目工作空间与当前用户能力",
    description="只读查询功能、PRD、UC、FR、分配和已接受UC候选，附带当前用户创建/更新/接受能力；修改通过独立POST接口提交。",
    responses={401: {"description": "需要现有Cookie登录"}, **{code: {"model": ProjectErrorResponse} for code in (404, 503, 504)}},
)
def read_project_workspace(
    project_id: UUID,
    response: Response,
    ts_session: str | None = Cookie(default=None),
) -> ProjectWorkspaceResponse:
    return _authenticated_read(response, ts_session, lambda subject: get_project_workspace(subject, project_id))

def _create_subject(
    request: Request, response: Response, ts_session: str | None = Cookie(default=None),
    x_project_request: str | None = Header(default=None, description="创建接口防跨站请求标记，填 1"),
) -> tuple[str, str]:
    request_id = uuid4().hex
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    try:
        identity = require_identity(ts_session)
    except HTTPException as error:
        raise HTTPException(error.status_code, error.detail, headers=headers) from error
    if request.headers.get("origin") not in settings.projects_write_origins or x_project_request != "1":
        raise HTTPException(403, detail={"code": "project_create_origin_forbidden", "message": "请求来源不允许业务写入", "request_id": request_id}, headers=headers)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, detail={"code": "project_create_json_required", "message": "请使用JSON提交", "request_id": request_id}, headers=headers)
    response.headers.update(headers)
    return identity.subject, request_id


@router.post(
    "", response_model=ProjectRead, status_code=201, summary="创建项目并初始化默认子系统目录",
    description="需要Cookie、允许的Origin与X-Project-Request: 1。creation_id为UUIDv4，原样重试不重复创建。同一事务初始化本人成员、项目角色和版本化默认子系统目录，不复制需求或分配；业务写开关启用时授予项目需求维护者，否则仅阅读。",
    responses={200: {"model": ProjectRead, "description": "安全重试，项目已存在"},
               **{code: {"model": ProjectErrorResponse} for code in (403, 409, 415, 503, 504)},
               401: {"description": "需要登录"}},
)
def create_project_route(
    payload: ProjectCreate, response: Response, identity: tuple[str, str] = Depends(_create_subject),
) -> ProjectRead:
    subject, request_id = identity
    try:
        project, created = create_project(subject, payload, request_id)
    except ProjectReadError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message, "request_id": request_id},
                            headers={"X-Request-ID": request_id, "Cache-Control": "no-store"}) from error
    response.status_code = 201 if created else 200
    response.headers["Location"] = f"/v1/projects/{project.id}"
    return project


def _metadata_mutation(response, identity, operation, *args):
    subject, request_id = identity
    try:
        return operation(subject, *args, request_id)
    except ProjectReadError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message, "request_id": request_id},
            headers={"X-Request-ID": request_id, "Cache-Control": "no-store"}) from error


@router.patch("/{project_id}", response_model=ProjectRead, summary="修改项目名称和简介",
    responses={code: {"model": ProjectErrorResponse} for code in (403,404,409,415,503,504)})
def update_project_route(project_id: UUID, payload: MetadataPatch, response: Response, identity=Depends(_create_subject)):
    return _metadata_mutation(response, identity, metadata_edit.update_project, project_id, payload)


@router.patch("/{project_id}/functions/{function_id}", response_model=FunctionRead, summary="修改功能名称和描述",
    responses={code: {"model": ProjectErrorResponse} for code in (403,404,409,415,503,504)})
def update_function_route(project_id: UUID, function_id: UUID, payload: MetadataPatch, response: Response, identity=Depends(_create_subject)):
    return _metadata_mutation(response, identity, metadata_edit.update_function, project_id, function_id, payload)


