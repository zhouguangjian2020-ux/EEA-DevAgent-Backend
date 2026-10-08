"""Stage 4 contracts; these tests never contact a database."""
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.main import app
from app.core.security import create_session_token
from app.schemas.deletion import BulkDeleteRequest, BulkDeleteSelection, BulkDeleteItem
from app.services import bulk_deletion
from tests.test_deletion import HEADERS


def item(**changes):
    return {"id": str(uuid4()), "kind": "FR", **changes}


@pytest.mark.parametrize("items", [[], [item(kind="project")], [item(kind="subsystem")], [item(force=True)], [item()] * 2, [item() for _ in range(201)]])
def test_batch_selection_validation(items):
    with pytest.raises(ValidationError): BulkDeleteSelection(items=items)


@pytest.mark.parametrize("version", [0, True, "-1", "01", "1.0", " 1", "9223372036854775808"])
def test_batch_version_validation(version):
    with pytest.raises(ValidationError): BulkDeleteItem(**item(), base_lock_version=version)


def test_batch_requires_every_version_and_mutation():
    with pytest.raises(ValidationError): BulkDeleteRequest(items=[item()], mutation_id=uuid4())
    with pytest.raises(ValidationError): BulkDeleteRequest(items=[item(base_lock_version="0")])
    with pytest.raises(ValidationError): BulkDeleteRequest(items=[item(base_lock_version="0")], mutation_id=uuid4(), cascade=True)
    assert BulkDeleteItem(**item(), base_lock_version="9007199254740993").base_lock_version == "9007199254740993"


@pytest.mark.parametrize("suffix", ["", "/impact"])
def test_batch_transport_security(suffix, monkeypatch):
    monkeypatch.setattr(bulk_deletion, "delete_batch", lambda *a: pytest.fail("Invalid write reached service"))
    monkeypatch.setattr(bulk_deletion, "get_impact", lambda *a: pytest.fail("Invalid preview reached service"))
    body = {"items": [item()]}
    if not suffix: body = {"mutation_id": str(uuid4()), "items": [item(base_lock_version="0")]}
    with TestClient(app) as c:
        url = f"/v1/projects/{uuid4()}/deletion-batch" + suffix
        assert c.post(url, json=body, headers=HEADERS).status_code == 401
        c.cookies.set("ts_session", create_session_token("engineer01"))
        assert c.post(url, json=body).status_code == 403
        assert c.post(url, json=body, headers={**HEADERS, "Origin": "https://invalid.example"}).status_code == 403
        assert c.post(url, json=body, headers={**HEADERS, "Content-Type": "text/plain"}).status_code == 415
        assert c.post(url, json={**body, "force": True}, headers=HEADERS).status_code == 422


def test_batch_has_distinct_impact_and_transaction_paths():
    paths = app.openapi()["paths"]
    assert "post" in paths["/v1/projects/{project_id}/deletion-batch"]
    assert "post" in paths["/v1/projects/{project_id}/deletion-batch/impact"]
    assert not any("restore" in path or "purge" in path for path in paths)
