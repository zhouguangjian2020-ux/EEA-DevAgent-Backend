"""Opt-in writer tests on a fresh local fixture only. Run AFTER reader integration tests; uses the migrated shared writer."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest

from app.core.config import settings
from app.core.security import create_session_token
from app.core.project_write_database import project_write_connection
from app.main import app
from app.schemas.projects import ProjectCreate
from app.services.project_creation import create_project
from app.services.projects import get_project, list_projects, ProjectReadError

HEADERS={"Origin":"http://127.0.0.1:3000","X-Project-Request":"1"}


@pytest.fixture
def db(monkeypatch):
    dsns=[os.getenv("TEST_PROJECTS_ADMIN_DSN"),os.getenv("TEST_PROJECTS_READER_DSN"),os.getenv("TEST_PROJECTS_WRITER_DSN")]
    if not all(dsns): pytest.skip("Requires dedicated isolated local PostgreSQL writer fixture")
    for dsn in dsns:
        info=conninfo_to_dict(dsn)
        assert info.get("host")=="127.0.0.1" and info.get("port")=="56428"
        assert info.get("dbname","").startswith("ee_project_read_")
    monkeypatch.setattr(settings,"projects_database_url",SecretStr(dsns[1]))
    monkeypatch.setattr(settings,"projects_write_database_url",SecretStr(dsns[2]))
    monkeypatch.setattr(settings,"projects_create_subjects",["engineer01"])
    with psycopg.connect(dsns[0],autocommit=True) as connection:
        assert connection.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0] != '7686454253092706426'
        yield connection


@pytest.fixture
def client(db):
    with TestClient(app) as c:
        c.cookies.set("ts_session",create_session_token("engineer01"))
        yield c


def payload(**kwargs): return {"creation_id":str(uuid4()),"name":"真实新增项目测试","description":"多行说明\n数据库持久化",**kwargs}


def test_atomic_five_rows_and_empty_workspace(client,db):
    data=payload(); r=client.post('/v1/projects',headers=HEADERS,json=data)
    assert r.status_code==201,r.text
    project=r.json();pid=project['id']
    assert project['code']=='PRJ-'+pid.replace('-','').upper() and project['lock_version']=='0'
    assert client.get('/v1/projects/'+pid).json()==project
    assert pid in [p['id'] for p in client.get('/v1/projects?limit=100').json()['items']]
    assert client.get('/v1/projects').json()['capabilities']['can_create'] is True
    workspace=client.get('/v1/projects/'+pid+'/workspace').json()
    assert workspace['functions']==[] and workspace['read_only'] is True
    actor=db.execute("SELECT id FROM ee.app_users WHERE external_subject='engineer01'").fetchone()[0]
    assert db.execute('SELECT created_by FROM ee.projects WHERE id=%s',(pid,)).fetchone()[0]==actor
    for table in ['project_members','access_bindings','project_subsystem_designs','security_audit_events']:
        assert db.execute(f'SELECT count(*) FROM ee.{table} WHERE project_id=%s',(pid,)).fetchone()[0]==1
    assert db.execute('SELECT current_version_id FROM ee.project_subsystem_designs WHERE project_id=%s',(pid,)).fetchone()[0] is None
    for table in ['business_functions','artifacts','subsystems','subsystem_design_versions']:
        assert db.execute(f'SELECT count(*) FROM ee.{table} WHERE project_id=%s',(pid,)).fetchone()[0]==0
    # A fresh Cookie/client still reads persisted data.
    with TestClient(app) as another:
        another.cookies.set('ts_session',create_session_token('engineer01'))
        assert another.get('/v1/projects/'+pid).json()['name']==data['name']


def test_duplicate_name_different_ids_and_stable_replay(client,db):
    data=payload(name='同名项目'); first=client.post('/v1/projects',headers=HEADERS,json=data)
    assert first.status_code==201
    repeat=client.post('/v1/projects',headers=HEADERS,json=data)
    assert repeat.status_code==200 and repeat.json()==first.json()
    different=client.post('/v1/projects',headers=HEADERS,json={**data,'creation_id':str(uuid4())})
    assert different.status_code==201 and different.json()['code']!=first.json()['code']
    assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE project_id=%s',(data['creation_id'],)).fetchone()[0]==1
    assert client.post('/v1/projects',headers=HEADERS,json={**data,'name':'changed'}).status_code==409


def test_concurrent_retries_create_once(db):
    data=ProjectCreate(**payload())
    with ThreadPoolExecutor(max_workers=5) as executor:
        results=list(executor.map(lambda i:create_project('engineer01',data,'concurrency-'+str(i)),range(5)))
    assert sum(created for _,created in results)==1
    assert len({p.id for p,_ in results})==1
    assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE project_id=%s',(data.creation_id,)).fetchone()[0]==1


def test_last_insert_failure_rolls_back_all(client,db):
    data=payload()
    db.execute("ALTER TABLE ee.security_audit_events ADD CONSTRAINT isolated_create_failure CHECK (event_type<>'project.create') NOT VALID")
    try:
        r=client.post('/v1/projects',headers=HEADERS,json=data)
        assert r.status_code==503
        assert db.execute('SELECT count(*) FROM ee.projects WHERE id=%s',(data['creation_id'],)).fetchone()[0]==0
        for table in ['project_members','access_bindings','project_subsystem_designs','security_audit_events']:
            assert db.execute(f'SELECT count(*) FROM ee.{table} WHERE project_id=%s',(data['creation_id'],)).fetchone()[0]==0
    finally: db.execute('ALTER TABLE ee.security_audit_events DROP CONSTRAINT isolated_create_failure')
    assert client.post('/v1/projects',headers=HEADERS,json=data).status_code==201


def test_active_user_without_projects_can_create_but_cannot_read_others(db,monkeypatch):
    subject='isolated-'+uuid4().hex; user=uuid4()
    db.execute('INSERT INTO ee.app_users(id,external_subject,display_name) VALUES(%s,%s,%s)',(user,subject,subject))
    monkeypatch.setattr(settings,'projects_create_subjects',[subject,'engineer01'])
    before=list_projects(subject,20,0)
    assert before.items==[] and before.capabilities.can_create
    mine,_=create_project(subject,ProjectCreate(**payload()),'other-subject')
    assert get_project(subject,mine.id).id==mine.id
    with pytest.raises(ProjectReadError) as e: get_project('engineer01',mine.id)
    assert e.value.status==404
    # Knowing UUID does not authorize a replay or disclose a project.
    with pytest.raises(ProjectReadError) as e:
        create_project('engineer01',ProjectCreate(creation_id=mine.id,name=mine.name,description=mine.description),'wrong-actor')
    assert e.value.status==409


def test_inactive_or_missing_mapping_denied(db,monkeypatch):
    for state in ['missing','disabled']:
        subject='isolated-'+uuid4().hex
        if state=='disabled': db.execute("INSERT INTO ee.app_users(id,external_subject,display_name,state) VALUES(%s,%s,%s,'disabled')",(uuid4(),subject,subject))
        monkeypatch.setattr(settings,'projects_create_subjects',[subject])
        assert list_projects(subject,20,0).capabilities.can_create is False
        with pytest.raises(ProjectReadError) as error:create_project(subject,ProjectCreate(**payload()),'denied')
        assert error.value.status==403


def test_revoked_membership_or_archived_project_cannot_be_replayed(client,db):
    data=payload();assert client.post('/v1/projects',headers=HEADERS,json=data).status_code==201
    db.execute("UPDATE ee.project_members SET state='disabled' WHERE project_id=%s",(data['creation_id'],))
    assert client.post('/v1/projects',headers=HEADERS,json=data).status_code==409
    assert client.get('/v1/projects/'+data['creation_id']).status_code==404
    data=payload();assert client.post('/v1/projects',headers=HEADERS,json=data).status_code==201
    db.execute('UPDATE ee.projects SET archived_at=now() WHERE id=%s',(data['creation_id'],))
    assert client.post('/v1/projects',headers=HEADERS,json=data).status_code==409


def test_writer_never_elevates_reader_or_writes_other_tables(db):
    with psycopg.connect(settings.projects_write_database_url.get_secret_value(),autocommit=True) as c:
        for statement in ['UPDATE ee.projects SET name=name WHERE false','DELETE FROM ee.projects WHERE false',
                          'UPDATE ee.business_functions SET name=name WHERE false',
                          "INSERT INTO ee.app_users(id) VALUES (NULL)",
                          'INSERT INTO ee.role_permissions(role_id) VALUES (NULL)',
                          'UPDATE ee.artifacts SET accepted_revision_id=NULL WHERE false','CREATE TABLE ee.isolated_forbidden(id int)',
                          'SELECT * FROM ee.security_audit_events LIMIT 0']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute(statement)
    with psycopg.connect(settings.projects_database_url.get_secret_value(),autocommit=True) as ro:
        ro.execute('SET default_transaction_read_only=off')
        with pytest.raises(psycopg.errors.InsufficientPrivilege): ro.execute('INSERT INTO ee.projects(id) VALUES(NULL)')
    with project_write_connection() as c:
        assert c.execute('SELECT current_user').fetchone()['current_user']=='ee_project_api_rw'
        c.execute('SELECT * FROM ee.artifacts LIMIT 0')


def test_unsafe_and_reader_dsn_rejected(client,db,monkeypatch):
    for dsn in [os.environ['TEST_PROJECTS_ADMIN_DSN'],os.environ['TEST_PROJECTS_READER_DSN']]:
        monkeypatch.setattr(settings,'projects_write_database_url',SecretStr(dsn))
        r=client.post('/v1/projects',headers=HEADERS,json=payload())
        assert r.status_code==503 and r.json()['detail']['code']=='project_create_unsafe_database_role'


def test_lock_timeout_is_bounded_and_same_request_retry_works(client,db):
    data=payload()
    db.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))',(data['creation_id'],))
    try:
        r=client.post('/v1/projects',headers=HEADERS,json=data)
        assert r.status_code==504
    finally: db.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(data['creation_id'],))
    assert client.post('/v1/projects',headers=HEADERS,json=data).status_code==201
