from uuid import uuid4
from contextlib import contextmanager

from fastapi.testclient import TestClient
from pydantic import ValidationError
import psycopg
import pytest

from app.core.config import settings
from app.core.security import create_session_token
from app.main import app
from app.schemas.manual_work import FunctionCreate, UcCreate, FrCreate, PrdCreate, WriteResult
from app.services import manual_work as service
from app.api.routes import manual_work as routes

HEADERS={"Origin":"http://127.0.0.1:3000","X-Project-Request":"1"}
PROJECT=str(uuid4()); FUNCTION=str(uuid4()); ARTIFACT=str(uuid4())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings,"business_writes_enabled",True)
    with TestClient(app) as c:
        c.cookies.set("ts_session",create_session_token("engineer01"))
        yield c


def test_defaults_and_strict_input():
    f=FunctionCreate(creation_id=uuid4(),name="  AEB  ")
    assert f.name=="AEB" and f.description==""
    uc=UcCreate(kind="UC",creation_id=uuid4(),name="停车",prd_revision_id=uuid4())
    assert uc.description=="" and len(uc.branches)==4 and len(uc.exceptions)==5
    assert FrCreate(kind="FR",creation_id=uuid4(),description="需求").sources==[]
    for name in ("", " ", "a"*101, 123, "a\x00b"):
        with pytest.raises(ValidationError): FunctionCreate(creation_id=uuid4(),name=name)
    with pytest.raises(ValidationError): PrdCreate(kind="PRD",creation_id=uuid4(),name="ok",created_by=uuid4())
    with pytest.raises(ValidationError): FrCreate(kind="FR",creation_id=uuid4(),description=" ")
    src={"uc_id":uuid4(),"uc_revision_id":uuid4()}
    with pytest.raises(ValidationError): FrCreate(kind="FR",creation_id=uuid4(),description="ok",sources=[src,src])


@pytest.mark.parametrize("url,payload",[
    (f"/v1/projects/{PROJECT}/functions",{"creation_id":str(uuid4()),"name":"AEB"}),
    (f"/v1/projects/{PROJECT}/functions/{FUNCTION}/artifacts",{"creation_id":str(uuid4()),"kind":"PRD","name":"AEB"}),
    (f"/v1/projects/{PROJECT}/artifacts/{ARTIFACT}/revisions",{"mutation_id":str(uuid4()),"base_revision_id":str(uuid4()),"content":{"kind":"PRD","markdown_body":"# AEB"}}),
    (f"/v1/projects/{PROJECT}/artifacts/{ARTIFACT}/accept",{"mutation_id":str(uuid4()),"revision_id":str(uuid4())}),
])
def test_all_mutations_require_cookie_origin_and_json(client,monkeypatch,url,payload):
    monkeypatch.setattr(routes,"execute",lambda *a: pytest.fail("forbidden request reached service"))
    assert client.post(url,json=payload).status_code==403
    assert client.post(url,headers={**HEADERS,"Origin":"https://evil.invalid"},json=payload).status_code==403
    assert client.post(url,headers={**HEADERS,"Content-Type":"text/plain"},content="{}").status_code==415
    client.cookies.clear()
    assert client.post(url,headers=HEADERS,json=payload).status_code==401


def test_route_success_and_replay(client,monkeypatch):
    calls=[]
    def create(subject,project,payload,request_id):
        calls.append(subject)
        return WriteResult(function_id=payload.creation_id,created=len(calls)==1)
    monkeypatch.setattr(service,"create_function",create)
    body={"creation_id":str(uuid4()),"name":"AEB"}
    for status in (201,200):
        response=client.post(f"/v1/projects/{PROJECT}/functions",headers=HEADERS,json=body)
        assert response.status_code==status and response.json()["function_id"]==body["creation_id"]
        assert response.headers["cache-control"]=="no-store" and response.headers["x-request-id"]
    assert calls==["engineer01","engineer01"]


@pytest.mark.parametrize("error,status",[(psycopg.OperationalError("password=private"),503),(psycopg.errors.CheckViolation("private SQL"),409),(psycopg.errors.LockNotAvailable("private"),504)])
def test_sanitized_errors(client,monkeypatch,error,status):
    @contextmanager
    def connection():
        raise error
        yield
    monkeypatch.setattr(service,"project_write_connection",connection)
    response=client.post(f"/v1/projects/{PROJECT}/functions",headers=HEADERS,json={"creation_id":str(uuid4()),"name":"AEB"})
    assert response.status_code==status and "private" not in response.text


def test_default_closed(client,monkeypatch):
    monkeypatch.setattr(settings,"business_writes_enabled",False)
    monkeypatch.setattr(service,"project_write_connection",lambda: pytest.fail("disabled writer connected"))
    assert client.post(f"/v1/projects/{PROJECT}/functions",headers=HEADERS,json={"creation_id":str(uuid4()),"name":"AEB"}).status_code==503
