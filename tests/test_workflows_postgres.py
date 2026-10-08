from uuid import uuid4
import json
import pytest
from tests.test_manual_work_postgres import db, client, PROJECT, HEADERS, new_function, create, accept, post
from app.services import workflow_agent
from app.core.security import create_session_token

P='/v1/projects/'+PROJECT

@pytest.fixture
def sources(client):
    fid=new_function(client)
    prd=create(client,fid,'PRD',name='Workflow PRD')
    accept(client,prd)
    uc=create(client,fid,'UC',name='用例',description='真实来源正文',main_flow='用户启动',branches=['分支'+str(i) for i in range(4)],exceptions=['异常'+str(i) for i in range(5)],prd_revision_id=prd['revision_id'])
    accept(client,uc)
    return fid,prd,uc

@pytest.fixture
def agent(monkeypatch):
    requests=[]
    async def invoke(body):
        requests.append(body)
        context=body['reference_data']
        if body.get('scene')=='FROM_UC_CREATE_FR':
            src=context['sources'][0]
            return {'functional_requirements':[{'kind':'FR','description':'生成的FR','acceptance_criteria':'可观察标准','sources':[{'uc_id':src['artifact_id'],'uc_revision_id':src['revision_id']}]}]}
        if body.get('scene')=='FROM_FR_CREATE_SS_ASSIGNMENT':
            return {'assignments':[{'fr_id':s['artifact_id'],'fr_revision_id':s['revision_id'],'targets':[{'subsystem_id':context['subsystems'][0]['subsystem_id'],'rationale':'职责匹配'}]} for s in context['sources']]}
        a=context['attachments'][0]
        return {'changes':[{'artifact_id':a['artifact_id'],'base_revision_id':a['revision_id'],'content':{**a['content'],'description':'建议修改内容'}}]}
    async def stream(body):
        requests.append(body)
        yield {'type':'token','content':'基于附件回答'}
        yield {'type':'done'}
    monkeypatch.setattr(workflow_agent,'invoke',invoke)
    monkeypatch.setattr(workflow_agent,'chat_stream',stream)
    return requests

def gen(client,fid,kind='fr'):
    return post(client,f'/functions/{fid}/{kind}-generations',{'client_request_id':str(uuid4())},200)

def test_fr_generate_adopt_and_stale_source(client,db,sources,agent):
    fid,prd,uc=sources
    g=gen(client,fid)
    assert len(agent[0]['reference_data']['sources'][0]['content']['branches'])==4
    assert len(agent[0]['reference_data']['sources'][0]['content']['exceptions'])==5
    assert db.execute('SELECT count(*) FROM ee.functional_requirements WHERE function_id=%s',(fid,)).fetchone()[0]==0
    c=g['candidates'][0];c.pop('candidate_id')
    body={'creation_id':str(uuid4()),'source_version':g['source_version'],'content':c}
    r=post(client,f'/functions/{fid}/fr-candidates/adopt',body,200)
    assert post(client,f'/functions/{fid}/fr-candidates/adopt',body,200)['artifact_id']==r['artifact_id']
    assert db.execute('SELECT accepted_revision_id FROM ee.artifacts WHERE id=%s',(r['artifact_id'],)).fetchone()[0] is None
    uc2=create(client,fid,'UC',name='新增未确认',prd_revision_id=prd['revision_id'])
    body['creation_id']=str(uuid4())
    post(client,f'/functions/{fid}/fr-candidates/adopt',body,409)
    post(client,f'/functions/{fid}/fr-generations',{'client_request_id':str(uuid4())},409)

def test_allocation_draft_accept_history(client,db,sources,agent):
    fid,prd,uc=sources
    fr=create(client,fid,'FR',description='已确认FR',sources=[{'uc_id':uc['artifact_id'],'uc_revision_id':uc['revision_id']}])
    accept(client,fr)
    g=gen(client,fid,'allocation');c=g['candidates'][0]
    data={'mutation_id':str(uuid4()),'allocation_id':str(uuid4()),'fr_id':fr['artifact_id'],'fr_revision_id':fr['revision_id'],'design_version_id':g['source']['subsystems'][0]['design_version_id'],'targets':c['targets'],'note':'人工说明','source_version':g['source_version']}
    url=f'/functions/{fid}/allocations'
    saved=post(client,url,data,200)
    post(client,url,data,200)
    confirm={'mutation_id':str(uuid4()),'revision_id':saved['revision_id']}
    post(client,f'/allocations/{saved["artifact_id"]}/accept',confirm,200)
    post(client,f'/allocations/{saved["artifact_id"]}/accept',confirm,200)
    data.update(mutation_id=str(uuid4()),base_revision_id=saved['revision_id'],note='第二版')
    pending=post(client,url,data,200)
    head,accepted=db.execute('SELECT head_revision_id,accepted_revision_id FROM ee.artifacts WHERE id=%s',(saved['artifact_id'],)).fetchone()
    assert str(head)==pending['revision_id'] and str(accepted)==saved['revision_id']
    assert db.execute('SELECT count(*) FROM ee.allocation_versions WHERE allocation_id=%s',(saved['artifact_id'],)).fetchone()[0]==2
    data.update(mutation_id=str(uuid4()),base_revision_id=pending['revision_id'],targets=[])
    empty=post(client,url,data,200)
    post(client,f'/allocations/{saved["artifact_id"]}/accept',{'mutation_id':str(uuid4()),'revision_id':empty['revision_id']},409)

def test_private_threads_history_attachments_preview(client,db,sources,agent):
    fid,prd,uc=sources
    create_body={'client_request_id':str(uuid4()),'title':'我的会话'}
    t=post(client,'/conversations',create_body,200)
    assert post(client,'/conversations',create_body,200)['id']==t['id']
    t2=post(client,'/conversations',{'client_request_id':str(uuid4())},200)
    assert t['id']!=t2['id']
    path=P+'/conversations/'+t['id']
    data={'client_request_id':str(uuid4()),'message':'分析来源','attachments':[{'artifact_id':uc['artifact_id'],'revision_id':uc['revision_id']}]}
    response=client.post(path+'/messages/stream',headers=HEADERS,json=data)
    assert response.status_code==200 and 'completed' in response.text,response.text
    assert agent[-1]['reference_data']['attachments'][0]['content']['description']=='真实来源正文'
    count=len(agent)
    assert 'replayed' in client.post(path+'/messages/stream',headers=HEADERS,json=data).text
    assert len(agent)==count
    data.update(client_request_id=str(uuid4()),message='改写',mode='modify')
    r=client.post(path+'/messages/stream',headers=HEADERS,json=data)
    assert 'preview' in r.text,r.text
    assert len(agent[-1]['reference_data']['messages'])==2
    assert db.execute('SELECT count(*) FROM ee.artifact_revisions WHERE artifact_id=%s',(uc['artifact_id'],)).fetchone()[0]==1
    history=client.get(path).json()
    assert len(history['turns'])==2 and history['turns'][1]['status']=='completed'
    assert client.get(P+'/conversations/'+t2['id']).json()['turns']==[]
    client.cookies.set('ts_session',create_session_token('unrelated-user'))
    assert client.get(path).status_code==401

def test_foreign_candidate_and_target_rejected(sources,client,agent,monkeypatch):
    async def invalid(body):
        return {'functional_requirements':[{'kind':'FR','description':'invalid','sources':[{'uc_id':str(uuid4()),'uc_revision_id':str(uuid4())}]}]}
    monkeypatch.setattr(workflow_agent,'invoke',invalid)
    post(client,f'/functions/{sources[0]}/fr-generations',{'client_request_id':str(uuid4())},502)


def test_threads_are_user_and_project_isolated(client,db,sources,monkeypatch,agent):
    t=post(client,'/conversations',{'client_request_id':str(uuid4())},200)
    actor=db.execute("SELECT id FROM ee.app_users WHERE external_subject='engineer01'").fetchone()[0]
    peer=str(uuid4());subject='peer-'+peer
    db.execute('INSERT INTO ee.app_users(id,external_subject,display_name) VALUES(%s,%s,%s)',(peer,subject,'Read-only peer'))
    db.execute('INSERT INTO ee.project_members(project_id,user_id,added_by) VALUES(%s,%s,%s)',(PROJECT,peer,actor))
    role=db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_reader'").fetchone()[0]
    db.execute("INSERT INTO ee.access_bindings(id,project_id,user_id,role_id,scope_kind,created_by,updated_by) VALUES(%s,%s,%s,%s,'project',%s,%s)",(uuid4(),PROJECT,peer,role,actor,actor))
    client.cookies.set('ts_session',create_session_token(subject))
    assert client.get(P+'/conversations/'+t['id']).status_code==404
    assert client.get(P+'/conversations').json()['threads']==[]
    post(client,f'/functions/{sources[0]}/fr-generations',{'client_request_id':str(uuid4())},403)
    other=post(client,'/conversations',{'client_request_id':str(uuid4())},200)
    body={'client_request_id':str(uuid4()),'message':'改写','mode':'modify','attachments':[{'artifact_id':sources[2]['artifact_id'],'revision_id':sources[2]['revision_id']}]}
    assert client.post(P+'/conversations/'+other['id']+'/messages/stream',headers=HEADERS,json=body).status_code==403
    assert client.get('/v1/projects/'+str(uuid4())+'/conversations/'+other['id']).status_code==404

def test_stale_attachment_and_failed_request_retry(client,db,sources,agent,monkeypatch):
    from app.services.projects import ProjectReadError
    fid,prd,uc=sources
    t=post(client,'/conversations',{'client_request_id':str(uuid4())},200)
    path=P+'/conversations/'+t['id']
    payload={'client_request_id':str(uuid4()),'message':'retry fixture','attachments':[]}
    async def failure(body):
        yield {'type':'token','content':'未完成部分'}
        raise ProjectReadError(502,'agent_stream_interrupted','未完整结束')
    original=workflow_agent.chat_stream;monkeypatch.setattr(workflow_agent,'chat_stream',failure)
    r=client.post(path+'/messages/stream',headers=HEADERS,json=payload)
    assert 'error' in r.text
    h=client.get(path).json();assert h['turns'][0]['status']=='failed'
    monkeypatch.setattr(workflow_agent,'chat_stream',original)
    r=client.post(path+'/messages/stream',headers=HEADERS,json={**payload,'retry':True})
    assert 'completed' in r.text
    assert len(client.get(path).json()['turns'])==1
    assert client.post(path+'/messages/stream',headers=HEADERS,json={**payload,'message':'changed'}).status_code==409
    body={'kind':'UC','name':'更新','description':'更新','main_flow':'主流程','prd_revision_id':prd['revision_id']}
    post(client,f'/artifacts/{uc["artifact_id"]}/revisions',{'mutation_id':str(uuid4()),'base_revision_id':uc['revision_id'],'content':body})
    payload.update(client_request_id=str(uuid4()),attachments=[{'artifact_id':uc['artifact_id'],'revision_id':uc['revision_id']}])
    assert client.post(path+'/messages/stream',headers=HEADERS,json=payload).status_code==409

def test_generation_validation_and_request_guards(client,sources,agent,monkeypatch):
    fid,prd,uc=sources
    path=P+f'/functions/{fid}/fr-generations'
    assert client.post(path,json={'client_request_id':str(uuid4())}).status_code==403
    assert client.post(path,headers={**HEADERS,'Origin':'http://foreign.invalid'},json={'client_request_id':str(uuid4())}).status_code==403
    assert client.post(path,headers=HEADERS,json={'client_request_id':str(uuid4()),'body':'untrusted'}).status_code==422
    async def invalid(body):return {'assignments':[]}
    fr=create(client,fid,'FR',description='FR',sources=[{'uc_id':uc['artifact_id'],'uc_revision_id':uc['revision_id']}]);accept(client,fr)
    monkeypatch.setattr(workflow_agent,'invoke',invalid)
    post(client,f'/functions/{fid}/allocation-generations',{'client_request_id':str(uuid4())},502)
