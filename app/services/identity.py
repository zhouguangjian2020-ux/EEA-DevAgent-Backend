"""Database-backed local identities used by authentication and permission administration."""
from __future__ import annotations

from uuid import UUID

from app.core.config import settings
from app.core.database import project_connection
from app.core.security import verify_password
from app.schemas.auth import UserResponse


class IdentityDatabaseError(Exception):
    """Raised when the identity store cannot be read safely."""


def normalize_login_name(value: str) -> str:
    return " ".join(value.strip().split()).lower()


def user_response(row: dict) -> UserResponse:
    metadata = row.get("metadata") or {}
    department = metadata.get("department") if isinstance(metadata, dict) else None
    return UserResponse(
        id=str(row["id"]),
        username=row["display_name"],
        department=str(department or ""),
        avatar_url=metadata.get("avatar_url") if isinstance(metadata, dict) else None,
        can_manage_accounts=row.get("external_subject") in settings.account_admin_subjects,
    )


def authenticate_login(login_name: str, password: str) -> tuple[str, UserResponse] | None:
    normalized = normalize_login_name(login_name)
    if not normalized:
        return None
    try:
        with project_connection() as db:
            row = db.execute(
                """
                SELECT u.id, u.external_subject, u.display_name, u.metadata, u.state,
                       c.password_hash
                FROM ee.app_credentials c
                JOIN ee.app_users u ON u.id=c.user_id
                WHERE c.login_name=%s
                """,
                (normalized,),
            ).fetchone()
    except Exception as error:  # raw DB diagnostics never leave this layer
        raise IdentityDatabaseError() from error
    if not row or row["state"] != "active" or not verify_password(password, row["password_hash"]):
        return None
    return row["external_subject"], user_response(row)


def find_subject(subject: str) -> UserResponse | None:
    try:
        with project_connection() as db:
            row = db.execute(
                "SELECT id, external_subject, display_name, metadata, state "
                "FROM ee.app_users WHERE external_subject=%s",
                (subject,),
            ).fetchone()
    except Exception as error:
        raise IdentityDatabaseError() from error
    if not row or row["state"] != "active":
        return None
    return user_response(row)


def user_id_for_subject(db, subject: str) -> UUID:
    row = db.execute(
        "SELECT id FROM ee.app_users WHERE external_subject=%s AND state='active'",
        (subject,),
    ).fetchone()
    if not row:
        raise LookupError("active user not found")
    return row["id"]

