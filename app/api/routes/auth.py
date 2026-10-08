from dataclasses import dataclass

from fastapi import APIRouter, Cookie, HTTPException, Response, status

from app.core.config import settings
from app.core.security import (
    SESSION_COOKIE_NAME,
    create_session_token,
    decode_session_token,
    verify_demo_password,
)
from app.schemas.auth import LoginRequest, LoginResponse, UserResponse
from app.services.identity import IdentityDatabaseError, authenticate_login, find_subject

router = APIRouter()


@dataclass(frozen=True)
class AuthenticatedIdentity:
    subject: str
    user: UserResponse


def demo_user() -> UserResponse:
    return UserResponse(
        id="demo-engineer-01",
        username="工程师01",
        department="电子电器架构部",
        avatar_url=None,
        can_manage_accounts=True,
    )


def _database_auth_enabled() -> bool:
    return bool(settings.projects_database_url.get_secret_value().strip())


def _auth_store_unavailable(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="账号服务暂不可用，请联系维护人员",
    )


def require_identity(session: str | None) -> AuthenticatedIdentity:
    subject = decode_session_token(session or "")
    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录已过期")
    if not _database_auth_enabled():
        if subject != settings.demo_username:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录已过期")
        return AuthenticatedIdentity(subject=settings.demo_username, user=demo_user())
    try:
        user = find_subject(subject)
    except IdentityDatabaseError as error:
        raise _auth_store_unavailable(error) from error
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录已过期")
    return AuthenticatedIdentity(subject=subject, user=user)


def require_user(session: str | None) -> UserResponse:
    return require_identity(session).user


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response) -> LoginResponse:
    if _database_auth_enabled():
        try:
            authenticated = authenticate_login(payload.username, payload.password)
        except IdentityDatabaseError as error:
            raise _auth_store_unavailable(error) from error
        if authenticated is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
        subject, user = authenticated
    else:
        if payload.username != settings.demo_username or not verify_demo_password(payload.password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
        subject, user = settings.demo_username, demo_user()

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_token(subject),
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.session_ttl_minutes * 60,
        path="/",
    )
    return LoginResponse(user=user)


@router.get("/me", response_model=UserResponse)
def me(ts_session: str | None = Cookie(default=None)) -> UserResponse:
    return require_identity(ts_session).user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")

