"""Stage-1 metadata contracts and authenticated HTTP boundary, no external DB."""
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError, SecretStr
from app.main import app
from app.core.config import settings
from app.core.security import create_session_token
from app.schemas.metadata_edit import MetadataPatch
from app.services import metadata_edit, projects
from app.services.projects import ProjectReadError

HEADERS = {"Origin": "http://127.0.0.1:3000", "X-Project-Request": "1"}


def payload(**fields):
    return {"mutation_id": str(uuid4()), "base_lock_version": "0", **fields}


@pytest.mark.parametrize("fields", [{}, {"name": None}, {"name": " "}, {"name": "a"*101}, {"description": "a"*2001}, {"name": 1}, {"description": False}, {"name": "a\x00"}, {"description": "\x00"}, {"name": "ok", "id": str(uuid4())}, {"name": "ok", "created_by": str(uuid4())}, {"name": "ok", "project_id": str(uuid4())}, {"name": "ok", "code": "x"}, {"name": "ok", "base_lock_version": 0}, {"name": "ok", "base_lock_version": "-1"}, {"name": "ok", "base_lock_version": "01"}, {"name": "ok", "base_lock_version": "9223372036854775808"}])
def test_patch_rejects_invalid_and_immutable_fields(fields):
    with pytest.raises(ValidationError): MetadataPatch.model_validate(payload(**fields))


def test_partial_fields_normalization_and_bigint():
    p = MetadataPatch.model_validate(payload(name=" 名称 ", base_lock_version="9007199254740993"))
    assert p.name == "名称" and "description" not in p.model_fields_set
    for description in (None, "", "   "):
        p = MetadataPatch.model_validate(payload(description=description))
        assert p.description is None and "name" not in p.model_fields_set
    assert MetadataPatch.model_validate(payload(name="字"*100, description="字"*2000)).name == "字"*100


@pytest.mark.parametrize("suffix", ["", "/functions/" + str(uuid4())])
def test_patch_security_guards(suffix, monkeypatch):
    monkeypatch.setattr(metadata_edit, "_update", lambda *args: pytest.fail("Invalid request reached DB"))
    with TestClient(app) as c:
        url = "/v1/projects/" + str(uuid4()) + suffix
        assert c.patch(url, headers=HEADERS, json=payload(name="ok")).status_code == 401
        c.cookies.set("ts_session", create_session_token(settings.demo_username))
        assert c.patch(url, json=payload(name="ok")).status_code == 403
        assert c.patch(url, headers={**HEADERS,"Origin":"https://untrusted.invalid"}, json=payload(name="ok")).status_code == 403
        assert c.patch(url, headers={**HEADERS,"Content-Type":"text/plain"}, content="{}").status_code == 415
        assert c.patch(url, headers=HEADERS, json=payload(name=" ")).status_code == 422


def test_patch_failure_has_request_id_without_raw_database_error(monkeypatch):
    def fail(*args): raise ProjectReadError(409, "business_write_conflict", "资料版本冲突")
    monkeypatch.setattr(metadata_edit,"update_project",fail)
    with TestClient(app) as c:
        c.cookies.set("ts_session", create_session_token(settings.demo_username))
        r = c.patch("/v1/projects/"+str(uuid4()), headers=HEADERS, json=payload(name="ok"))
        assert r.status_code == 409 and r.json()["detail"]["request_id"] == r.headers["X-Request-ID"]
        assert r.headers["Cache-Control"] == "no-store"


def test_project_read_capability_is_masked_when_writes_disabled(monkeypatch):
    from tests.test_projects import row
    record = {**row(), "can_update": True}
    monkeypatch.setattr(settings,"business_writes_enabled",False)
    assert not projects.project_read(record).can_update
    monkeypatch.setattr(settings,"business_writes_enabled",True)
    monkeypatch.setattr(settings,"projects_write_database_url",SecretStr(""))
    assert not projects.project_read(record).can_update
    monkeypatch.setattr(settings,"projects_write_database_url",SecretStr("isolated-test-not-a-dsn"))
    assert projects.project_read(record).can_update
