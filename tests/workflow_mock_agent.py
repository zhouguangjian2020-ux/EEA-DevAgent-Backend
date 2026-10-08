"""Local contract fixture, never a production Agent or Skill implementation."""
import asyncio,json,os
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import StreamingResponse
if os.getenv('WORKFLOW_MOCK_ALLOWED')!='1':raise RuntimeError('Explicit isolated mock opt-in required')
app=FastAPI();requests=[]
@app.get('/health')
def health():return {'status':'ok','mode':'contract-mock'}
@app.get('/requests')
def captured():return requests
async def receive(r):
    if r.headers.get('X-API-Key')!='isolated-contract-key':raise HTTPException(401)
    body=await r.json();requests.append(body);return body
@app.post('/api/v1/agent/invoke')
async def invoke(r:Request):
    b=await receive(r);c=b.get('reference_data',{});scene=b.get('scene')
    if b.get('response_format')!='json':raise HTTPException(409,'OUTPUT_FORMAT_CONFLICT')
    if b.get('idempotency_key','').endswith(':intent-v1'):
        prompt=b['message'].rsplit('本次用户要求：',1)[-1]
        result={'intent':'modify' if any(word in prompt for word in ('修改', '改写')) else 'question'}
    elif scene=='FROM_PRD_CREATE_UC':
        result={'use_cases':[{'name':'[契约Mock] 用例'+str(i),'description':'结构化用例正文','actors':'驾驶员','preconditions':'车辆上电','scenario':{'main_flow':'驾驶员开启录像','branches':['用户取消操作'],'exceptions':['存储异常']},'postconditions':'视频已保存','extensions':'','hmi':'','regulations':'','performance':'','nonfunctional':''} for i in range(1,3)]}
    elif scene=='FROM_UC_CREATE_FR':
        src=c['sources'][0];result={'functional_requirements':[{'kind':'FR','description':'[契约Mock] 系统应记录触发事件','acceptance_criteria':'可读取对应记录','sources':[{'uc_id':src['artifact_id'],'uc_revision_id':src['revision_id']}]}]}
    elif scene=='FROM_FR_CREATE_SS_ASSIGNMENT':
        result={'assignments':[{'fr_id':s['artifact_id'],'fr_revision_id':s['revision_id'],'targets':[{'subsystem_id':x['subsystem_id'],'rationale':'契约Mock职责匹配'} for x in c['subsystems'][:2]],'note':'契约Mock：人工核对后再保存'} for s in c['sources']]}
    elif scene=='FROM_UC_CREATE_UC_DIAGRAM':
        lines=['flowchart LR', 'actor_user['+chr(34)+'用户'+chr(34)+']']
        for index,uc in enumerate(c['uc_list']):
            label=''.join(ch for ch in uc['content']['name'] if ch.isalnum() or ch in ' _-')[:100] or 'UC'
            node='uc_'+str(index)
            lines += [node+'['+chr(34)+label+chr(34)+']', 'actor_user --- '+node]
        result={'diagram_format':'mermaid','diagram_code':chr(10).join(lines)}
    else:
        a=c['attachments'][0];body=dict(a['content']);field='markdown_body' if body['kind']=='PRD' else 'description';body[field]='[契约Mock] 人工审阅后应用的修改'
        result={'changes':[{'artifact_id':a['artifact_id'],'base_revision_id':a['revision_id'],'content':body}]}
    return {'scene':scene,'structured':result,'trace_id':b.get('trace_id')}
@app.post('/api/v1/agent/invoke/stream')
async def stream(r:Request):
    b=await receive(r)
    async def events():
        for text in ['[契约Mock] ','已读取附件','与会话历史。']:
            await asyncio.sleep(.12 if '慢速' not in b['message'] else 1)
            yield 'data: '+json.dumps({'type':'token','content':text},ensure_ascii=False)+chr(10)*2
        if '断流测试' not in b['message']:
            yield 'data: '+json.dumps({'type':'done'})+chr(10)*2
    return StreamingResponse(events(),media_type='text/event-stream')
