"""Local text-plan/PRD fixture; no production Agent access or model quality claim."""
import asyncio
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from tests import workflow_mock_agent as base
app = FastAPI()
settings = {"name": "霜冻感知", "delay": 0, "fail_next_prd": False}
@app.get('/health')
def health(): return base.health()
@app.get('/requests')
def requests(): return base.requests
@app.post('/control')
async def control(request: Request):
    settings.update(await request.json())
    return {"ok": True}
@app.post('/api/v1/agent/invoke')
async def invoke(request: Request):
    body = await request.json()
    if body.get('scene') != 'FEATURE_DEFINITION': return await base.invoke(request)
    await base.receive(request)
    if body.get('response_format') != 'text': raise HTTPException(409, 'PLAN_MUST_USE_TEXT')
    name = settings['name']
    await asyncio.sleep(settings['delay'])
    return {'scene': 'FEATURE_DEFINITION', 'plan': {'feature_name': name}, 'output': name + '的独立功能说明'}
@app.post('/api/v1/agent/invoke/stream')
async def stream(request: Request):
    body = await request.json()
    if body.get('scene') != 'FROM_DEFINITION_CREATE_PRD': return await base.stream(request)
    await base.receive(request)
    if body.get('response_format') != 'text': raise HTTPException(409, 'PRD_MUST_USE_TEXT')
    if settings['fail_next_prd']:
        settings['fail_next_prd'] = False
        raise HTTPException(503, 'LOCAL_SIMULATED_AGENT_FAILURE')
    async def events():
        await asyncio.sleep(settings['delay'])
        output = '# [契约Mock] PRD\n\n本地生成后保存，等待工程师确认。'
        yield 'data: ' + json.dumps({'type':'token','content':output}, ensure_ascii=False) + '\n\n'
        yield 'data: ' + json.dumps({'type':'done','scene':'FROM_DEFINITION_CREATE_PRD','output':output}, ensure_ascii=False) + '\n\n'
    return StreamingResponse(events(), media_type='text/event-stream')
