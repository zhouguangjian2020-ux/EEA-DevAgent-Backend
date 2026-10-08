import json
from uuid import uuid4
import pytest
from app.services import workflow_agent
from tests.test_manual_work_postgres import db,client,PROJECT,HEADERS
from tests.test_workflows_postgres import sources

def thread(client):
    r=client.post('/v1/projects/'+PROJECT+'/conversations',headers=HEADERS,json={'client_request_id':str(uuid4()),'title':'Auto intent'});assert r.status_code==200,r.text;return r.json()['id']
def send(client,tid,**extra):
    body={'client_request_id':str(uuid4()),'message':'分析当前需求','auto_intent':True,'attachments':[],**extra}
    return client.post('/v1/projects/'+PROJECT+'/conversations/'+tid+'/messages/stream',headers=HEADERS,json=body)
def events(response):
    return [json.loads(x[6:]) for x in response.text.splitlines() if x.startswith('data: ')]
@pytest.fixture
def auto_agent(monkeypatch):
    state={'intent':'question','requests':[]}
    async def invoke(body):
        state['requests'].append(body)
        if body['idempotency_key'].endswith(':intent-v1'):return {'intent':state['intent']}
        a=body['reference_data']['attachments'][0]
        return {'changes':[{'artifact_id':a['artifact_id'],'base_revision_id':a['revision_id'],'content':{**a['content'],'description':'新的建议描述'}}]}
    async def stream(body):
        state['requests'].append(body)
        yield {'type':'token','content':'流式回答一'}
        yield {'type':'token','content':'流式回答二'}
        yield {'type':'done'}
    monkeypatch.setattr(workflow_agent,'invoke',invoke);monkeypatch.setattr(workflow_agent,'chat_stream',stream)
    return state

def test_auto_question_empty_attachments_preserves_thread(client,db,auto_agent):
    tid=thread(client);r=send(client,tid);assert r.status_code==200,r.text
    e=events(r);assert [x['content'] for x in e if x['type']=='token']==['流式回答一','流式回答二']
    assert e[-1]['type']=='done'
    assert len(auto_agent['requests'])==2
    for body in auto_agent['requests']:
        ctx=body['reference_data'];assert ctx['attachments']==[] and ctx['thread_id']==tid and ctx['project_id']==PROJECT
        assert not any(k in ctx for k in ['artifact_id','revision_id','function_id','prd_markdown'])
    assert auto_agent['requests'][0]['response_format']=='json' and auto_agent['requests'][1]['response_format']=='text'
    assert db.execute('SELECT status,body FROM ee.chat_turns WHERE thread_id=%s',(tid,)).fetchone()==('completed','流式回答一流式回答二')

def test_auto_modify_only_previews_explicit_attachment(client,db,sources,auto_agent):
    fid,_,uc=sources;tid=thread(client);auto_agent['intent']='modify'
    r=send(client,tid,message='修改这个UC的描述',attachments=[{'artifact_id':uc['artifact_id'],'revision_id':uc['revision_id']}])
    e=events(r);assert e[-1]['type']=='done',r.text
    previews=[x for x in e if x['type']=='preview'];assert len(previews)==1 and previews[0]['changes'][0]['artifact_id']==uc['artifact_id']
    assert str(db.execute('SELECT head_revision_id FROM ee.artifacts WHERE id=%s',(uc['artifact_id'],)).fetchone()[0])==uc['revision_id']

def test_auto_modify_without_attachment_never_uses_history(client,db,sources,auto_agent):
    _,_,uc=sources;tid=thread(client)
    r=send(client,tid,attachments=[{'artifact_id':uc['artifact_id'],'revision_id':uc['revision_id']}]);assert events(r)[-1]['type']=='done'
    auto_agent['intent']='modify';auto_agent['requests'].clear()
    r=send(client,tid,message='帮我修改上一轮的文档');e=events(r)
    assert e[-1]['code']=='modification_attachment_required',r.text
    assert len(auto_agent['requests'])==1
    ctx=auto_agent['requests'][0]['reference_data'];assert ctx['attachments']==[] and ctx['messages']

def test_invalid_agent_intent_fails_closed(client,db,auto_agent):
    auto_agent['intent']='execute';r=send(client,thread(client));assert events(r)[-1]['code']=='agent_intent_invalid'
    assert len(auto_agent['requests'])==1

def test_completed_auto_turn_replay_does_not_call_agent(client,db,auto_agent):
    tid=thread(client);cid=str(uuid4());r=send(client,tid,client_request_id=cid);assert events(r)[-1]['type']=='done'
    count=len(auto_agent['requests']);r=send(client,tid,client_request_id=cid);assert events(r)[-1]['replayed']
    assert len(auto_agent['requests'])==count
