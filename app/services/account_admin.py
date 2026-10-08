"""Opt-in global account administration using the shared business writer."""
from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.core.config import settings
from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole, project_connection
from app.core.security import hash_password
from app.schemas.permissions import AccountCreate, AccountListResponse, AccountPatch, AccountRead
from app.services.identity import normalize_login_name
from app.services.manual_work import write_transaction
from app.services.projects import ProjectReadError


def _require_admin(subject: str) -> None:
    if subject not in settings.account_admin_subjects:
        raise ProjectReadError(403, "account_manage_forbidden", "当前账号没有账号管理权限")


def _account(row: dict) -> AccountRead:
    return AccountRead(
        id=row["id"], login_name=row["login_name"], display_name=row["display_name"],
        department=str(row.get("department") or ""), state=row["state"],
    )


def list_accounts(subject: str) -> AccountListResponse:
    _require_admin(subject)
    try:
        with project_connection() as db:
            rows = db.execute(
                """
                SELECT u.id,c.login_name,u.display_name,u.metadata->>'department' AS department,u.state
                FROM ee.app_users u JOIN ee.app_credentials c ON c.user_id=u.id
                ORDER BY u.display_name,u.id
                """
            ).fetchall()
    except ProjectReadError:
        raise
    except (ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole) as error:
        raise ProjectReadError(503, "account_database_not_configured", "账号管理需要已配置的低权限数据库连接") from error
    except psycopg.Error as error:
        raise ProjectReadError(503, "account_database_unavailable", "账号查询暂不可用") from error
    return AccountListResponse(items=[_account(row) for row in rows])


def create_account(subject: str, payload: AccountCreate, request_id: str) -> AccountRead:
    _require_admin(subject)
    login_name = normalize_login_name(payload.login_name)
    if not login_name:
        raise ProjectReadError(422, "account_login_invalid", "登录名不能为空")
    with write_transaction() as db:
        actor = db.execute("SELECT id FROM ee.app_users WHERE external_subject=%s AND state='active'", (subject,)).fetchone()
        if not actor:
            raise ProjectReadError(401, "not_authenticated", "当前账号不可用")
        if db.execute("SELECT 1 FROM ee.app_credentials WHERE login_name=%s", (login_name,)).fetchone():
            raise ProjectReadError(409, "account_login_exists", "登录名已存在，请更换后重试")
        user_id = uuid4()
        external_subject = f"local:{user_id}"
        db.execute(
            "INSERT INTO ee.app_users(id,external_subject,display_name,metadata) VALUES(%s,%s,%s,%s)",
            (user_id, external_subject, payload.display_name, Jsonb({"department": payload.department})),
        )
        db.execute(
            "INSERT INTO ee.app_credentials(user_id,login_name,password_hash) VALUES(%s,%s,%s)",
            (user_id, login_name, hash_password(payload.password)),
        )
        db.execute(
            "INSERT INTO ee.security_audit_events(id,actor_id,subject_user_id,event_type,after_state,reason,request_id) VALUES(%s,%s,%s,'account.create',%s,%s,%s)",
            (uuid4(), actor["id"], user_id, Jsonb({"login_name": login_name, "display_name": payload.display_name, "department": payload.department}), "创建本地登录账号；默认不加入任何项目", request_id),
        )
        return AccountRead(id=user_id, login_name=login_name, display_name=payload.display_name, department=payload.department, state="active")


def update_account(subject: str, user_id: UUID, payload: AccountPatch, request_id: str) -> AccountRead:
    _require_admin(subject)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise ProjectReadError(422, "account_patch_empty", "至少提供一项账号修改")
    with write_transaction() as db:
        actor = db.execute("SELECT id FROM ee.app_users WHERE external_subject=%s AND state='active'", (subject,)).fetchone()
        if not actor:
            raise ProjectReadError(401, "not_authenticated", "当前账号不可用")
        current = db.execute("SELECT id,display_name,metadata,state FROM ee.app_users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
        credential = db.execute("SELECT login_name FROM ee.app_credentials WHERE user_id=%s", (user_id,)).fetchone()
        if not current or not credential:
            raise ProjectReadError(404, "account_not_found", "账号不存在")
        display_name = changes.get("display_name", current["display_name"])
        metadata = dict(current["metadata"] or {})
        if "department" in changes:
            metadata["department"] = changes["department"]
        state = changes.get("state", current["state"])
        db.execute("UPDATE ee.app_users SET display_name=%s,metadata=%s,state=%s,updated_at=clock_timestamp() WHERE id=%s", (display_name, Jsonb(metadata), state, user_id))
        db.execute(
            "INSERT INTO ee.security_audit_events(id,actor_id,subject_user_id,event_type,after_state,reason,request_id) VALUES(%s,%s,%s,'account.update',%s,%s,%s)",
            (uuid4(), actor["id"], user_id, Jsonb({"display_name": display_name, "department": metadata.get("department"), "state": state}), "修改本地账号资料或状态", request_id),
        )
        return AccountRead(id=user_id, login_name=credential["login_name"], display_name=display_name, department=str(metadata.get("department") or ""), state=state)
