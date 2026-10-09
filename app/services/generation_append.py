# Atomic pending batches; receipts use existing metadata, no new tables.
import asyncio
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
from pydantic import ValidationError
from app.schemas.manual_work import UcCreate, FrCreate
from app.services import manual_work as mw, workflow_agent, uc_generation
from app.services.workflow_sources import context_in_db, object_content, digest, fail

def batch_id(subject, kind, request_id, index=0):
    return UUID(bytes=uuid5(NAMESPACE_URL, f'ts-generated-batch/v1/{subject}/{kind}/{request_id}/{index}').bytes, version=4)

def input_hash(project_id, function_id, kind, payload):
    return digest({'project_id':str(project_id),'function_id':str(function_id),'kind':kind,'input':payload.model_dump(mode='json', exclude={'thread_id'})})

def guard(db, subject, project_id, function_id, kind, payload):
    mw.lock_project(db,project_id)
    actor=mw.authorize(db,subject,project_id,kind+'.create',function_id=function_id)
    mw.feature(db,project_id,function_id)
    root=batch_id(subject,kind,payload.client_request_id)
    mw.lock_request(db,root)
    row=db.execute('SELECT project_id,function_id,created_by,metadata FROM ee.artifacts WHERE id=%s',(root,)).fetchone()
    if row:
        receipt=row['metadata'].get('generation_batch')
        if row['project_id']!=project_id or row['function_id']!=function_id or row['created_by']!=actor or not receipt or receipt['input_hash']!=input_hash(project_id,function_id,kind,payload):
            fail(409,'generation_request_conflict','生成请求标识已用于其他输入，请勿更换输入重试')
        return {**receipt['result'],'replayed':True}
    return None

def source_in_db(db, subject, project_id, function_id, kind, payload):
    if kind=='FR':
        return context_in_db(db,subject,project_id,function_id,'UC')
    obj=object_content(db,project_id,payload.prd_id,payload.prd_revision_id)
    if obj['function_id']!=function_id or obj['content']['kind']!='PRD' or obj['accepted_revision_id']!=payload.prd_revision_id:
        fail(409,'prd_revision_not_current','请选择本功能最新已确认PRD后生成UC')
    if not obj['content']['markdown_body'].strip():
        fail(409,'prd_content_empty','已确认PRD正文为空')
    return {'project_id':str(project_id),'function_id':str(function_id),'prd_id':str(payload.prd_id),'prd_revision_id':str(payload.prd_revision_id),'markdown_body':obj['content']['markdown_body']}

def prepare(subject, project_id, function_id, kind, payload):
    with mw.write_transaction() as db:
        replay=guard(db,subject,project_id,function_id,kind,payload)
        return (replay,None) if replay else (None,source_in_db(db,subject,project_id,function_id,kind,payload))

def candidate_payloads(subject, kind, payload, candidates):
    if not candidates or len(candidates)>100:
        fail(502,'agent_invalid_response','生成结果必须包含1至100条有效对象')
    result=[]
    try:
        for i,candidate in enumerate(candidates):
            raw=candidate.model_dump(mode='json') if hasattr(candidate,'model_dump') else dict(candidate)
            raw.pop('candidate_id',None);raw.pop('ordinal',None)
            raw['creation_id']=batch_id(subject,kind,payload.client_request_id,i)
            if kind=='UC':
                scenario=raw.pop('scenario')
                if len(scenario['branches'])>4 or len(scenario['exceptions'])>5:
                    fail(502,'uc_scenario_slots_exceeded','Agent结果超过4个分支或5个异常槽位，请对齐UC模板；未丢弃文本或保存任何UC')
                raw.update(kind='UC',prd_revision_id=payload.prd_revision_id,main_flow=scenario['main_flow'],branches=scenario['branches']+['']*(4-len(scenario['branches'])),exceptions=scenario['exceptions']+['']*(5-len(scenario['exceptions'])))
                result.append(UcCreate.model_validate(raw))
            else:
                result.append(FrCreate.model_validate(raw))
    except (KeyError,TypeError,ValueError,ValidationError):
        fail(502,'agent_invalid_response','Agent生成字段不符合保存契约；未保存任何对象')
    return result

def commit(subject, project_id, function_id, kind, payload, snapshot, candidates, trace_id):
    creates=candidate_payloads(subject,kind,payload,candidates)
    with mw.write_transaction() as db:
        replay=guard(db,subject,project_id,function_id,kind,payload)
        if replay:return replay
        if digest(source_in_db(db,subject,project_id,function_id,kind,payload))!=digest(snapshot):
            fail(409,'source_changed','生成期间来源已变化，请重新生成；本批没有保存')
        revisions=[uuid4() for _ in creates]
        result={'generation_id':str(payload.client_request_id),'kind':kind,'status':'succeeded','project_id':str(project_id),'function_id':str(function_id),'source_version':digest(snapshot),'replayed':False,'trace_id':trace_id,'items':[{'artifact_id':str(c.creation_id),'revision_id':str(rev),'function_id':str(function_id),'created':True} for c,rev in zip(creates,revisions)]}
        receipt={'input_hash':input_hash(project_id,function_id,kind,payload),'result':result}
        for i,(creation,revision) in enumerate(zip(creates,revisions)):
            mw.create_artifact_in_db(db,subject,project_id,function_id,creation,str(trace_id),initial_revision_id=revision,creation_metadata={'generation_batch':receipt} if i==0 else None)
        return result

async def generate_and_append(subject, project_id, function_id, kind, payload, request_id):
    from app.services.task_progress import TaskProgress
    progress = await TaskProgress.begin(subject, project_id, payload, kind, function_id)
    await progress.advance('generating', retry=True)
    try:
        result = await _generate_and_append(subject, project_id, function_id, kind, payload, request_id, progress)
    except asyncio.CancelledError:
        await asyncio.shield(progress.failed(uncertain=True))
        raise
    except Exception:
        await progress.failed(uncertain=bool(progress.row and progress.row["stage"] == "saving"))
        raise
    if not result.get('replayed', False):
        progress.observe_completion()
    await progress.saved()  # commit() has exited its transaction before this proof check.
    return result

async def _generate_and_append(subject, project_id, function_id, kind, payload, request_id, progress):
    replay,snapshot=await asyncio.to_thread(prepare,subject,project_id,function_id,kind,payload)
    if replay:return replay
    if kind=='UC':
        prd=uc_generation.ConfirmedPrd(project_id=project_id,function_id=function_id,prd_id=payload.prd_id,revision_id=payload.prd_revision_id,markdown_body=snapshot['markdown_body'])
        generated=await uc_generation.generate_uc_from_prd(prd,client_request_id=payload.client_request_id,request_id=request_id)
        candidates=generated.candidates;trace_id=generated.trace_id
    else:
        generated=await workflow_agent.generate(subject,project_id,function_id,'UC',payload.client_request_id,request_id)
        if generated['source_version']!=digest(snapshot):
            fail(409,'source_changed','来源已经变化，请重新生成；本批没有保存')
        candidates=generated['candidates'];trace_id=request_id
    await progress.advance("saving")
    return await asyncio.to_thread(commit,subject,project_id,function_id,kind,payload,snapshot,candidates,trace_id)
