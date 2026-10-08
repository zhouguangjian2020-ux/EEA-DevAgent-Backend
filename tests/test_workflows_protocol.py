import json
from uuid import uuid4
import httpx
import pytest
from pydantic import SecretStr
from app.core.config import settings
from app.services import workflow_agent as agent
from app.services.projects import ProjectReadError
from app.core.diagnostic_http import workflow_project

@pytest.fixture
def protocol(monkeypatch):
    monkeypatch.setattr(settings,'agent_generation_invoke_url','http://127.0.0.1:8419/api/v1/agent/invoke')
    monkeypatch.setattr(settings,'agent_invoke_url','http://127.0.0.1:8419/api/v1/agent/invoke/stream')
    monkeypatch.setattr(settings,'agent_api_key',SecretStr('isolated-contract-test-key'))
    def install(handler):monkeypatch.setattr(agent,'agent_client',lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return install

@pytest.mark.anyio
async def test_json_auth_and_format(protocol):
    def handler(r):
        body=json.loads(r.content)
        assert r.headers['X-API-Key']=='isolated-contract-test-key'
        assert body['response_format']=='json' and body['scene']=='FROM_UC_CREATE_FR'
        return httpx.Response(200,json={'scene':body['scene'],'structured':{'functional_requirements':[]}})
    protocol(handler)
    assert await agent.invoke(agent.request_body('request',{'sources':[]},'trace','FROM_UC_CREATE_FR'))=={'functional_requirements':[]}

@pytest.mark.anyio
@pytest.mark.parametrize('status,body,code',[(409,{},'agent_upstream_rejected'),(200,{'scene':'wrong'},'agent_invalid_response'),(200,{'scene':'FROM_UC_CREATE_FR','output':'plain text'},'agent_invalid_response')])
async def test_protocol_rejection(protocol,status,body,code):
    protocol(lambda r:httpx.Response(status,json=body))
    with pytest.raises(ProjectReadError) as e:await agent.invoke(agent.request_body('x',{},'t','FROM_UC_CREATE_FR'))
    assert e.value.code==code

@pytest.mark.anyio
@pytest.mark.parametrize('ending,ok',[('done',True),('eof',False),('error',False)])
async def test_chat_stream_termination(protocol,ending,ok):
    events=[{'type':'token','content':'真实分块'}]+([] if ending=='eof' else [{'type':ending}])
    body=''.join('data: '+json.dumps(e,ensure_ascii=False)+chr(10)*2 for e in events)
    protocol(lambda r:httpx.Response(200,content=body.encode(),headers={'Content-Type':'text/event-stream'}))
    async def collect():return [e async for e in agent.chat_stream(agent.request_body('x',{},'t',response_format='text'))]
    if ok:assert (await collect())[-1]['type']=='done'
    else:
        with pytest.raises(Exception):await collect()

def test_trace_paths_are_fixed_uuid_scoped():
    p=str(uuid4());f=str(uuid4())
    assert workflow_project(f'/v1/projects/{p}/functions/{f}/fr-generations')==p
    assert workflow_project(f'/v1/projects/{p}/functions/{f}/artifacts') is None
    assert workflow_project('/v1/projects/not-id/conversations') is None
    assert workflow_project(f'/v1/projects/{p}/conversations/{f}/messages/stream')==p

@pytest.fixture
def anyio_backend():return 'asyncio'
