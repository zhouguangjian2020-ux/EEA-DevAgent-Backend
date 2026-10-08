from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

SESSION_COOKIE_NAME = "ts_session"
password_hash = PasswordHash.recommended()
_demo_password_hash = password_hash.hash(settings.demo_password)


def verify_demo_password(password: str) -> bool:
    return password_hash.verify(password, _demo_password_hash)


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify an Argon2id password hash without exposing hash details to callers."""
    try:
        return password_hash.verify(password, stored_hash)
    except (ValueError, TypeError):
        return False


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def create_session_token(subject: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.session_ttl_minutes)
    return jwt.encode(
        {"sub": subject, "exp": expires_at},
        settings.secret_key,
        algorithm="HS256",
    )


def decode_session_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None
