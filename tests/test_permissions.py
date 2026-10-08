from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.schemas.permissions import AccountCreate, FunctionPermissionUpsert
from app.services import account_admin, business_access, permission_admin
from app.services.projects import ProjectReadError


PROJECT_ID = uuid4()
FUNCTION_ID = uuid4()
ACTOR_ID = uuid4()
MEMBER_ID = uuid4()


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def execute(self, statement, params=()):
        self.calls.append((statement, params))
        for marker, response in self.responses.items():
            if marker in statement:
                return response
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])


@contextmanager
def fake_transaction(db):
    yield db


def test_permission_routes_require_login():
    with TestClient(app) as client:
        assert client.get("/v1/accounts").status_code == 401
        assert client.get(f"/v1/projects/{PROJECT_ID}/permissions").status_code == 401


def test_account_listing_is_not_available_to_non_global_admin(monkeypatch):
    monkeypatch.setattr(settings, "account_admin_subjects", [])
    with TestClient(app) as client:
        client.cookies.set("ts_session", create_session_token(settings.demo_username))
        response = client.get("/v1/accounts")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "account_manage_forbidden"


def test_permission_writes_keep_origin_and_json_guards(monkeypatch):
    monkeypatch.setattr(permission_admin, "upsert_member", lambda *args: pytest.fail("service must not run"))
    with TestClient(app) as client:
        client.cookies.set("ts_session", create_session_token(settings.demo_username))
        response = client.post(
            f"/v1/projects/{PROJECT_ID}/members",
            json={"user_id": str(MEMBER_ID), "role_code": "project_api_reader"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_write_origin_forbidden"


def test_function_scope_capability_uses_function_id(monkeypatch):
    monkeypatch.setattr(settings, "business_writes_enabled", True)
    monkeypatch.setattr(settings, "projects_write_database_url", SecretStr("postgresql://isolated.invalid/db"))
    db = RecordingDB(responses={"SELECT DISTINCT": SimpleNamespace(fetchall=lambda: [{"capability": "function.update"}])})

    result = business_access.capabilities(db, "subject-1", PROJECT_ID, FUNCTION_ID)

    assert result == {"function.update"}
    statement, params = db.calls[0]
    assert "b.function_id=%s" in statement
    assert params == (PROJECT_ID, "subject-1", FUNCTION_ID)


def test_remove_member_revokes_function_permissions(monkeypatch):
    db = RecordingDB(
        responses={
            "SELECT state FROM ee.project_members": SimpleNamespace(fetchone=lambda: {"state": "active"}),
            "SELECT 1 FROM ee.projects": SimpleNamespace(fetchone=lambda: None),
            "DELETE FROM ee.access_bindings": SimpleNamespace(fetchall=lambda: [{"id": uuid4()}, {"id": uuid4()}]),
        }
    )
    monkeypatch.setattr(permission_admin, "_require_manager", lambda *_args: ACTOR_ID)
    monkeypatch.setattr(permission_admin, "write_transaction", lambda: fake_transaction(db))

    permission_admin.remove_member("subject-1", PROJECT_ID, MEMBER_ID, "request-1")

    delete_calls = [(statement, params) for statement, params in db.calls if "DELETE FROM ee.access_bindings" in statement]
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == (PROJECT_ID, MEMBER_ID)
    audit_call = db.calls[-1]
    assert "project.member.remove" in audit_call[0]
    audit_state = next(item.obj for item in audit_call[1] if hasattr(item, "obj"))
    assert audit_state["function_permissions_removed"] == 2


def test_function_permission_requires_project_membership(monkeypatch):
    db = RecordingDB(
        responses={
            "SELECT 1 FROM ee.business_functions": SimpleNamespace(fetchone=lambda: {"id": FUNCTION_ID}),
            "SELECT 1 FROM ee.project_members": SimpleNamespace(fetchone=lambda: None),
        }
    )
    monkeypatch.setattr(permission_admin, "_require_manager", lambda *_args: ACTOR_ID)
    monkeypatch.setattr(permission_admin, "_ensure_active_user", lambda *_args: {"id": MEMBER_ID, "state": "active"})
    monkeypatch.setattr(permission_admin, "write_transaction", lambda: fake_transaction(db))

    with pytest.raises(ProjectReadError) as error:
        permission_admin.upsert_function_permission(
            "subject-1",
            PROJECT_ID,
            FUNCTION_ID,
            FunctionPermissionUpsert(user_id=MEMBER_ID, role_code="function_editor"),
            "request-1",
        )
    assert error.value.status == 409
    assert error.value.code == "project_member_required"


def test_account_creation_has_no_project_membership_or_plaintext_password(monkeypatch):
    db = RecordingDB(
        responses={
            "SELECT id FROM ee.app_users WHERE external_subject": SimpleNamespace(fetchone=lambda: {"id": ACTOR_ID}),
            "SELECT 1 FROM ee.app_credentials": SimpleNamespace(fetchone=lambda: None),
        }
    )
    monkeypatch.setattr(settings, "account_admin_subjects", [settings.demo_username])
    monkeypatch.setattr(account_admin, "hash_password", lambda _password: "argon2id$test-hash-value-0123456789")
    monkeypatch.setattr(account_admin, "write_transaction", lambda: fake_transaction(db))

    result = account_admin.create_account(
        settings.demo_username,
        AccountCreate(
            login_name="NewEngineer",
            display_name="新工程师",
            department="电子电气架构部",
            password="not-written-to-audit",
        ),
        "request-1",
    )

    assert result.login_name == "newengineer"
    statements = [statement for statement, _params in db.calls]
    assert not any("project_members" in statement for statement in statements)
    audit_statement, audit_params = db.calls[-1]
    assert "account.create" in audit_statement
    audit_state = next(item.obj for item in audit_params if hasattr(item, "obj"))
    assert "password" not in audit_state
    assert "not-written-to-audit" not in repr(audit_state)

