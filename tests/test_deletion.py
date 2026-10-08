"""Single-object deletion contract and request security; never contacts a database."""
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.main import app
from app.core.config import settings
from app.core.security import create_session_token
from app.schemas.deletion import DeleteRequest
from app.services import deletion
from app.services.projects import ProjectReadError

HEADERS={"Origin":"http://127.0.0.1:3000","X-Project-Request":"1","Content-Type":"application/json"}

@pytest.mark.parametrize("value",[0,True,"-1","01","1.0"," 1","1 ","9223372036854775808",None])
def test_delete_requires_exact_bigint_string(value):
    with pytest.raises(ValidationError): DeleteRequest(mutation_id=uuid4(),base_lock_version=value)


def test_delete_request_forbids_cascade_and_extra_fields():
    with pytest.raises(ValidationError): DeleteRequest(mutation_id=uuid4(),base_lock_version="0",cascade=True)
    assert DeleteRequest(mutation_id=uuid4(),base_lock_version="9007199254740993").base_lock_version=="9007199254740993"


@pytest.mark.parametrize("suffix",["","/functions/"+str(uuid4()),"/artifacts/"+str(uuid4())])
def test_delete_security_and_transport(suffix,monkeypatch):
    monkeypatch.setattr(deletion,"delete_one",lambda *a:pytest.fail("Invalid request reached deletion service"))
    url="/v1/projects/"+str(uuid4())+suffix
    params={"mutation_id":str(uuid4()),"base_lock_version":"0"}
    with TestClient(app) as c:
        assert c.delete(url,headers=HEADERS,params=params).status_code==401
        c.cookies.set("ts_session",create_session_token(settings.demo_username))
        assert c.delete(url,params=params).status_code==403
        assert c.delete(url,headers={**HEADERS,"Origin":"https://untrusted.invalid"},params=params).status_code==403
        assert c.delete(url,headers={**HEADERS,"Content-Type":"text/plain"},params=params).status_code==415
        assert c.delete(url,headers=HEADERS,params={**params,"force":"true"}).status_code==422
        assert c.delete(url,headers=HEADERS,params={**params,"mutation_id":"not-uuid"}).status_code==422
        assert c.delete(url,headers=HEADERS,params={**params,"base_lock_version":"01"}).status_code==422
        assert c.request("DELETE",url,headers=HEADERS,params=params,json={}).status_code==422
        assert c.delete(url,headers=HEADERS,params=list(params.items())+[("mutation_id",str(uuid4()))]).status_code==422


def test_delete_conflict_request_id_and_no_store(monkeypatch):
    def reject(*a): raise ProjectReadError(409,"deletion_blocked","存在下游依赖")
    monkeypatch.setattr(deletion,"delete_one",reject)
    with TestClient(app) as c:
        c.cookies.set("ts_session",create_session_token(settings.demo_username))
        r=c.delete("/v1/projects/"+str(uuid4()),headers=HEADERS,params={"mutation_id":str(uuid4()),"base_lock_version":"0"})
        assert r.status_code==409 and r.json()['detail']['code']=='deletion_blocked'
        assert r.json()['detail']['request_id']==r.headers['X-Request-ID']
        assert r.headers['Cache-Control']=='no-store'


def test_single_delete_preserved_no_restore_or_purge_contract():
    paths=app.openapi()['paths']
    assert {path for path, operations in paths.items() if 'delete' in operations} == {
        '/v1/projects/{project_id}',
        '/v1/projects/{project_id}/functions/{function_id}',
        '/v1/projects/{project_id}/artifacts/{artifact_id}',
        '/v1/projects/{project_id}/members/{user_id}',
        '/v1/projects/{project_id}/functions/{function_id}/permissions/{user_id}',
    }
    assert not any(word in path for path in paths for word in ('restore','purge'))
    operation=paths['/v1/projects/{project_id}/artifacts/{artifact_id}']['delete']
    assert 'requestBody' not in operation
