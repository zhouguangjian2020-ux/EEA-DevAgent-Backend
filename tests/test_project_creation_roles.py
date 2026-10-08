"""Role compatibility tests: fake database only, no network or real writes."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from pydantic import SecretStr
from app.core.config import settings
from app.schemas.projects import ProjectCreate
from app.services import project_creation as service
from app.services.projects import ProjectReadError

BASE = {"project.read", "function.create"} | {
    f"{k}.{a}" for k in ("prd", "uc", "fr") for a in ("create", "update", "accept")}
META = BASE | {"project.update", "function.update"}
DELETE = META | {f"{k}.archive" for k in ("project", "function", "prd", "uc", "fr", "fr_allocation")}
MANAGED = DELETE | {"project.manage_access"}

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(settings, "business_writes_enabled", True)
    monkeypatch.setattr(settings, "projects_create_subjects", ["engineer01"])
    monkeypatch.setattr(settings, "projects_write_database_url", SecretStr("postgresql://unused.invalid/test"))
    def prepare(codes, enabled=True):
        db = SimpleNamespace(calls=[], permissions=[], actor=uuid4(), role=uuid4(), selected=None)
        for code in sorted(codes):
            resource, action = code.split(".", 1)
            scopes = ["project"]
            if "project.manage_access" in codes and (resource in {"prd", "uc", "fr"} and action != "archive" or code == "function.update"):
                scopes.append("function")
            db.permissions.append(dict(code=code, resource_kind=resource.upper(), action=action, allowed_scopes=scopes, enabled=True))
        def execute(sql, params=()):
            db.calls.append((sql, params))
            one, many = None, []
            if sql.startswith("SELECT id FROM ee.app_users"):
                one = {"id": db.actor}
            elif sql.startswith("SELECT pg_advisory_xact_lock") or sql.startswith("SELECT created_by"):
                pass
            elif sql.startswith("SELECT id FROM ee.access_roles"):
                db.selected = params[0]
                one = {"id": db.role} if enabled else None
            elif "FROM ee.role_permissions rp JOIN" in sql:
                many = db.permissions
            elif sql.startswith("INSERT INTO ee.projects("):
                now = datetime.now(timezone.utc)
                one = dict(id=params[0], code=params[1], name=params[2], description=params[3], lock_version="0", created_at=now, updated_at=now)
            elif not sql.startswith("INSERT INTO ee."):
                raise AssertionError("Unexpected SQL: " + sql)
            return SimpleNamespace(fetchone=lambda: one, fetchall=lambda: many)
        db.execute = execute
        @contextmanager
        def connection():
            yield db
        monkeypatch.setattr(service, "project_write_connection", connection)
        return db, ProjectCreate(creation_id=uuid4(), name="Isolated role regression")
    return prepare

@pytest.mark.parametrize("codes", [BASE, META, DELETE, META | {"project.manage_access"}, MANAGED],
    ids=["manual", "metadata", "deletion", "metadata-permissions", "deletion-permissions"])
def test_reviewed_author_sets_are_accepted(setup, codes):
    db, payload = setup(codes)
    project, created = service.create_project("engineer01", payload, "test-role")
    assert created and project.id == payload.creation_id and db.selected == "project_api_author"
    assert project.can_update == ("project.update" in codes)
    assert project.can_delete == ("project.archive" in codes)
    assert project.can_manage_access == ("project.manage_access" in codes)
    writes = [(sql, args) for sql, args in db.calls if sql.startswith("INSERT")]
    assert len(writes) == 5
    binding = next(args for sql, args in writes if sql.startswith("INSERT INTO ee.access_bindings"))
    assert binding[1:4] == (payload.creation_id, db.actor, db.role)
    assert not any(sql.startswith(("UPDATE", "DELETE", "GRANT", "ALTER")) for sql, _ in db.calls)

@pytest.mark.parametrize("codes", [MANAGED | {"project.super_admin"}, MANAGED - {"prd.accept"},
    MANAGED - {"uc.archive"}, MANAGED - {"function.update"}, BASE | {"project.manage_access"}],
    ids=["unknown-extra", "missing-core", "partial-deletion", "partial-metadata", "missing-prerequisite"])
def test_invalid_sets_are_rejected_before_insert(setup, codes):
    db, payload = setup(codes)
    with pytest.raises(ProjectReadError) as error:
        service.create_project("engineer01", payload, "test-role")
    assert error.value.code == "project_create_role_not_ready"
    assert not any(sql.startswith("INSERT") for sql, _ in db.calls)

@pytest.mark.parametrize("changes", [{"enabled": False}, {"allowed_scopes": ["function"]},
    {"resource_kind": "function"}, {"action": "update"}])
def test_invalid_manage_permission_is_rejected(setup, changes):
    db, payload = setup(MANAGED)
    next(p for p in db.permissions if p["code"] == "project.manage_access").update(changes)
    with pytest.raises(ProjectReadError) as error:
        service.create_project("engineer01", payload, "test-role")
    assert error.value.code == "project_create_role_not_ready"
    assert not any(sql.startswith("INSERT") for sql, _ in db.calls)

def test_disabled_role_remains_rejected(setup):
    db, payload = setup(MANAGED, enabled=False)
    with pytest.raises(ProjectReadError):
        service.create_project("engineer01", payload, "test-role")
    assert not any(sql.startswith("INSERT") for sql, _ in db.calls)

def test_reader_mode_remains_restricted(setup, monkeypatch):
    monkeypatch.setattr(settings, "business_writes_enabled", False)
    db, payload = setup({"project.read"})
    project, created = service.create_project("engineer01", payload, "test-role")
    assert created and db.selected == "project_api_reader"
    assert not project.can_update and not project.can_delete and not project.can_manage_access
    db, payload = setup({"project.read", "project.manage_access"})
    with pytest.raises(ProjectReadError):
        service.create_project("engineer01", payload, "test-role")
    assert not any(sql.startswith("INSERT") for sql, _ in db.calls)
