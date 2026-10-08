from collections.abc import Callable
from uuid import UUID, uuid4

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response

from app.api.routes.auth import require_identity
from app.core.config import settings
from app.schemas.permissions import (
    AccountCreate,
    AccountListResponse,
    AccountPatch,
    AccountRead,
    FunctionPermissionUpsert,
    PermissionMutationResponse,
    PermissionSettingsResponse,
    ProjectMemberUpsert,
)
from app.services import account_admin, permission_admin
from app.services.projects import ProjectReadError

router = APIRouter()


def _error(response: Response, request_id: str, error: ProjectReadError) -> HTTPException:
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    response.headers.update(headers)
    return HTTPException(error.status, detail={"code": error.code, "message": error.message, "request_id": request_id}, headers=headers)


def _read_identity(response: Response, session: str | None):
    request_id = uuid4().hex
    try:
        identity = require_identity(session)
    except HTTPException as error:
        response.headers.update({"X-Request-ID": request_id, "Cache-Control": "no-store"})
        raise HTTPException(error.status_code, error.detail, headers=response.headers) from error
    response.headers.update({"X-Request-ID": request_id, "Cache-Control": "no-store"})
    return identity, request_id


def _write_identity(
    request: Request,
    response: Response,
    session: str | None,
    x_project_request: str | None,
):
    identity, request_id = _read_identity(response, session)
    if request.headers.get("origin") not in settings.projects_write_origins or x_project_request != "1":
        raise HTTPException(403, detail={"code": "permission_write_origin_forbidden", "message": "请求来源不允许权限写入", "request_id": request_id}, headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, detail={"code": "permission_json_required", "message": "请使用JSON提交", "request_id": request_id}, headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})
    return identity, request_id


def _execute(response: Response, request_id: str, operation: Callable, *args):
    try:
        result = operation(*args, request_id)
    except ProjectReadError as error:
        raise _error(response, request_id, error) from error
    response.headers["X-Request-ID"] = request_id
    return result


@router.get("/v1/accounts", response_model=AccountListResponse, summary="读取账号列表")
def read_accounts(response: Response, ts_session: str | None = Cookie(default=None)) -> AccountListResponse:
    identity, request_id = _read_identity(response, ts_session)
    try:
        return account_admin.list_accounts(identity.subject)
    except ProjectReadError as error:
        raise _error(response, request_id, error) from error


@router.post("/v1/accounts", response_model=AccountRead, status_code=201, summary="创建本地登录账号")
def create_account_route(
    request: Request,
    payload: AccountCreate,
    response: Response,
    x_project_request: str | None = Header(default=None),
    ts_session: str | None = Cookie(default=None),
) -> AccountRead:
    identity, request_id = _write_identity(request, response, ts_session, x_project_request)
    return _execute(response, request_id, account_admin.create_account, identity.subject, payload)


@router.patch("/v1/accounts/{user_id}", response_model=AccountRead, summary="启用或停用登录账号")
def update_account_route(
    user_id: UUID,
    request: Request,
    payload: AccountPatch,
    response: Response,
    x_project_request: str | None = Header(default=None),
    ts_session: str | None = Cookie(default=None),
) -> AccountRead:
    identity, request_id = _write_identity(request, response, ts_session, x_project_request)
    return _execute(response, request_id, account_admin.update_account, identity.subject, user_id, payload)


@router.get("/v1/projects/{project_id}/permissions", response_model=PermissionSettingsResponse, summary="读取当前项目权限设置")
def read_permission_settings(project_id: UUID, response: Response, ts_session: str | None = Cookie(default=None)) -> PermissionSettingsResponse:
    identity, request_id = _read_identity(response, ts_session)
    try:
        return permission_admin.get_permission_settings(identity.subject, project_id)
    except ProjectReadError as error:
        raise _error(response, request_id, error) from error


@router.get("/v1/projects/{project_id}/permission-candidates", response_model=AccountListResponse, summary="读取可加入项目的账号")
def read_permission_candidates(project_id: UUID, response: Response, ts_session: str | None = Cookie(default=None)) -> AccountListResponse:
    identity, request_id = _read_identity(response, ts_session)
    try:
        rows = permission_admin.account_candidates(identity.subject, project_id)
        return AccountListResponse(items=[AccountRead(state="active", **row) for row in rows])
    except ProjectReadError as error:
        raise _error(response, request_id, error) from error


@router.post("/v1/projects/{project_id}/members", response_model=PermissionMutationResponse, summary="添加或调整项目成员")
def upsert_member_route(
    project_id: UUID,
    request: Request,
    payload: ProjectMemberUpsert,
    response: Response,
    x_project_request: str | None = Header(default=None),
    ts_session: str | None = Cookie(default=None),
) -> PermissionMutationResponse:
    identity, request_id = _write_identity(request, response, ts_session, x_project_request)
    _execute(response, request_id, permission_admin.upsert_member, identity.subject, project_id, payload)
    return PermissionMutationResponse()


@router.delete("/v1/projects/{project_id}/members/{user_id}", response_model=PermissionMutationResponse, summary="移出项目成员")
def remove_member_route(
    project_id: UUID,
    user_id: UUID,
    request: Request,
    response: Response,
    x_project_request: str | None = Header(default=None),
    ts_session: str | None = Cookie(default=None),
) -> PermissionMutationResponse:
    identity, request_id = _write_identity(request, response, ts_session, x_project_request)
    _execute(response, request_id, permission_admin.remove_member, identity.subject, project_id, user_id)
    return PermissionMutationResponse()


@router.post("/v1/projects/{project_id}/functions/{function_id}/permissions", response_model=PermissionMutationResponse, summary="分配功能权限")
def upsert_function_permission_route(
    project_id: UUID,
    function_id: UUID,
    request: Request,
    payload: FunctionPermissionUpsert,
    response: Response,
    x_project_request: str | None = Header(default=None),
    ts_session: str | None = Cookie(default=None),
) -> PermissionMutationResponse:
    identity, request_id = _write_identity(request, response, ts_session, x_project_request)
    _execute(response, request_id, permission_admin.upsert_function_permission, identity.subject, project_id, function_id, payload)
    return PermissionMutationResponse()


@router.delete("/v1/projects/{project_id}/functions/{function_id}/permissions/{user_id}", response_model=PermissionMutationResponse, summary="移除功能权限")
def remove_function_permission_route(
    project_id: UUID,
    function_id: UUID,
    user_id: UUID,
    request: Request,
    response: Response,
    x_project_request: str | None = Header(default=None),
    ts_session: str | None = Cookie(default=None),
) -> PermissionMutationResponse:
    identity, request_id = _write_identity(request, response, ts_session, x_project_request)
    _execute(response, request_id, permission_admin.remove_function_permission, identity.subject, project_id, function_id, user_id)
    return PermissionMutationResponse()
