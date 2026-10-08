"""Stage-2 live PostgreSQL API tests, only via the disposable localhost fixture."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4
import psycopg
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.security import create_session_token
from app.core.config import settings
from tests.test_manual_work_postgres import db, client, HEADERS, PROJECT

DHEADERS={**HEADERS,"Content-Type":"application/json"}

@pytest.fixture(autouse=True)
def deletion_increment_required():
    if os.environ.get("TEST_DELETION_ENABLED")!="1": pytest.skip("Requires isolated stage-2 deletion increment")


def post(c,path,body,status=201):
    r=c.post('/v1/projects'+path,headers=HEADERS,json=body)
    assert r.status_code==status,r.text
    return r.json()

@pytest.fixture
def resource(client):
    p=post(client,'',{'creation_id':str(uuid4()),'name':'删除测试项目'})
    f=post(client,'/'+p['id']+'/functions',{'creation_id':str(uuid4()),'name':'删除测试功能'})['function_id']
    return p,f


def create(c,p,f,kind,**fields):
    return post(c,f'/{p}/functions/{f}/artifacts',{'creation_id':str(uuid4()),'kind':kind,**fields})


def accept(c,p,a):
    return post(c,f'/{p}/artifacts/{a["artifact_id"]}/accept',{'mutation_id':str(uuid4()),'revision_id':a['revision_id']})


def save_fr(c,p,a,sources):
    return post(c,f'/{p}/artifacts/{a["artifact_id"]}/revisions',{'mutation_id':str(uuid4()),'base_revision_id':a['revision_id'],'content':{'kind':'FR','description':'需求','sources':[{'uc_id':u['artifact_id'],'uc_revision_id':u['revision_id']} for u in sources]}})


def impact(c,path,status=200):
    r=c.get('/v1/projects'+path+'/deletion-impact')
    assert r.status_code==status,r.text
    return r.json()


def erase(c,path,version=None,mid=None,status=200):
    if version is None: version=impact(c,path)['target']['lock_version']
    r=c.delete('/v1/projects'+path,headers=DHEADERS,params={'mutation_id':mid or str(uuid4()),'base_lock_version':version})
    assert r.status_code==status,r.text
    return r.json()


def chain(c,p,f):
    prd=create(c,p,f,'PRD',name='PRD');accept(c,p,prd)
    uc=create(c,p,f,'UC',name='UC',description='场景',main_flow='流程',prd_revision_id=prd['revision_id']);accept(c,p,uc)
    fr=create(c,p,f,'FR',description='FR',sources=[{'uc_id':uc['artifact_id'],'uc_revision_id':uc['revision_id']}]);accept(c,p,fr)
    return prd,uc,fr


def test_dependency_chain_and_empty_parent_rules(client,db,resource):
    p,f=resource;pid=p['id'];prd,uc,fr=chain(client,pid,f)
    paths=[f'/{pid}',f'/{pid}/functions/{f}',f'/{pid}/artifacts/{prd["artifact_id"]}',f'/{pid}/artifacts/{uc["artifact_id"]}']
    for path in paths:
        info=impact(client,path)
        assert info['blocked'] and info['blocker_count']>0 and info['can_delete']
        assert erase(client,path,status=409)['detail']['code']=='deletion_blocked'
    counts=db.execute('SELECT count(*) FROM ee.artifact_revisions WHERE function_id=%s',(f,)).fetchone()[0]
    for a in (fr,uc,prd): erase(client,f'/{pid}/artifacts/{a["artifact_id"]}')
    erase(client,f'/{pid}/functions/{f}')
    erase(client,f'/{pid}')
    assert db.execute('SELECT count(*) FROM ee.artifact_revisions WHERE function_id=%s',(f,)).fetchone()[0]==counts
    assert client.get('/v1/projects/'+pid).status_code==404
    assert client.get('/v1/projects/'+pid+'/workspace').status_code==404
    assert pid not in {x['id'] for x in client.get('/v1/projects?limit=100').json()['items']}


def test_prd_recreation_new_identity_and_old_history(client,db,resource):
    p,f=resource;pid=p['id'];old=create(client,pid,f,'PRD',name='旧PRD');accept(client,pid,old)
    erase(client,f'/{pid}/artifacts/{old["artifact_id"]}')
    duplicate=client.post(f'/v1/projects/{pid}/functions/{f}/artifacts',headers=HEADERS,json={'creation_id':old['artifact_id'],'kind':'PRD','name':'旧PRD'})
    assert duplicate.status_code==409
    new=create(client,pid,f,'PRD',name='新PRD')
    assert new['artifact_id']!=old['artifact_id']
    rows=db.execute('SELECT id,archived_at,accepted_revision_id FROM ee.artifacts WHERE function_id=%s ORDER BY created_at',(f,)).fetchall()
    assert len(rows)==2 and rows[0][1] and rows[0][2] and rows[1][1] is None and rows[1][2] is None
    ws=client.get('/v1/projects/'+pid+'/workspace').json()['functions'][0]
    assert ws['prd']['id']==new['artifact_id'] and not ws['can_create_prd']
    assert client.post(f'/v1/projects/{pid}/functions/{f}/artifacts',headers=HEADERS,json={'creation_id':str(uuid4()),'kind':'UC','name':'非法旧PRD','prd_revision_id':old['revision_id']}).status_code==409
    accept(client,pid,new)
    fresh_uc=create(client,pid,f,'UC',name='新UC',prd_revision_id=new['revision_id'])
    assert db.execute('SELECT prd_id FROM ee.use_cases WHERE artifact_id=%s',(fresh_uc['artifact_id'],)).fetchone()[0]==UUID(new['artifact_id'])
    assert db.execute('SELECT count(*) FROM ee.prd_versions WHERE prd_id=%s',(old['artifact_id'],)).fetchone()[0]==1


def test_prd_create_concurrency_after_delete(client,db,resource):
    p,f=resource;pid=p['id'];old=create(client,pid,f,'PRD',name='旧');erase(client,f'/{pid}/artifacts/{old["artifact_id"]}')
    def create_one(_):
        with TestClient(app) as c:
            c.cookies.set('ts_session',create_session_token('engineer01'))
            return c.post(f'/v1/projects/{pid}/functions/{f}/artifacts',headers=HEADERS,json={'creation_id':str(uuid4()),'kind':'PRD','name':'并发PRD'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: assert sorted(pool.map(create_one,range(2)))==[201,409]
    assert db.execute("SELECT count(*) FROM ee.artifacts WHERE function_id=%s AND type_code='PRD' AND archived_at IS NULL",(f,)).fetchone()[0]==1


def test_current_head_and_accepted_block_but_old_history_does_not(client,db,resource):
    p,f=resource;pid=p['id'];prd,u1,fr1=chain(client,pid,f)
    u2=create(client,pid,f,'UC',name='UC2',description='场景2',main_flow='流程2',prd_revision_id=prd['revision_id']);accept(client,pid,u2)
    fr2=save_fr(client,pid,fr1,[u2])
    i1=impact(client,f'/{pid}/artifacts/{u1["artifact_id"]}');i2=impact(client,f'/{pid}/artifacts/{u2["artifact_id"]}')
    assert i1['blockers'][0]['reference_versions']==['accepted']
    assert i2['blockers'][0]['reference_versions']==['head']
    erase(client,f'/{pid}/artifacts/{u1["artifact_id"]}',status=409)
    accept(client,pid,fr2)
    assert not impact(client,f'/{pid}/artifacts/{u1["artifact_id"]}')['blocked']
    erase(client,f'/{pid}/artifacts/{u1["artifact_id"]}')
    assert db.execute('SELECT count(*) FROM ee.fr_uc_sources WHERE fr_revision_id=%s AND uc_id=%s',(fr1['revision_id'],u1['artifact_id'])).fetchone()[0]==1
    assert client.post(f'/v1/projects/{pid}/functions/{f}/artifacts',headers=HEADERS,json={'creation_id':str(uuid4()),'kind':'FR','description':'非法新引用','sources':[{'uc_id':u1['artifact_id'],'uc_revision_id':u1['revision_id']}]}).status_code==409


def allocation(db,f,fr,targets=False):
    aid,rid=uuid4(),uuid4()
    actor=db.execute("SELECT id FROM ee.app_users WHERE external_subject='engineer01'").fetchone()[0]
    tv=db.execute("SELECT id FROM ee.artifact_type_versions WHERE type_code='FR_ALLOCATION' AND retired_at IS NULL ORDER BY version_no DESC LIMIT 1").fetchone()[0]
    scheme=db.execute('SELECT current_version_id FROM ee.project_subsystem_designs WHERE project_id=%s',(PROJECT,)).fetchone()[0]
    with db.transaction():
        db.execute("INSERT INTO ee.artifacts(id,project_id,function_id,type_code,code,head_revision_id,created_by) VALUES(%s,%s,%s,'FR_ALLOCATION',%s,%s,%s)",(aid,PROJECT,f,'AL-'+aid.hex,rid,actor))
        db.execute('INSERT INTO ee.fr_allocations(artifact_id,project_id,function_id,fr_id) VALUES(%s,%s,%s,%s)',(aid,PROJECT,f,fr['artifact_id']))
        db.execute("INSERT INTO ee.artifact_revisions(id,project_id,function_id,artifact_id,type_code,revision_no,type_version_id,created_by) VALUES(%s,%s,%s,%s,'FR_ALLOCATION',1,%s,%s)",(rid,PROJECT,f,aid,tv,actor))
        db.execute('INSERT INTO ee.allocation_versions(revision_id,project_id,function_id,allocation_id,fr_id,fr_revision_id,design_version_id) VALUES(%s,%s,%s,%s,%s,%s,%s)',(rid,PROJECT,f,aid,fr['artifact_id'],fr['revision_id'],scheme))
        if targets:
            db.execute('INSERT INTO ee.allocation_targets(project_id,function_id,allocation_revision_id,design_version_id,subsystem_id) SELECT %s,%s,%s,%s,subsystem_id FROM ee.subsystem_version_items WHERE design_version_id=%s AND is_allocatable LIMIT 2',(PROJECT,f,rid,scheme,scheme))
    return str(aid),str(rid),actor


@pytest.mark.parametrize('targets,confirmed',[(False,False),(True,False),(True,True)])
def test_allocation_blocks_fr_including_empty_and_never_deletes_directory(client,db,targets,confirmed):
    f=post(client,'/'+PROJECT+'/functions',{'creation_id':str(uuid4()),'name':'分配删除测试-'+uuid4().hex[:8]})['function_id']
    prd,uc,fr=chain(client,PROJECT,f);aid,rid,actor=allocation(db,f,fr,targets)
    if confirmed:
        # Fixture-only admin grant inside one transaction; no new allocation accept API.
        with db.transaction():
            role=db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_author'").fetchone()[0]
            permission='test.fixture.allocation.accept'
            db.execute("INSERT INTO ee.permission_definitions(code,display_name,resource_kind,action,allowed_scopes) VALUES(%s,'Fixture only','FR_ALLOCATION','accept',ARRAY['project'])",(permission,))
            db.execute('INSERT INTO ee.role_permissions VALUES(%s,%s)',(role,permission))
            db.execute("INSERT INTO ee.review_events(id,project_id,function_id,artifact_id,revision_id,decision,actor_id,event_no,request_key) VALUES(%s,%s,%s,%s,%s,'accept',%s,1,%s)",(uuid4(),PROJECT,f,aid,rid,actor,str(uuid4())))
            db.execute('UPDATE ee.artifacts SET accepted_revision_id=%s WHERE id=%s',(rid,aid))
            db.execute('DELETE FROM ee.role_permissions WHERE role_id=%s AND permission_code=%s',(role,permission))
            db.execute('DELETE FROM ee.permission_definitions WHERE code=%s',(permission,))
    before=db.execute('SELECT count(*) FROM ee.subsystems WHERE project_id=%s',(PROJECT,)).fetchone()[0]
    i=impact(client,f'/{PROJECT}/artifacts/{fr["artifact_id"]}');assert i['blocked'] and i['blockers'][0]['id']==aid
    ainfo=impact(client,f'/{PROJECT}/artifacts/{aid}');assert ainfo['allocation_fr_id']==fr['artifact_id'] and not ainfo['blocked']
    assert bool(ainfo['allocation_targets'])==targets
    assert (ainfo['target']['accepted_revision_id']==rid)==confirmed
    if confirmed:
        assert all(t['reference_versions']==['head','accepted'] for t in ainfo['allocation_targets'])
    ws=client.get('/v1/projects/'+PROJECT+'/workspace').json()
    a=next(x for x in ws['functions'] if x['id']==f)['fr_allocations'][0]
    assert a['id']==aid and a['lock_version']==ainfo['target']['lock_version'] and a['can_delete']
    erase(client,f'/{PROJECT}/artifacts/{aid}')
    assert db.execute('SELECT archived_at FROM ee.artifacts WHERE id=%s',(fr['artifact_id'],)).fetchone()[0] is None
    assert db.execute('SELECT count(*) FROM ee.subsystems WHERE project_id=%s',(PROJECT,)).fetchone()[0]==before
    assert db.execute('SELECT count(*) FROM ee.allocation_versions WHERE allocation_id=%s',(aid,)).fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM ee.review_events WHERE artifact_id=%s',(aid,)).fetchone()[0]==int(confirmed)
    assert (db.execute('SELECT accepted_revision_id FROM ee.artifacts WHERE id=%s',(aid,)).fetchone()[0]==UUID(rid))==confirmed
    erase(client,f'/{PROJECT}/artifacts/{fr["artifact_id"]}')


def test_idempotent_delete_replay_and_revoked_permission(client,db,resource):
    p,f=resource;pid=p['id'];path=f'/{pid}/functions/{f}';mid=str(uuid4());first=erase(client,path,version='0',mid=mid)
    assert erase(client,path,version='0',mid=mid)==first
    assert erase(client,path,version='1',mid=mid,status=409)['detail']['code']=='business_write_conflict'
    assert erase(client,path,version='0',status=404)['detail']['code']=='delete_target_not_found'
    assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE id=%s',(mid,)).fetchone()[0]==1
    role=db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_author'").fetchone()[0]
    db.execute("DELETE FROM ee.role_permissions WHERE role_id=%s AND permission_code='function.archive'",(role,))
    try: assert erase(client,path,version='0',mid=mid,status=403)['detail']['code']=='business_write_forbidden'
    finally: db.execute("INSERT INTO ee.role_permissions VALUES(%s,'function.archive')",(role,))
    pmid=str(uuid4());r=erase(client,'/'+pid,version=p['lock_version'],mid=pmid)
    assert erase(client,'/'+pid,version=p['lock_version'],mid=pmid)==r
    assert impact(client,'/'+pid,status=404)['detail']['code']=='project_not_found'


def test_permission_and_cross_project_isolation(client,db,resource):
    p,f=resource;pid=p['id'];other=post(client,'',{'creation_id':str(uuid4()),'name':'另一个项目'})
    impact(client,f'/{other["id"]}/functions/{f}',404)
    erase(client,f'/{other["id"]}/functions/{f}',version='0',status=404)
    db.execute("UPDATE ee.access_bindings SET role_id=(SELECT id FROM ee.access_roles WHERE code='project_api_reader') WHERE project_id=%s",(pid,))
    assert not impact(client,f'/{pid}/functions/{f}')['can_delete']
    erase(client,f'/{pid}/functions/{f}',version='0',status=403)
    db.execute("UPDATE ee.project_members SET state='disabled' WHERE project_id=%s",(pid,))
    impact(client,f'/{pid}/functions/{f}',404)
    erase(client,f'/{pid}/functions/{f}',version='0',status=404)


def test_version_conflicts_and_new_dependency_after_preview(client,db,resource):
    p,f=resource;pid=p['id'];fr=create(client,pid,f,'FR',description='原文');path=f'/{pid}/artifacts/{fr["artifact_id"]}'
    info=impact(client,path)
    newer=save_fr(client,pid,fr,[])
    assert erase(client,path,version=info['target']['lock_version'],status=409)['detail']['code']=='deletion_version_conflict'
    accept(client,pid,create(client,pid,f,'PRD',name='P'))
    fn=impact(client,f'/{pid}/functions/{f}')
    assert fn['blocked']
    erase(client,f'/{pid}/functions/{f}',version=fn['target']['lock_version'],status=409)


def test_old_preview_rechecks_new_child(client,db,resource):
    p,f=resource;pid=p['id'];path=f'/{pid}/functions/{f}';old=impact(client,path);assert not old['blocked']
    create(client,pid,f,'FR',description='晚到的需求')
    assert erase(client,path,version=old['target']['lock_version'],status=409)['detail']['code']=='deletion_blocked'


def test_deleted_ancestors_block_creations_edits_replay_and_review(client,db,resource):
    p,f=resource;pid=p['id'];fr=create(client,pid,f,'FR',description='正文');erase(client,f'/{pid}/artifacts/{fr["artifact_id"]}')
    assert client.post(f'/v1/projects/{pid}/artifacts/{fr["artifact_id"]}/revisions',headers=HEADERS,json={'mutation_id':str(uuid4()),'base_revision_id':fr['revision_id'],'content':{'kind':'FR','description':'新','sources':[]}}).status_code==404
    assert client.post(f'/v1/projects/{pid}/artifacts/{fr["artifact_id"]}/accept',headers=HEADERS,json={'mutation_id':str(uuid4()),'revision_id':fr['revision_id']}).status_code==404
    erase(client,f'/{pid}/functions/{f}')
    assert client.post(f'/v1/projects/{pid}/functions/{f}/artifacts',headers=HEADERS,json={'creation_id':str(uuid4()),'kind':'PRD','name':'不得创建'}).status_code==404
    assert client.patch(f'/v1/projects/{pid}/functions/{f}',headers=HEADERS,json={'mutation_id':str(uuid4()),'base_lock_version':'0','name':'不得修改'}).status_code==404
    erase(client,'/'+pid)
    assert client.post(f'/v1/projects/{pid}/functions',headers=HEADERS,json={'creation_id':str(uuid4()),'name':'不得创建'}).status_code==404
    assert client.patch(f'/v1/projects/{pid}',headers=HEADERS,json={'mutation_id':str(uuid4()),'base_lock_version':'0','name':'不得修改'}).status_code==404


def test_deletion_audit_failure_rolls_back_and_retry(client,db,resource):
    p,f=resource;path=f'/{p["id"]}/functions/{f}';mid=str(uuid4())
    db.execute("ALTER TABLE ee.security_audit_events ADD CONSTRAINT deletion_test_failure CHECK(event_type<>'function.delete') NOT VALID")
    try:
        erase(client,path,version='0',mid=mid,status=409)
        assert db.execute('SELECT archived_at,lock_version FROM ee.business_functions WHERE id=%s',(f,)).fetchone()==(None,0)
        assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE id=%s',(mid,)).fetchone()[0]==0
    finally: db.execute('ALTER TABLE ee.security_audit_events DROP CONSTRAINT deletion_test_failure')
    erase(client,path,version='0',mid=mid)


def test_concurrent_delete_retries_write_once(client,db,resource):
    p,f=resource;pid=p['id'];mid=str(uuid4())
    def remove(_):
        with TestClient(app) as c:
            c.cookies.set('ts_session',create_session_token('engineer01'))
            return c.delete(f'/v1/projects/{pid}/functions/{f}',headers=DHEADERS,params={'mutation_id':mid,'base_lock_version':'0'})
    with ThreadPoolExecutor(max_workers=2) as pool: rows=list(pool.map(remove,range(2)))
    assert [r.status_code for r in rows]==[200,200] and rows[0].json()==rows[1].json()
    assert db.execute('SELECT count(*) FROM ee.security_audit_events WHERE id=%s',(mid,)).fetchone()[0]==1


def test_create_racing_function_deletion_cannot_leave_active_child(client,db,resource):
    p,f=resource;pid=p['id']
    def action(delete):
        with TestClient(app) as c:
            c.cookies.set('ts_session',create_session_token('engineer01'))
            if delete:return c.delete(f'/v1/projects/{pid}/functions/{f}',headers=DHEADERS,params={'mutation_id':str(uuid4()),'base_lock_version':'0'}).status_code
            return c.post(f'/v1/projects/{pid}/functions/{f}/artifacts',headers=HEADERS,json={'creation_id':str(uuid4()),'kind':'FR','description':'竞态'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: deleted,created=list(pool.map(action,[True,False]))
    assert (deleted,created) in ((200,404),(409,201))
    assert db.execute('SELECT count(*) FROM ee.artifacts a JOIN ee.business_functions f ON f.id=a.function_id WHERE f.id=%s AND f.archived_at IS NOT NULL AND a.archived_at IS NULL',(f,)).fetchone()[0]==0


def test_lock_timeout_and_bigint_version(client,db,resource):
    p,f=resource;pid=p['id'];path=f'/{pid}/functions/{f}'
    with psycopg.connect(os.environ['TEST_MANUAL_ADMIN_DSN']) as blocker:
        blocker.execute('SELECT id FROM ee.projects WHERE id=%s FOR UPDATE',(pid,))
        erase(client,path,version='0',status=504)
    assert db.execute('SELECT archived_at FROM ee.business_functions WHERE id=%s',(f,)).fetchone()[0] is None
    db.execute('UPDATE ee.business_functions SET lock_version=9007199254740993 WHERE id=%s',(f,))
    assert erase(client,path,version='9007199254740993')['lock_version']=='9007199254740994'


def test_capped_blocker_summary_still_checks_every_child(client,db,resource):
    p,f=resource;pid=p['id']
    for i in range(23):create(client,pid,f,'FR',description='FR '+str(i))
    info=impact(client,f'/{pid}/functions/{f}')
    assert info['blocker_count']==23 and len(info['blockers'])==20 and info['blockers_truncated']
    erase(client,f'/{pid}/functions/{f}',version='0',status=409)


def test_minimal_grants_and_immutable_histories(client,db):
    with psycopg.connect(settings.projects_write_database_url.get_secret_value(),autocommit=True) as c:
        for table in ('projects','business_functions','artifacts'):
            c.execute('UPDATE ee.'+table+' SET archived_at=archived_at WHERE false')
        for query in ('DELETE FROM ee.artifacts WHERE false','UPDATE ee.prd_versions SET markdown_body=markdown_body WHERE false','UPDATE ee.artifact_revisions SET metadata=metadata WHERE false','UPDATE ee.security_audit_events SET before_state=NULL WHERE false','UPDATE ee.projects SET code=code WHERE false','UPDATE ee.artifacts SET accepted_revision_id=NULL WHERE false'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute(query)
    with psycopg.connect(settings.projects_database_url.get_secret_value(),autocommit=True) as c:
        c.execute('SET default_transaction_read_only=off')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('UPDATE ee.artifacts SET archived_at=NULL WHERE false')


def test_partial_deletion_role_upgrade_blocks_new_project(client,db):
    role=db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_author'").fetchone()[0]
    db.execute("DELETE FROM ee.role_permissions WHERE role_id=%s AND permission_code='fr.archive'",(role,))
    try:
        r=client.post('/v1/projects',headers=HEADERS,json={'creation_id':str(uuid4()),'name':'拒绝半升级'})
        assert r.status_code==503
    finally:db.execute("INSERT INTO ee.role_permissions VALUES(%s,'fr.archive')",(role,))


@pytest.mark.parametrize('operation',['save','accept'])
@pytest.mark.parametrize('first',['delete','write'])
def test_ordered_delete_vs_save_or_accept(client,db,resource,monkeypatch,operation,first):
    from app.services import deletion,manual_work
    p,f=resource;pid=p['id'];a=create(client,pid,f,'PRD',name='并发正文')
    path=f'/{pid}/artifacts/{a["artifact_id"]}';version=impact(client,path)['target']['lock_version']
    entered,attempted,release=Event(),Event(),Event()
    holder=deletion if first=='delete' else manual_work
    method='_blockers' if first=='delete' else 'artifact'
    original=getattr(holder,method)
    def pause(*args,**kwargs):
        result=original(*args,**kwargs)
        entered.set()
        assert release.wait(5),'Owner lock was not released by test'
        return result
    monkeypatch.setattr(holder,method,pause)
    waiter=manual_work if first=='delete' else deletion
    original_lock=waiter.lock_project
    def arriving(*args,**kwargs):
        attempted.set()
        return original_lock(*args,**kwargs)
    monkeypatch.setattr(waiter,'lock_project',arriving)
    def request(action):
        with TestClient(app) as c:
            c.cookies.set('ts_session',create_session_token('engineer01'))
            if action=='delete':
                return c.delete('/v1/projects'+path,headers=DHEADERS,params={'mutation_id':str(uuid4()),'base_lock_version':version})
            if operation=='save':
                return c.post('/v1/projects'+path+'/revisions',headers=HEADERS,json={'mutation_id':str(uuid4()),'base_revision_id':a['revision_id'],'content':{'kind':'PRD','markdown_body':'保存后的正文'}})
            return c.post('/v1/projects'+path+'/accept',headers=HEADERS,json={'mutation_id':str(uuid4()),'revision_id':a['revision_id']})
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(request,first)
        try:
            assert entered.wait(5)
            two=pool.submit(request,'write' if first=='delete' else 'delete')
            assert attempted.wait(5)
        finally: release.set()
        r1,r2=one.result(),two.result()
    assert (r1.status_code,r2.status_code)==((200,404) if first=='delete' else (201,409)),(r1.text,r2.text)
    archived,accepted=db.execute('SELECT archived_at,accepted_revision_id FROM ee.artifacts WHERE id=%s',(a['artifact_id'],)).fetchone()
    assert bool(archived)==(first=='delete')
    assert bool(accepted)==(first=='write' and operation=='accept')
    if first=='write':assert r2.json()['detail']['code']=='deletion_version_conflict'


@pytest.mark.parametrize('first',['delete','reference'])
def test_ordered_new_reference_vs_uc_delete(client,db,resource,monkeypatch,first):
    from app.services import deletion,manual_work
    p,f=resource;pid=p['id'];prd=create(client,pid,f,'PRD',name='PRD');accept(client,pid,prd)
    uc=create(client,pid,f,'UC',name='UC',description='描述',main_flow='流程',prd_revision_id=prd['revision_id']);accept(client,pid,uc)
    fr=create(client,pid,f,'FR',description='暂未选来源')
    path=f'/{pid}/artifacts/{uc["artifact_id"]}';version=impact(client,path)['target']['lock_version']
    entered,attempted,release=Event(),Event(),Event()
    holder=deletion if first=='delete' else manual_work
    method='_blockers' if first=='delete' else 'validate_sources'
    original=getattr(holder,method)
    def pause(*args,**kwargs):
        result=original(*args,**kwargs);entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(holder,method,pause)
    waiter=manual_work if first=='delete' else deletion;original_lock=waiter.lock_project
    def arriving(*args,**kwargs):
        attempted.set();return original_lock(*args,**kwargs)
    monkeypatch.setattr(waiter,'lock_project',arriving)
    def request(action):
        with TestClient(app) as c:
            c.cookies.set('ts_session',create_session_token('engineer01'))
            if action=='delete':return c.delete('/v1/projects'+path,headers=DHEADERS,params={'mutation_id':str(uuid4()),'base_lock_version':version})
            return c.post(f'/v1/projects/{pid}/artifacts/{fr["artifact_id"]}/revisions',headers=HEADERS,json={'mutation_id':str(uuid4()),'base_revision_id':fr['revision_id'],'content':{'kind':'FR','description':'新增来源','sources':[{'uc_id':uc['artifact_id'],'uc_revision_id':uc['revision_id']}]}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(request,first)
        try:
            assert entered.wait(5)
            two=pool.submit(request,'reference' if first=='delete' else 'delete')
            assert attempted.wait(5)
        finally:release.set()
        r1,r2=one.result(),two.result()
    assert (r1.status_code,r2.status_code)==((200,409) if first=='delete' else (201,409)),(r1.text,r2.text)
    assert db.execute('SELECT count(*) FROM ee.fr_uc_sources s JOIN ee.artifacts fr ON fr.head_revision_id=s.fr_revision_id JOIN ee.artifacts uc ON uc.id=s.uc_id WHERE fr.id=%s AND fr.archived_at IS NULL AND uc.archived_at IS NOT NULL',(fr['artifact_id'],)).fetchone()[0]==0
    if first=='reference':assert r2.json()['detail']['code']=='deletion_blocked'


def test_mutation_id_cannot_cross_target_or_operation(client,db,resource):
    p,f=resource;pid=p['id'];mid=str(uuid4())
    second=post(client,f'/{pid}/functions',{'creation_id':str(uuid4()),'name':'另一个功能'})['function_id']
    erase(client,f'/{pid}/functions/{f}',version='0',mid=mid)
    erase(client,f'/{pid}/functions/{second}',version='0',mid=mid,status=409)
    r=client.patch(f'/v1/projects/{pid}/functions/{second}',headers=HEADERS,json={'mutation_id':mid,'base_lock_version':'0','name':'不得复用'})
    assert r.status_code==409
    assert db.execute('SELECT archived_at,lock_version FROM ee.business_functions WHERE id=%s',(second,)).fetchone()==(None,0)


@pytest.mark.parametrize('parent',['project','function'])
def test_legacy_active_child_under_archived_parent_is_not_writable(client,db,resource,parent):
    p,f=resource;pid=p['id'];a=create(client,pid,f,'PRD',name='遗留对象')
    # Deliberately inconsistent legacy state can only be set by fixture admin.
    table,identity=('ee.projects',pid) if parent=='project' else ('ee.business_functions',f)
    db.execute('UPDATE '+table+' SET archived_at=clock_timestamp() WHERE id=%s',(identity,))
    path=f'/{pid}/artifacts/{a["artifact_id"]}'
    impact(client,path,404);erase(client,path,version='0',status=404)
    for suffix,body in [('/revisions',{'mutation_id':str(uuid4()),'base_revision_id':a['revision_id'],'content':{'kind':'PRD','markdown_body':'不得保存'}}),('/accept',{'mutation_id':str(uuid4()),'revision_id':a['revision_id']})]:
        assert client.post('/v1/projects'+path+suffix,headers=HEADERS,json=body).status_code==404
    assert db.execute('SELECT archived_at FROM ee.artifacts WHERE id=%s',(a['artifact_id'],)).fetchone()[0] is None
    if parent=='function':
        # The project check must still include active objects hidden under a deleted parent.
        assert impact(client,'/'+pid)['blocker_count']==1
        erase(client,'/'+pid,status=409)


def test_deleted_source_cannot_be_carried_or_accepted_in_legacy_draft(client,db,resource):
    p,f=resource;pid=p['id'];prd,uc,fr=chain(client,pid,f)
    draft=save_fr(client,pid,fr,[uc])
    db.execute('UPDATE ee.artifacts SET archived_at=clock_timestamp() WHERE id=%s',(uc['artifact_id'],))
    assert client.post(f'/v1/projects/{pid}/artifacts/{fr["artifact_id"]}/revisions',headers=HEADERS,json={'mutation_id':str(uuid4()),'base_revision_id':draft['revision_id'],'content':{'kind':'FR','description':'保留旧来源也必须检查','sources':[{'uc_id':uc['artifact_id'],'uc_revision_id':uc['revision_id']}]}}).status_code==409
    assert client.post(f'/v1/projects/{pid}/artifacts/{fr["artifact_id"]}/accept',headers=HEADERS,json={'mutation_id':str(uuid4()),'revision_id':draft['revision_id']}).status_code==409
    assert db.execute('SELECT head_revision_id,accepted_revision_id FROM ee.artifacts WHERE id=%s',(fr['artifact_id'],)).fetchone()==(UUID(draft['revision_id']),UUID(fr['revision_id']))


def test_write_switch_disables_delete_and_capabilities(client,db,resource,monkeypatch):
    p,f=resource;pid=p['id'];monkeypatch.setattr(settings,'business_writes_enabled',False)
    assert not impact(client,f'/{pid}/functions/{f}')['can_delete']
    assert not client.get('/v1/projects/'+pid).json()['can_delete']
    assert erase(client,f'/{pid}/functions/{f}',version='0',status=503)['detail']['code']=='business_writes_disabled'
    assert db.execute('SELECT archived_at FROM ee.business_functions WHERE id=%s',(f,)).fetchone()[0] is None
