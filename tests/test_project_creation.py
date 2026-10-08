from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import psycopg
import pytest

from app.api.routes import projects as routes
from app.core.config import settings
from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole
from app.core.security import create_session_token
from app.main import app
from app.schemas.projects import ProjectCreate, ProjectRead
from app.services import project_creation as service

HEADERS = {"Origin": "http://127.0.0.1:3000", "X-Project-Request": "1"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "projects_write_database_url", SecretStr(""))
    monkeypatch.setattr(settings, "projects_create_subjects", ["engineer01"])
    with TestClient(app) as c:
        c.cookies.set("ts_session", create_session_token("engineer01"))
        yield c


def body(**kwargs):
    return {"creation_id": str(uuid4()), "name": "新增项目中文", "description": None, **kwargs}


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": "   "}, {"name": "中"*101}, {"description": "文"*2001},
    {"creation_id": "not-a-uuid"}, {"creation_id": "10d4c8a0-ff3a-56c3-abf6-18dc3b531e62"},
    {"name": 123}, {"name": None}, {"description": 123}, {"name": "abc\x00def"},
    {"created_by": str(uuid4())}, {"metadata": {}}, {"role_id": str(uuid4())}, {"code": "INJECTED"},
])
def test_invalid_payload_never_reaches_service(client, monkeypatch, changes):
    monkeypatch.setattr(routes, "create_project", lambda *a: pytest.fail("invalid payload reached service"))
    assert client.post("/v1/projects", headers=HEADERS, json=body(**changes)).status_code == 422


def test_normalization_and_unicode_limits():
    value = ProjectCreate(**body(name="  中😀  ", description=" \n "))
    assert value.name == "中😀" and value.description is None
    assert len(ProjectCreate(**body(name="😀"*100, description="中"*2000)).name) == 100
    with pytest.raises(ValidationError): ProjectCreate(**body(description="\x00"))


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "http://127.0.0.1:3000"}, {"X-Project-Request": "1"},
    {**HEADERS, "Origin": "null"}, {**HEADERS, "Origin": "https://evil.invalid"},
    {**HEADERS, "Origin": "http://127.0.0.1:3000.evil.invalid"}, {**HEADERS, "X-Project-Request": "0"},
])
def test_csrf_precedes_write(client, monkeypatch, headers):
    monkeypatch.setattr(routes, "create_project", lambda *a: pytest.fail("forbidden origin reached service"))
    r = client.post("/v1/projects", headers=headers, json=body())
    assert r.status_code == 403 and r.headers["Cache-Control"] == "no-store"


def test_auth_precedes_write(client, monkeypatch):
    client.cookies.clear()
    monkeypatch.setattr(routes, "create_project", lambda *a: pytest.fail("unauthenticated write"))
    assert client.post("/v1/projects", headers=HEADERS, json=body()).status_code == 401


def test_json_only(client):
    assert client.post("/v1/projects", headers={**HEADERS, "Content-Type": "text/plain"}, content="{}").status_code == 415


@pytest.mark.parametrize("created,expected", [(True,201), (False,200)])
def test_success_and_replay_contract(client, monkeypatch, created, expected):
    payload = body()
    called = []
    def create(subject, value, request_id):
        called.append((subject, value, request_id))
        return ProjectRead(id=value.creation_id, code="PRJ-X", name=value.name, description=None,
                           lock_version="0", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)), created
    monkeypatch.setattr(routes, "create_project", create)
    r=client.post("/v1/projects", headers=HEADERS, json=payload)
    assert r.status_code == expected and r.json()["id"] == payload["creation_id"]
    assert r.headers["Location"] == "/v1/projects/"+payload["creation_id"]
    assert called[0][0] == "engineer01" and called[0][2] == r.headers["X-Request-ID"]


def test_allowlist_denies_before_connection(client, monkeypatch):
    monkeypatch.setattr(settings, "projects_create_subjects", [])
    monkeypatch.setattr(service, "project_write_connection", lambda: pytest.fail("forbidden subject reached DB"))
    assert client.post("/v1/projects", headers=HEADERS, json=body()).status_code == 403


@pytest.mark.parametrize("error,status,code", [
    (ProjectDatabaseNotConfigured(),503,"project_create_not_configured"),
    (UnsafeProjectDatabaseRole(),503,"project_create_unsafe_database_role"),
    (psycopg.OperationalError("password=secret private SQL"),503,"project_create_unavailable"),
    (psycopg.errors.QueryCanceled("private"),504,"project_create_timeout"),
    (psycopg.errors.LockNotAvailable("private"),504,"project_create_timeout"),
    (psycopg.errors.UniqueViolation("private"),409,"project_creation_conflict"),
])
def test_sanitized_write_errors(client,monkeypatch,error,status,code):
    def fail(): raise error
    monkeypatch.setattr(service,"project_write_connection",fail)
    r=client.post("/v1/projects",headers=HEADERS,json=body())
    assert r.status_code==status and r.json()["detail"]["code"]==code
    assert r.json()["detail"]["request_id"]==r.headers["X-Request-ID"]
    assert "secret" not in r.text and "private" not in r.text


def test_write_settings_default_closed():
    from app.core.config import Settings
    s=Settings(_env_file=None, projects_create_subjects="", projects_write_database_url="")
    assert not s.projects_create_subjects and not s.projects_write_database_url.get_secret_value()
