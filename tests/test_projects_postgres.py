"""Opt-in integration tests: a dedicated local fixture only, never a company DB."""
import os
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from app.core.config import settings
from app.core.database import project_connection
from app.main import app
from app.services.projects import list_projects

PID='10d4c8a0-ff3a-56c3-abf6-18dc3b531e62'
FID='29fe01ea-f727-58cf-a326-b75294ba4dbb'


@pytest.fixture
def db(monkeypatch):
    admin_dsn=os.environ.get('TEST_PROJECTS_ADMIN_DSN')
    reader_dsn=os.environ.get('TEST_PROJECTS_READER_DSN')
    if not admin_dsn or not reader_dsn:
        pytest.skip('Requires explicit isolated local PostgreSQL project fixture')
    for dsn in [admin_dsn,reader_dsn]:
        info=conninfo_to_dict(dsn)
        assert info.get('host')=='127.0.0.1' and info.get('port')=='56428'
        assert info.get('dbname','').startswith('ee_project_read_')
    monkeypatch.setattr(settings,'projects_database_url',SecretStr(reader_dsn))
    with psycopg.connect(admin_dsn,autocommit=True) as connection:
        yield connection


@pytest.fixture
def client(db):
    with TestClient(app) as c:
        response=c.post('/v1/auth/login',json={'username':'engineer01','password':'change-me-before-demo'})
        assert response.status_code==200
        yield c


def test_real_renamed_project_list_and_detail(client,db):
    r=client.get('/v1/projects')
    assert r.status_code==200
    assert len(r.json()['items'])==1 and not r.json()['has_more']
    project=r.json()['items'][0]
    assert project['id']==PID and project['name']=='测试项目1'
    assert client.get('/v1/projects/'+PID).json()==project
    assert db.execute('SELECT name FROM ee.business_functions WHERE id=%s',(FID,)).fetchone()[0]=='行车记录仪DVR'
    assert db.execute('SELECT count(*) FROM ee.business_functions WHERE project_id=%s',(PID,)).fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM ee.artifacts WHERE project_id=%s AND function_id=%s',(PID,FID)).fetchone()[0]==32


def test_real_workspace_reads_all_sample_content(client, db):
    response = client.get('/v1/projects/' + PID + '/workspace')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    data = response.json()
    assert data['project'] == client.get('/v1/projects/' + PID).json()
    assert data['read_only'] is True and data['source'] == 'database'
    assert len(data['functions']) == 1
    function = data['functions'][0]
    assert function['id'] == FID and function['name'] == '行车记录仪DVR'
    assert function['prd']['source'] == 'database'
    assert function['prd']['markdown_body']
    assert len(function['use_cases']) == 3
    assert len(function['functional_requirements']) == 14
    assert len(function['subsystems']) == 43
    assert len(function['fr_allocations']) == 14
    assert sum(len(fr['sources']) for fr in function['functional_requirements']) == 16
    for subsystem in function['subsystems']:
        assert isinstance(subsystem['design_version_no'], int)
        assert subsystem['design_version_no'] > 0
    target_ids = {target['subsystem_id'] for allocation in function['fr_allocations'] for target in allocation['targets']}
    assert target_ids <= {subsystem['id'] for subsystem in function['subsystems']}
    for uc in function['use_cases']:
        assert uc['revision']['status'] == 'pending'
        rows = db.execute('SELECT kind,category,body FROM ee.uc_scenario_items WHERE uc_revision_id=%s', (uc['revision']['revision_id'],)).fetchall()
        bodies = {(kind, category): body for kind, category, body in rows}
        assert uc['branches'] == [bodies.get(('branch', key), '') for key in ('a1', 'a2', 'a3', 'a4')]
        assert uc['exceptions'] == [bodies.get(('exception', key), '') for key in ('vehicle_state', 'mechanical', 'environmental', 'misuse', 'power')]
    assert db.execute('SELECT count(*) FROM ee.artifacts WHERE project_id=%s AND accepted_revision_id IS NOT NULL', (PID,)).fetchone()[0] == 0


def test_real_pagination(client):
    assert client.get('/v1/projects?limit=1').json()['has_more'] is False
    assert client.get('/v1/projects?offset=1').json()['items']==[]


@pytest.mark.parametrize('target', ['user','member','role','permission','archived'])
def test_revocation_takes_effect_without_new_login(client,db,target):
    pairs={
      'user':("UPDATE ee.app_users SET state='disabled' WHERE external_subject='engineer01'", "UPDATE ee.app_users SET state='active' WHERE external_subject='engineer01'"),
      'member':("UPDATE ee.project_members SET state='disabled' WHERE user_id=(SELECT id FROM ee.app_users WHERE external_subject='engineer01')", "UPDATE ee.project_members SET state='active' WHERE user_id=(SELECT id FROM ee.app_users WHERE external_subject='engineer01')"),
      'role':("UPDATE ee.access_roles SET enabled=false WHERE code='project_api_reader'", "UPDATE ee.access_roles SET enabled=true WHERE code='project_api_reader'"),
      'permission':("UPDATE ee.permission_definitions SET enabled=false WHERE code='project.read'", "UPDATE ee.permission_definitions SET enabled=true WHERE code='project.read'"),
      'archived':("UPDATE ee.projects SET archived_at=now() WHERE code='XRAY-DVR-SAMPLE'", "UPDATE ee.projects SET archived_at=NULL WHERE code='XRAY-DVR-SAMPLE'"),
    }
    off,on=pairs[target]
    try:
        db.execute(off)
        assert client.get('/v1/projects').json()['items']==[]
        assert client.get('/v1/projects/'+PID).status_code==404
        assert client.get('/v1/projects/'+PID+'/workspace').status_code==404
    finally: db.execute(on)
    assert client.get('/v1/projects/'+PID).status_code==200


def test_project_isolation_even_when_member_without_permission(client,db):
    private=uuid4()
    actor=db.execute('SELECT created_by FROM ee.projects WHERE id=%s',(PID,)).fetchone()[0]
    user=db.execute("SELECT id FROM ee.app_users WHERE external_subject='engineer01'").fetchone()[0]
    try:
        db.execute('INSERT INTO ee.projects(id,code,name,created_by) VALUES(%s,%s,%s,%s)',(private,'PRIVATE-READ-TEST','not visible',actor))
        db.execute('INSERT INTO ee.project_members(project_id,user_id,added_by) VALUES(%s,%s,%s)',(private,user,actor))
        assert client.get('/v1/projects/'+str(private)).status_code==404
        assert [p['id'] for p in client.get('/v1/projects').json()['items']]==[PID]
        assert client.get('/v1/projects/'+str(uuid4())).status_code==404
    finally:
        db.execute('DELETE FROM ee.project_members WHERE project_id=%s',(private,))
        db.execute('DELETE FROM ee.projects WHERE id=%s',(private,))


def test_binding_removal_hides_project(client,db):
    record=db.execute("SELECT b.id,b.user_id,b.role_id,b.created_by,b.updated_by FROM ee.access_bindings b JOIN ee.app_users u ON u.id=b.user_id WHERE u.external_subject='engineer01' AND b.project_id=%s",(PID,)).fetchone()
    binding,user,role,creator,updater=record
    try:
        db.execute('DELETE FROM ee.access_bindings WHERE id=%s',(binding,))
        assert client.get('/v1/projects').json()['items']==[]
        assert client.get('/v1/projects/'+PID).status_code==404
        assert client.get('/v1/projects/'+PID+'/workspace').status_code==404
    finally:
        db.execute("INSERT INTO ee.access_bindings(id,project_id,user_id,scope_kind,role_id,created_by,updated_by) VALUES(%s,%s,%s,'project',%s,%s,%s)",(binding,PID,user,role,creator,updater))
    assert client.get('/v1/projects/'+PID).status_code==200


def test_parameterized_subject_cannot_inject(db):
    assert list_projects("engineer01' OR TRUE --",20,0).items==[]


def test_database_account_is_actually_read_only(db):
    dsn=settings.projects_database_url.get_secret_value()
    with psycopg.connect(dsn,autocommit=True) as ro:
        assert ro.execute('SHOW default_transaction_read_only').fetchone()[0]=='on'
        ro.execute('SET default_transaction_read_only=off')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute('UPDATE ee.projects SET name=name WHERE false')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute('SELECT * FROM ee.security_audit_events LIMIT 1')
    with project_connection() as ro:
        assert ro.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'


def test_superuser_database_connection_is_refused(client,monkeypatch):
    monkeypatch.setattr(settings,'projects_database_url',SecretStr(os.environ['TEST_PROJECTS_ADMIN_DSN']))
    r=client.get('/v1/projects')
    assert r.status_code==503 and r.json()['detail']['code']=='projects_unsafe_database_role'


def test_all_sample_content_still_unaccepted(client,db):
    client.get('/v1/projects');client.get('/v1/projects/'+PID)
    assert db.execute('SELECT count(*) FROM ee.artifacts WHERE project_id=%s AND accepted_revision_id IS NOT NULL',(PID,)).fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM ee.fr_uc_sources WHERE project_id=%s',(PID,)).fetchone()[0]==16
    assert db.execute('SELECT count(*) FROM ee.subsystem_version_items WHERE project_id=%s',(PID,)).fetchone()[0]==43
