"""Runs only against the disposable local manual-work fixture."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest

from app.core.config import settings
from app.core.security import create_session_token
from app.main import app

HEADERS={"Origin":"http://127.0.0.1:3000","X-Project-Request":"1"}
PROJECT="10d4c8a0-ff3a-56c3-abf6-18dc3b531e62"


@pytest.fixture
def db(monkeypatch):
    dsns=[os.getenv("TEST_MANUAL_ADMIN_DSN"),os.getenv("TEST_MANUAL_READER_DSN"),os.getenv("TEST_MANUAL_WRITER_DSN")]
    if not all(dsns): pytest.skip("Requires isolated manual-work PostgreSQL")
    for dsn in dsns:
        info=conninfo_to_dict(dsn)
        ports = {"56429", "56431"} if os.getenv("TEST_TASK_RECORDS_ENABLED") == "1" else {"56429"}
        assert info.get("host")=="127.0.0.1" and info.get("port") in ports and info.get("dbname","").startswith("ee_manual_")
    monkeypatch.setattr(settings,"projects_database_url",SecretStr(dsns[1]))
    monkeypatch.setattr(settings,"projects_write_database_url",SecretStr(dsns[2]))
    monkeypatch.setattr(settings,"business_writes_enabled",True)
    monkeypatch.setattr(settings,"projects_create_subjects",["engineer01"])
    with psycopg.connect(dsns[0],autocommit=True) as c:
        assert c.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]!='7686454253092706426'
        yield c


@pytest.fixture
def client(db):
    with TestClient(app) as c:
        c.cookies.set("ts_session",create_session_token("engineer01"))
        yield c


def post(client,url,data,status=201):
    r=client.post('/v1/projects/'+PROJECT+url,headers=HEADERS,json=data)
    assert r.status_code==status,r.text
    return r.json()


def new_function(client):
    return post(client,'/functions',{"creation_id":str(uuid4()),"name":"AEB-"+uuid4().hex[:8],"description":""})['function_id']


def create(client,fid,kind,**fields):
    return post(client,f'/functions/{fid}/artifacts',{"creation_id":str(uuid4()),"kind":kind,**fields})


def accept(client,result,status=201):
    return post(client,f'/artifacts/{result["artifact_id"]}/accept',{"mutation_id":str(uuid4()),"revision_id":result["revision_id"]},status)


def test_full_manual_lifecycle(client,db):
    fid=new_function(client)
    ws=client.get('/v1/projects/'+PROJECT+'/workspace').json()
    f=next(x for x in ws['functions'] if x['id']==fid)
    assert f['prd'] is None and not f['use_cases'] and not f['functional_requirements'] and not f['fr_allocations']
    assert ws['can_create_function'] and f['can_create_prd']
    prd=create(client,fid,'PRD',name='AEB PRD')
    assert db.execute('SELECT accepted_revision_id FROM ee.artifacts WHERE id=%s',(prd['artifact_id'],)).fetchone()[0] is None
    post(client,f'/functions/{fid}/artifacts',{"creation_id":str(uuid4()),"kind":"PRD","name":"duplicate"},409)
    uc_payload={"creation_id":str(uuid4()),"kind":"UC","name":"停车","prd_revision_id":prd['revision_id']}
    post(client,f'/functions/{fid}/artifacts',uc_payload,409)
    accept(client,prd)
    uc=post(client,f'/functions/{fid}/artifacts',uc_payload)
    accept(client,uc,409)  # name-only draft lacks description and main flow
    saved=post(client,f'/artifacts/{uc["artifact_id"]}/revisions',{"mutation_id":str(uuid4()),"base_revision_id":uc['revision_id'],"content":{"kind":"UC","name":"停车","description":"停车场景","main_flow":"驾驶员停车","prd_revision_id":prd['revision_id']}})
    accept(client,saved)
    fr=create(client,fid,'FR',description='制动需求')
    accept(client,fr,409)
    fr2=post(client,f'/artifacts/{fr["artifact_id"]}/revisions',{"mutation_id":str(uuid4()),"base_revision_id":fr['revision_id'],"content":{"kind":"FR","description":"制动需求","acceptance_criteria":"制动生效","sources":[{"uc_id":uc['artifact_id'],"uc_revision_id":saved['revision_id']}]}})
    accept(client,fr2)
    ws=client.get('/v1/projects/'+PROJECT+'/workspace').json()
    f=next(x for x in ws['functions'] if x['id']==fid)
    assert f['prd']['revision']['status']=='accepted'
    assert f['use_cases'][0]['revision']['status']=='accepted'
    assert len(f['accepted_use_cases'])==1
    assert f['functional_requirements'][0]['revision']['status']=='accepted'
    assert f['functional_requirements'][0]['acceptance_criteria']=='制动生效'
    # Revisions are immutable and creation stayed pending until explicitly accepted.
    assert db.execute('SELECT count(*) FROM ee.artifact_revisions WHERE artifact_id=%s',(uc['artifact_id'],)).fetchone()[0]==2


def test_idempotence_concurrency_and_conflicts(client,db):
    fid=new_function(client)
    payload={"creation_id":str(uuid4()),"kind":"FR","description":"唯一草稿"}
    first=post(client,f'/functions/{fid}/artifacts',payload)
    repeat=post(client,f'/functions/{fid}/artifacts',payload,200)
    assert first['artifact_id']==repeat['artifact_id']
    post(client,f'/functions/{fid}/artifacts',{**payload,'description':'different'},409)
    save={"mutation_id":str(uuid4()),"base_revision_id":first['revision_id'],"content":{"kind":"FR","description":"新正文","sources":[]}}
    second=post(client,f'/artifacts/{first["artifact_id"]}/revisions',save)
    post(client,f'/artifacts/{first["artifact_id"]}/revisions',save,200)
    post(client,f'/artifacts/{first["artifact_id"]}/revisions',{**save,'mutation_id':str(uuid4())},409)
    assert second['revision_id']!=first['revision_id']
    def concurrent():
        with TestClient(app) as c:
            c.cookies.set('ts_session',create_session_token('engineer01'))
            return c.post('/v1/projects/'+PROJECT+f'/functions/{fid}/artifacts',headers=HEADERS,json={"creation_id":str(uuid4()),"kind":"PRD","name":"race"}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _:concurrent(),range(2)))==[201,409]


def test_cross_function_and_stale_sources(client,db):
    a,b=new_function(client),new_function(client)
    p=create(client,a,'PRD',name='P');accept(client,p)
    post(client,f'/functions/{b}/artifacts',{"creation_id":str(uuid4()),"kind":"UC","name":"invalid","prd_revision_id":p['revision_id']},409)
    u=create(client,a,'UC',name='U',description='D',main_flow='F',prd_revision_id=p['revision_id'])
    p2=post(client,f'/artifacts/{p["artifact_id"]}/revisions',{"mutation_id":str(uuid4()),"base_revision_id":p['revision_id'],"content":{"kind":"PRD","markdown_body":"# P2"}})
    accept(client,u)  # pending upstream revision doesn't invalidate current accepted source
    accept(client,p2)
    assert db.execute('SELECT accepted_revision_id FROM ee.artifacts WHERE id=%s',(u['artifact_id'],)).fetchone()[0] is not None
    post(client,f'/functions/{a}/artifacts',{"creation_id":str(uuid4()),"kind":"UC","name":"stale","prd_revision_id":p['revision_id']},409)
    post(client,f'/functions/{b}/artifacts',{"creation_id":str(uuid4()),"kind":"FR","description":"bad","sources":[{"uc_id":u['artifact_id'],"uc_revision_id":u['revision_id']}]},409)


def test_permission_denial_and_new_project_author(client,db,monkeypatch):
    r=client.post('/v1/projects',headers=HEADERS,json={"creation_id":str(uuid4()),"name":"new author"})
    assert r.status_code==201,r.text
    project=r.json()['id']
    assert client.get('/v1/projects/'+project+'/workspace').json()['can_create_function']
    role=db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_author'").fetchone()[0]
    db.execute('DELETE FROM ee.access_bindings WHERE project_id=%s AND role_id=%s',(project,role))
    assert client.post('/v1/projects/'+project+'/functions',headers=HEADERS,json={"creation_id":str(uuid4()),"name":"forbidden"}).status_code==404


def test_atomic_rollback_and_least_privilege(client,db):
    fid=new_function(client);creation=str(uuid4())
    db.execute("ALTER TABLE ee.security_audit_events ADD CONSTRAINT isolated_manual_fail CHECK(event_type<>'fr.create') NOT VALID")
    try:
        post(client,f'/functions/{fid}/artifacts',{"creation_id":creation,"kind":"FR","description":"rollback"},409)
        assert db.execute('SELECT count(*) FROM ee.artifacts WHERE id=%s',(creation,)).fetchone()[0]==0
    finally:db.execute('ALTER TABLE ee.security_audit_events DROP CONSTRAINT isolated_manual_fail')
    with psycopg.connect(settings.projects_write_database_url.get_secret_value(),autocommit=True) as w:
        for sql in ['DELETE FROM ee.artifacts WHERE false','UPDATE ee.artifacts SET accepted_revision_id=NULL WHERE false','UPDATE ee.permission_definitions SET enabled=false WHERE false','CREATE TABLE ee.forbidden(id int)']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):w.execute(sql)
    with psycopg.connect(settings.projects_database_url.get_secret_value(),autocommit=True) as r:
        r.execute('SET default_transaction_read_only=off')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):r.execute('INSERT INTO ee.business_functions(id) VALUES(NULL)')


def test_stale_references_can_be_saved_but_not_accepted(client, db):
    fid = new_function(client)
    p = create(client, fid, 'PRD', name='P'); accept(client, p)
    body = {"kind": "UC", "name": "U", "description": "D", "main_flow": "F", "prd_revision_id": p['revision_id']}
    u = create(client, fid, **body); accept(client, u)
    sources = [{"uc_id": u['artifact_id'], "uc_revision_id": u['revision_id']}]
    f = create(client, fid, 'FR', description='D', sources=sources); accept(client, f)
    p2 = post(client, f'/artifacts/{p["artifact_id"]}/revisions', {"mutation_id": str(uuid4()), "base_revision_id": p['revision_id'], "content": {"kind": "PRD", "markdown_body": "new"}})
    accept(client, p2)
    u2 = post(client, f'/artifacts/{u["artifact_id"]}/revisions', {"mutation_id": str(uuid4()), "base_revision_id": u['revision_id'], "content": body})
    accept(client, u2, 409)
    assert db.execute('SELECT accepted_revision_id FROM ee.artifacts WHERE id=%s', (u['artifact_id'],)).fetchone()[0] == UUID(u['revision_id'])
    u3 = post(client, f'/artifacts/{u["artifact_id"]}/revisions', {"mutation_id": str(uuid4()), "base_revision_id": u2['revision_id'], "content": {**body, "prd_revision_id": p2['revision_id']}})
    accept(client, u3)
    f2 = post(client, f'/artifacts/{f["artifact_id"]}/revisions', {"mutation_id": str(uuid4()), "base_revision_id": f['revision_id'], "content": {"kind": "FR", "description": "new", "sources": sources}})
    accept(client, f2, 409)
    assert db.execute('SELECT accepted_revision_id FROM ee.artifacts WHERE id=%s', (f['artifact_id'],)).fetchone()[0] == UUID(f['revision_id'])


def test_reader_member_cannot_write_and_revoked_capability_stays_denied(client, db):
    member = db.execute("SELECT id FROM ee.app_users WHERE external_subject='engineer01'").fetchone()[0]
    role = db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_reader'").fetchone()[0]
    project = client.post('/v1/projects', headers=HEADERS, json={"creation_id": str(uuid4()), "name": "reader only"}).json()['id']
    db.execute('UPDATE ee.access_bindings SET role_id=%s WHERE project_id=%s AND user_id=%s', (role, project, member))
    workspace = client.get('/v1/projects/' + project + '/workspace').json()
    assert not workspace['can_create_function']
    assert client.post('/v1/projects/' + project + '/functions', headers=HEADERS, json={"creation_id": str(uuid4()), "name": "forbidden"}).status_code == 403


def test_concurrent_revision_saves_have_one_winner(client, db):
    fid = new_function(client)
    f = create(client, fid, 'FR', description='base')
    def save(_):
        with TestClient(app) as c:
            c.cookies.set('ts_session', create_session_token('engineer01'))
            return c.post('/v1/projects/' + PROJECT + f'/artifacts/{f["artifact_id"]}/revisions', headers=HEADERS, json={"mutation_id": str(uuid4()), "base_revision_id": f['revision_id'], "content": {"kind": "FR", "description": "parallel", "sources": []}}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, range(2))) == [201, 409]
