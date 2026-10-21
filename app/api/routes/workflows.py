from contextlib import aclosing
import asyncio, json
from uuid import UUID
from fastapi import APIRouter, Depends, Cookie, HTTPException, Query
from fastapi.responses import StreamingResponse
from app.api.routes.projects import _create_subject
from app.api.routes.auth import require_identity
from app.schemas.workflows import GenerationRequest, AllocationSave, ThreadCreate, ChatRequest, FrAdopt
from app.schemas.manual_work import AcceptRevision, FrCreate, WriteResult
from app.schemas.workflows import FrGenerationResult, AllocationGenerationResult, ThreadRead, ThreadPage, ThreadHistory, ChatTaskPage
from app.services.projects import ProjectReadError
from app.services import workflow_agent, workflow_allocations, project_chat, chat_tasks, chat_orchestration
router=APIRouter()

def read_subject(ts_session: str | None=Cookie(default=None)):
    return require_identity(ts_session).subject
def error_response(e,request_id=None):
    return HTTPException(e.status,detail={'code':e.code,'message':e.message,'request_id':request_id},headers={'Cache-Control':'no-store'})
async def call(fn,*args):
    try:return await asyncio.to_thread(fn,*args)
    except ProjectReadError as e:raise error_response(e) from e

@router.post('/{project_id}/functions/{function_id}/fr-generations',response_model=FrGenerationResult)
async def fr_generation(project_id:UUID,function_id:UUID,payload:GenerationRequest,identity=Depends(_create_subject)):
    try:return await workflow_agent.generate(identity[0],project_id,function_id,'UC',payload.client_request_id,identity[1])
    except ProjectReadError as e:raise error_response(e,identity[1]) from e

@router.post('/{project_id}/functions/{function_id}/allocation-generations',response_model=AllocationGenerationResult)
async def allocation_generation(project_id:UUID,function_id:UUID,payload:GenerationRequest,identity=Depends(_create_subject)):
    from app.services.task_progress import TaskProgress
    progress = None
    try:
        from app.services.default_subsystems import prepare_allocation_catalogue
        await call(prepare_allocation_catalogue, identity[0], project_id, function_id, identity[1])
        progress = await TaskProgress.begin(identity[0], project_id, payload, 'FR_ALLOCATION', function_id)
        await progress.advance('generating', retry=True)
        result = await workflow_agent.generate(identity[0],project_id,function_id,'FR',payload.client_request_id,identity[1])
        if result['candidates']:
            await progress.preview(len(result['candidates']), result['generation_id'])
        else:
            await progress.empty()
        return result
    except ProjectReadError as e:
        if progress: await progress.failed()
        raise error_response(e,identity[1]) from e
    except BaseException:
        if progress: await progress.failed(uncertain=True)
        raise

@router.post('/{project_id}/functions/{function_id}/allocations',response_model=WriteResult)
async def allocation_save(project_id:UUID,function_id:UUID,payload:AllocationSave,identity=Depends(_create_subject)):
    return await call(workflow_allocations.save_allocation,identity[0],project_id,function_id,payload,identity[1])

@router.post('/{project_id}/allocations/{allocation_id}/accept',response_model=WriteResult)
async def allocation_accept(project_id:UUID,allocation_id:UUID,payload:AcceptRevision,identity=Depends(_create_subject)):
    return await call(workflow_allocations.accept_allocation,identity[0],project_id,allocation_id,payload,identity[1])

@router.get('/{project_id}/conversations',response_model=ThreadPage)
async def thread_list(project_id:UUID,offset:int=Query(default=0,ge=0),subject=Depends(read_subject)):
    rows=await call(project_chat.list_threads,subject,project_id,offset)
    return {'threads':rows[:50],'next_offset':offset+50 if len(rows)>50 else None}

@router.post('/{project_id}/conversations',response_model=ThreadRead)
async def thread_create(project_id:UUID,payload:ThreadCreate,identity=Depends(_create_subject)):
    return await call(project_chat.create_thread,identity[0],project_id,payload)

@router.get('/{project_id}/conversations/{thread_id}',response_model=ThreadHistory)
async def thread_history(project_id:UUID,thread_id:UUID,subject=Depends(read_subject)):
    return await call(project_chat.history,subject,project_id,thread_id)

@router.get('/{project_id}/conversations/{thread_id}/tasks',response_model=ChatTaskPage)
async def thread_tasks(project_id:UUID,thread_id:UUID,offset:int=Query(default=0,ge=0),subject=Depends(read_subject)):
    return await call(chat_tasks.list_tasks,subject,project_id,thread_id,offset)

def sse(event):return 'data: '+json.dumps(event,ensure_ascii=False,default=str)+chr(10)+chr(10)

@router.post('/{project_id}/conversations/{thread_id}/messages/stream')
async def message_stream(project_id:UUID,thread_id:UUID,payload:ChatRequest,identity=Depends(_create_subject)):
    subject,request_id=identity
    try:
        stream=await chat_orchestration.prepare_message_stream(subject,project_id,thread_id,payload,request_id)
    except ProjectReadError as e:
        # Preparation still fails as HTTP, before StreamingResponse sends headers.
        raise error_response(e) from e
    async def events():
        # Closing the transport iterator must also close the business iterator,
        # including its interrupted-turn cleanup when a client disconnects.
        async with aclosing(stream):
            async for event in stream:
                yield sse(event)
    # Keep intermediary compression/transforms from buffering live progress.
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-store, no-transform','X-Accel-Buffering':'no','X-Request-ID':request_id})

@router.post('/{project_id}/functions/{function_id}/fr-candidates/adopt',response_model=WriteResult)
async def adopt_fr(project_id:UUID,function_id:UUID,payload:FrAdopt,identity=Depends(_create_subject)):
    from app.services.manual_work import create_artifact
    candidate=FrCreate(creation_id=payload.creation_id,**payload.content.model_dump())
    return await call(create_artifact,identity[0],project_id,function_id,candidate,identity[1],payload.source_version)


from app.schemas.workflows import UcAppendRequest, GeneratedBatchResult
from app.services import generation_append
from app.services.uc_generation import UcGenerationError

@router.post('/{project_id}/functions/{function_id}/uc-generations/append',response_model=GeneratedBatchResult)
async def uc_generate_append(project_id:UUID,function_id:UUID,payload:UcAppendRequest,identity=Depends(_create_subject)):
    try:return await generation_append.generate_and_append(identity[0],project_id,function_id,'UC',payload,identity[1])
    except (ProjectReadError,UcGenerationError) as e:raise error_response(e,identity[1]) from e

@router.post('/{project_id}/functions/{function_id}/fr-generations/append',response_model=GeneratedBatchResult)
async def fr_generate_append(project_id:UUID,function_id:UUID,payload:GenerationRequest,identity=Depends(_create_subject)):
    try:return await generation_append.generate_and_append(identity[0],project_id,function_id,'FR',payload,identity[1])
    except (ProjectReadError,UcGenerationError) as e:raise error_response(e,identity[1]) from e
