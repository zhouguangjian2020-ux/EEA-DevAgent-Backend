"""Only run after the stage-1 increment in a disposable localhost cluster."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import psycopg
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings
from app.core.security import create_session_token
from tests.test_manual_work_postgres import db, client, HEADERS


@pytest.fixture(autouse=True)
def metadata_increment_required():
    if os.environ.get("TEST_METADATA_ENABLED") != "1": pytest.skip("Requires isolated metadata increment")


@pytest.fixture
def resource(client):
    r = client.post('/v1/projects', headers=HEADERS, json={"creation_id":str(uuid4()), "name":"资料编辑测试", "description":"旧简介"})
    assert r.status_code == 201, r.text
    p = r.json()
    f = client.post('/v1/projects/'+p['id']+'/functions', headers=HEADERS, json={"creation_id":str(uuid4()),"name":"原功能","description":"旧描述"}).json()['function_id']
    return p, f


def patch(client, url, version="0", mid=None, **fields):
    return client.patch(url, headers=HEADERS, json={"mutation_id":mid or str(uuid4()), "base_lock_version":version, **fields})


@pytest.mark.parametrize("kind", ["project", "function"])
def test_metadata_roundtrip_and_exact_replay(client, db, resource, kind):
    p,f = resource
    url='/v1/projects/'+p['id'] + (('/functions/'+f) if kind=='function' else '')
    mid=str(uuid4())
    r=patch(client,url,mid=mid,name=' 新名称 ')
    assert r.status_code==200,r.text
    first=r.json()
    assert first['name']=='新名称' and first['description']==('旧描述' if kind=='function' else '旧简介')
    assert first['can_update'] and first['lock_version']=='1'
    assert patch(client,url,mid=mid,name=' 新名称 ').json()==first
    assert patch(client,url,mid=mid,name='不同').status_code==409
    assert patch(client,url,name='过期').status_code==409
    assert patch(client,url,version='1',description=' ').json()['description'] is None
    # A late retry returns its original result and cannot overwrite a newer update.
    assert patch(client,url,mid=mid,name=' 新名称 ').json()==first
    row=db.execute('SELECT before_state,after_state FROM ee.security_audit_events WHERE id=%s',(mid,)).fetchone()
    assert row[1]['result']==first and row[0]['resource']['lock_version']=='0'
    assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE id=%s',(mid,)).fetchone()[0]==1
    ws=client.get('/v1/projects/'+p['id']+'/workspace').json()
    current=next(x for x in ws['functions'] if x['id']==f) if kind=='function' else ws['project']
    assert current['lock_version']=='2' and current['can_update'] and current['description'] is None
    assert client.get('/v1/projects/'+p['id']).json()['can_update']
    # Earlier suites may create more than one page; do not assume this project is on page one.
    offset=0;listed=None
    while listed is None:
        page=client.get('/v1/projects',params={'limit':100,'offset':offset}).json()
        listed=next((x for x in page['items'] if x['id']==p['id']),None)
        if not page['has_more']:break
        offset+=page['limit']
    assert listed is not None and listed['can_update']


def test_function_edit_leaves_prd_and_revisions_unchanged(client, db, resource):
    p,f=resource; url='/v1/projects/'+p['id']+'/functions/'+f
    prd=client.post(url+'/artifacts',headers=HEADERS,json={"creation_id":str(uuid4()),"kind":"PRD","name":"旧功能历史正文"}).json()
    before=db.execute('SELECT row_to_json(t)::text FROM ee.prd_versions t WHERE prd_id=%s',(prd['artifact_id'],)).fetchone()[0]
    assert patch(client,url,name='新功能',description='新描述').status_code==200
    assert db.execute('SELECT row_to_json(t)::text FROM ee.prd_versions t WHERE prd_id=%s',(prd['artifact_id'],)).fetchone()[0]==before
    assert db.execute('SELECT count(*) FROM ee.artifact_revisions WHERE artifact_id=%s',(prd['artifact_id'],)).fetchone()[0]==1


def test_cross_project_reader_and_replay_after_revocation(client,db,resource):
    p,f=resource; base='/v1/projects/'+p['id']; url=base+'/functions/'+f
    mid=str(uuid4()); assert patch(client,url,mid=mid,name='已保存').status_code==200
    assert patch(client,'/v1/projects/'+str(uuid4())+'/functions/'+f,name='跨项目').status_code==404
    assert patch(client,base+'/functions/'+str(uuid4()),name='不存在').status_code==404
    rid=db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_reader'").fetchone()[0]
    db.execute('UPDATE ee.access_bindings SET role_id=%s WHERE project_id=%s',(rid,p['id']))
    assert not client.get(base).json()['can_update']
    assert not client.get(base+'/workspace').json()['functions'][0]['can_update']
    assert patch(client,url,mid=mid,name='已保存').status_code==403
    assert patch(client,base,name='读者不能改').status_code==403
    db.execute("DELETE FROM ee.access_bindings WHERE project_id=%s",(p['id'],))
    assert patch(client,base,name='已失效').status_code==404


def test_deleted_ancestor_or_target_cannot_update(client,db,resource):
    p,f=resource; base='/v1/projects/'+p['id']
    db.execute('UPDATE ee.business_functions SET archived_at=now() WHERE id=%s',(f,))
    assert patch(client,base+'/functions/'+f,name='不能修改').status_code==404
    db.execute('UPDATE ee.projects SET archived_at=now() WHERE id=%s',(p['id'],))
    assert patch(client,base,name='不能修改').status_code==404


def test_audit_failure_rolls_back_metadata_and_retry_is_safe(client,db,resource):
    p,f=resource; mid=str(uuid4()); url='/v1/projects/'+p['id']+'/functions/'+f
    db.execute("ALTER TABLE ee.security_audit_events ADD CONSTRAINT metadata_test_failure CHECK(event_type<>'function.update') NOT VALID")
    try:
        assert patch(client,url,mid=mid,name='回滚').status_code==409
        assert db.execute('SELECT name,lock_version FROM ee.business_functions WHERE id=%s',(f,)).fetchone()==('原功能',0)
        assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE id=%s',(mid,)).fetchone()[0]==0
    finally: db.execute('ALTER TABLE ee.security_audit_events DROP CONSTRAINT metadata_test_failure')
    assert patch(client,url,mid=mid,name='回滚').status_code==200


def test_concurrent_updates_and_same_request_serialization(client,db,resource):
    p,f=resource; url='/v1/projects/'+p['id']+'/functions/'+f
    def update(mid, version, name):
        with TestClient(app) as c:
            c.cookies.set('ts_session',create_session_token('engineer01'))
            return patch(c,url,version=version,mid=mid,name=name).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda name:update(str(uuid4()),'0',name),['first','second']))
    assert sorted(results)==[200,409]
    mid=str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:update(mid,'1','same'),range(2)))
    assert results==[200,200]
    assert db.execute('SELECT lock_version FROM ee.business_functions WHERE id=%s',(f,)).fetchone()[0]==2
    assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE id=%s',(mid,)).fetchone()[0]==1


def test_mutation_id_cross_target_and_bigint(client,db,resource):
    p,f=resource; base='/v1/projects/'+p['id']; mid=str(uuid4())
    assert patch(client,base,mid=mid,name='项目更新').status_code==200
    assert patch(client,base+'/functions/'+f,mid=mid,name='项目更新').status_code==409
    db.execute('UPDATE ee.business_functions SET lock_version=9007199254740993 WHERE id=%s',(f,))
    r=patch(client,base+'/functions/'+f,version='9007199254740993',name='大版本')
    assert r.status_code==200 and r.json()['lock_version']=='9007199254740994'


def test_stage1_column_privileges(client,db,resource):
    for dsn,reader in [(settings.projects_write_database_url.get_secret_value(),False),(settings.projects_database_url.get_secret_value(),True)]:
        with psycopg.connect(dsn,autocommit=True) as c:
            c.execute('SET default_transaction_read_only=off')
            statements=['UPDATE ee.projects SET code=code WHERE false','UPDATE ee.business_functions SET project_id=project_id WHERE false','UPDATE ee.business_functions SET archived_at=NULL WHERE false','DELETE FROM ee.projects WHERE false','UPDATE ee.security_audit_events SET before_state=NULL WHERE false','UPDATE ee.permission_definitions SET enabled=false WHERE false']
            if os.environ.get('TEST_DELETION_ENABLED') == '1' and not reader:
                statements.remove('UPDATE ee.business_functions SET archived_at=NULL WHERE false')
            if reader: statements+=['UPDATE ee.projects SET name=name WHERE false','UPDATE ee.business_functions SET description=NULL WHERE false']
            for statement in statements:
                with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute(statement)
            if not reader:
                c.execute('UPDATE ee.projects SET name=name,description=description,lock_version=lock_version WHERE false')
                c.execute('UPDATE ee.business_functions SET name=name,description=description,lock_version=lock_version WHERE false')


def test_accessible_other_project_is_not_a_function_alias(client,db,resource):
    p,f=resource
    other=client.post('/v1/projects',headers=HEADERS,json={"creation_id":str(uuid4()),"name":"其他可访问项目"}).json()['id']
    assert patch(client,'/v1/projects/'+other+'/functions/'+f,name='越界').status_code==404
    assert db.execute('SELECT name FROM ee.business_functions WHERE id=%s',(f,)).fetchone()[0]=='原功能'


def test_author_can_edit_objects_created_by_someone_else(client,db,resource):
    p,f=resource
    another=db.execute("SELECT id FROM ee.app_users WHERE external_subject<>'engineer01' LIMIT 1").fetchone()[0]
    db.execute('UPDATE ee.projects SET created_by=%s WHERE id=%s',(another,p['id']))
    db.execute('UPDATE ee.business_functions SET created_by=%s WHERE id=%s',(another,f))
    assert patch(client,'/v1/projects/'+p['id'],name='协作维护').status_code==200
    assert patch(client,'/v1/projects/'+p['id']+'/functions/'+f,name='协作功能').status_code==200


def test_lock_timeout_does_not_partially_update(client,db,resource):
    p,f=resource
    with psycopg.connect(os.environ['TEST_MANUAL_ADMIN_DSN']) as blocker:
        blocker.execute('SELECT id FROM ee.projects WHERE id=%s FOR UPDATE',(p['id'],))
        r=patch(client,'/v1/projects/'+p['id']+'/functions/'+f,name='锁超时')
        assert r.status_code==504 and r.json()['detail']['code']=='business_write_timeout'
    assert db.execute('SELECT name,lock_version FROM ee.business_functions WHERE id=%s',(f,)).fetchone()==('原功能',0)


def test_project_creation_refuses_partial_metadata_permission_upgrade(client,db):
    role=db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_author'").fetchone()[0]
    db.execute("DELETE FROM ee.role_permissions WHERE role_id=%s AND permission_code='function.update'",(role,))
    try:
        r=client.post('/v1/projects',headers=HEADERS,json={"creation_id":str(uuid4()),"name":"不完整授权"})
        assert r.status_code==503 and r.json()['detail']['code']=='project_create_role_not_ready'
    finally:
        db.execute("INSERT INTO ee.role_permissions(role_id,permission_code) VALUES(%s,'function.update')",(role,))
