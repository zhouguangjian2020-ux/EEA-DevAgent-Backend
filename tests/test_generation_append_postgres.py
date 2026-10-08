import asyncio
from uuid import UUID,uuid4
import pytest
from app.services import uc_generation,generation_append,manual_work
from app.services.projects import ProjectReadError
from app.schemas.agent import UcGenerationCandidate
from app.schemas.manual_work import SaveRevision,PrdContent,AcceptRevision
from app.core.config import settings
from tests.test_manual_work_postgres import db,client,PROJECT,HEADERS,new_function,create,accept
from tests.test_workflows_postgres import sources,agent

@pytest.fixture
def uc_agent(monkeypatch):
    calls=[]
    async def generate(prd,**kwargs):
        calls.append((prd,kwargs))
        items=[UcGenerationCandidate(candidate_id=uuid4(),ordinal=i+1,name='UC '+str(i),description='原文',actors='驾驶员',preconditions='',scenario={'main_flow':'用户执行','branches':['正常分支'],'exceptions':[]},postconditions='',extensions='',hmi='',regulations='',performance='',nonfunctional='') for i in range(2)]
        return uc_generation.UcGenerationAgentResult(candidates=items,trace_id='local-contract-trace')
    monkeypatch.setattr(uc_generation,'generate_uc_from_prd',generate)
    return calls

def endpoint(fid,kind):return '/v1/projects/'+PROJECT+'/functions/'+fid+'/'+kind.lower()+'-generations/append'
def uc_input(prd):return {'client_request_id':str(uuid4()),'prd_id':prd['artifact_id'],'prd_revision_id':prd['revision_id']}
def rows(db,fid,kind):
    return db.execute('SELECT id,head_revision_id,accepted_revision_id,metadata FROM ee.artifacts WHERE function_id=%s AND type_code=%s ORDER BY id',(fid,kind)).fetchall()

def test_uc_batch_atomic_pending_replay_and_append(client,db,sources,uc_agent):
    fid,prd,existing=sources;body=uc_input(prd)
    original=rows(db,fid,'UC')
    r=client.post(endpoint(fid,'UC'),headers=HEADERS,json=body);assert r.status_code==200,r.text
    result=r.json();assert len(result['items'])==2 and not result['replayed']
    saved=rows(db,fid,'UC');assert len(saved)==len(original)+2
    for item in result['items']:
        row=next(x for x in saved if str(x[0])==item['artifact_id']);assert row[2] is None
        branches=db.execute('SELECT body FROM ee.uc_scenario_items WHERE uc_revision_id=%s AND kind=%s ORDER BY position',(item['revision_id'],'branch')).fetchall()
        assert len(branches)==4 and any(x[0]=='正常分支' for x in branches)
    replay=client.post(endpoint(fid,'UC'),headers=HEADERS,json=body);assert replay.status_code==200,replay.text
    assert replay.json()['replayed'] and replay.json()['items']==result['items'] and len(uc_agent)==1
    accept(client,result['items'][0])
    after_accept=client.post(endpoint(fid,'UC'),headers=HEADERS,json=body);assert after_accept.status_code==200
    assert len(rows(db,fid,'UC'))==len(saved)
    body['client_request_id']=str(uuid4())
    assert client.post(endpoint(fid,'UC'),headers=HEADERS,json=body).status_code==200
    assert len(rows(db,fid,'UC'))==len(saved)+2
    old=next(x for x in rows(db,fid,'UC') if str(x[0])==existing['artifact_id']);assert str(old[2])==existing['revision_id']

def test_uc_failure_rolls_back_whole_batch(client,db,sources,uc_agent,monkeypatch):
    fid,prd,_=sources;before=rows(db,fid,'UC');original=manual_work.create_artifact_in_db;calls=[]
    def broken(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:raise ProjectReadError(409,'test_second_item','local injected failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(manual_work,'create_artifact_in_db',broken)
    body=uc_input(prd);r=client.post(endpoint(fid,'UC'),headers=HEADERS,json=body)
    assert r.status_code==409,r.text
    assert rows(db,fid,'UC')==before
    monkeypatch.setattr(manual_work,'create_artifact_in_db',original)
    assert client.post(endpoint(fid,'UC'),headers=HEADERS,json=body).status_code==200
    assert len(rows(db,fid,'UC'))==len(before)+2

def test_uc_source_change_blocks_commit(client,db,sources,uc_agent,monkeypatch):
    fid,prd,_=sources;original=uc_generation.generate_uc_from_prd;before=rows(db,fid,'UC')
    async def changed(*args,**kwargs):
        result=await original(*args,**kwargs)
        revision=await asyncio.to_thread(manual_work.save_revision,'engineer01',UUID(PROJECT),UUID(prd['artifact_id']),SaveRevision(mutation_id=uuid4(),base_revision_id=UUID(prd['revision_id']),content=PrdContent(kind='PRD',markdown_body='# 新的已确认来源')),str(uuid4()))
        await asyncio.to_thread(manual_work.accept_revision,'engineer01',UUID(PROJECT),UUID(prd['artifact_id']),AcceptRevision(mutation_id=uuid4(),revision_id=revision.revision_id),str(uuid4()))
        return result
    monkeypatch.setattr(uc_generation,'generate_uc_from_prd',changed)
    r=client.post(endpoint(fid,'UC'),headers=HEADERS,json=uc_input(prd))
    assert r.status_code==409,r.text
    assert rows(db,fid,'UC')==before

def test_writes_disabled_before_agent(client,db,sources,uc_agent,monkeypatch):
    fid,prd,_=sources;monkeypatch.setattr(settings,'business_writes_enabled',False)
    r=client.post(endpoint(fid,'UC'),headers=HEADERS,json=uc_input(prd))
    assert r.status_code==503 and not uc_agent

def test_uc_overflow_is_not_truncated_or_saved(client,db,sources,uc_agent,monkeypatch):
    fid,prd,_=sources;before=rows(db,fid,'UC');original=uc_generation.generate_uc_from_prd
    async def overflow(*args,**kwargs):
        result=await original(*args,**kwargs)
        c=result.candidates[0];scenario=c.scenario.model_copy(update={'branches':['x']*5})
        result.candidates[0]=c.model_copy(update={'scenario':scenario});return result
    monkeypatch.setattr(uc_generation,'generate_uc_from_prd',overflow)
    r=client.post(endpoint(fid,'UC'),headers=HEADERS,json=uc_input(prd))
    assert r.status_code==502,r.text
    assert rows(db,fid,'UC')==before

def test_reusing_request_for_another_function_conflicts(client,db,sources,uc_agent):
    fid,prd,_=sources;body=uc_input(prd);assert client.post(endpoint(fid,'UC'),headers=HEADERS,json=body).status_code==200
    other=new_function(client);other_prd=create(client,other,'PRD',name='Other');accept(client,other_prd)
    other_body={**uc_input(other_prd),'client_request_id':body['client_request_id']}
    r=client.post(endpoint(other,'UC'),headers=HEADERS,json=other_body);assert r.status_code==409,r.text
    assert not rows(db,other,'UC')

def test_fr_auto_append_replay_and_new_batch(client,db,sources,agent):
    fid,_,uc=sources;body={'client_request_id':str(uuid4())}
    r=client.post(endpoint(fid,'FR'),headers=HEADERS,json=body);assert r.status_code==200,r.text
    result=r.json();assert len(result['items'])==1
    assert rows(db,fid,'FR')[0][2] is None
    replay=client.post(endpoint(fid,'FR'),headers=HEADERS,json=body);assert replay.json()['replayed'] and len(agent)==1
    body['client_request_id']=str(uuid4());assert client.post(endpoint(fid,'FR'),headers=HEADERS,json=body).status_code==200
    assert len(rows(db,fid,'FR'))==2
    assert all(r[2] is None for r in rows(db,fid,'FR'))

def test_fr_pending_source_blocks_agent(client,db,sources,agent):
    fid,prd,uc=sources
    create(client,fid,'UC',name='尚未确认的附加来源',prd_revision_id=prd['revision_id'])
    r=client.post(endpoint(fid,'FR'),headers=HEADERS,json={'client_request_id':str(uuid4())})
    assert r.status_code==409 and not agent and not rows(db,fid,'FR')

def test_same_uc_request_concurrently_creates_one_batch(client,db,sources,uc_agent):
    from concurrent.futures import ThreadPoolExecutor
    fid,prd,_=sources;body=uc_input(prd);before=len(rows(db,fid,'UC'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:client.post(endpoint(fid,'UC'),headers=HEADERS,json=body),range(2)))
    assert all(r.status_code==200 for r in responses),[r.text for r in responses]
    assert responses[0].json()['items']==responses[1].json()['items']
    assert len(rows(db,fid,'UC'))==before+2
