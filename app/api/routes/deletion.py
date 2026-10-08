"""Stage 2: explicit single-object DELETE, no request body or cascade flags."""
from uuid import UUID
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from pydantic import UUID4, ValidationError
from app.api.routes.projects import _authenticated_read, _create_subject, _metadata_mutation
from app.schemas.deletion import DeleteRequest, DeleteResult, DeletionImpact, BulkDeleteSelection, BulkDeleteRequest, BulkDeletionImpact, BulkDeleteResult
from app.schemas.projects import ProjectErrorResponse
from app.services import deletion, bulk_deletion

router = APIRouter(responses={code:{"model":ProjectErrorResponse} for code in (403,404,409,503,504)})


async def delete_parameters(request: Request, mutation_id: UUID4 = Query(...), base_lock_version: str = Query(...)):
    if await request.body():
        raise HTTPException(422, "删除不接受请求体，请通过查询参数提交操作标识和版本")
    if set(request.query_params)!={"mutation_id","base_lock_version"} or any(len(request.query_params.getlist(k))!=1 for k in request.query_params):
        raise HTTPException(422, "删除仅接受唯一的mutation_id和base_lock_version，不支持级联或其他参数")
    try:
        return DeleteRequest(mutation_id=mutation_id,base_lock_version=base_lock_version)
    except ValidationError as error:
        raise HTTPException(422, "删除操作标识或版本格式无效") from error


@router.get("/{project_id}/deletion-impact",response_model=DeletionImpact,summary="检查空项目删除条件，不执行删除")
def project_impact(project_id: UUID,response: Response,ts_session: str | None = Cookie(default=None)):
    return _authenticated_read(response,ts_session,lambda subject:deletion.get_impact(subject,project_id,"project",project_id))


@router.get("/{project_id}/functions/{function_id}/deletion-impact",response_model=DeletionImpact,summary="检查功能是否存在未删除业务对象")
def function_impact(project_id: UUID,function_id: UUID,response: Response,ts_session: str | None = Cookie(default=None)):
    return _authenticated_read(response,ts_session,lambda subject:deletion.get_impact(subject,project_id,"function",function_id))


@router.get("/{project_id}/artifacts/{artifact_id}/deletion-impact",response_model=DeletionImpact,summary="检查PRD/UC/FR/分配当前引用和删除影响")
def artifact_impact(project_id: UUID,artifact_id: UUID,response: Response,ts_session: str | None = Cookie(default=None)):
    return _authenticated_read(response,ts_session,lambda subject:deletion.get_impact(subject,project_id,"artifact",artifact_id))


@router.delete("/{project_id}",response_model=DeleteResult,summary="仅删除空项目；保留历史，不级联")
def delete_project(project_id: UUID,response: Response,identity=Depends(_create_subject),payload=Depends(delete_parameters)):
    return _metadata_mutation(response,identity,deletion.delete_one,project_id,"project",project_id,payload)


@router.delete("/{project_id}/functions/{function_id}",response_model=DeleteResult,summary="仅删除空功能；保留历史，不级联")
def delete_function(project_id: UUID,function_id: UUID,response: Response,identity=Depends(_create_subject),payload=Depends(delete_parameters)):
    return _metadata_mutation(response,identity,deletion.delete_one,project_id,"function",function_id,payload)


@router.delete("/{project_id}/artifacts/{artifact_id}",response_model=DeleteResult,summary="依赖保护下删除PRD/UC/FR/分配；保留历史")
def delete_artifact(project_id: UUID,artifact_id: UUID,response: Response,identity=Depends(_create_subject),payload=Depends(delete_parameters)):
    return _metadata_mutation(response,identity,deletion.delete_one,project_id,"artifact",artifact_id,payload)

# POST accepts a bounded, explicit selection. It is not a loop of single DELETEs.


@router.post("/{project_id}/deletion-batch/impact", response_model=BulkDeletionImpact, summary="检查明确选择的整批对象，不执行删除")
def batch_impact(request: Request, project_id: UUID, payload: BulkDeleteSelection, response: Response, identity=Depends(_create_subject)):
    if request.query_params:
        raise HTTPException(422, "批量接口不接受查询参数，不支持force或cascade")
    return _metadata_mutation(response, identity, bulk_deletion.get_impact, project_id, payload)


@router.post("/{project_id}/deletion-batch", response_model=BulkDeleteResult, summary="同一事务内删除全部明确选中对象，任一失败整批回滚")
def delete_batch(request: Request, project_id: UUID, payload: BulkDeleteRequest, response: Response, identity=Depends(_create_subject)):
    if request.query_params:
        raise HTTPException(422, "批量接口不接受查询参数，不支持force或cascade")
    return _metadata_mutation(response, identity, bulk_deletion.delete_batch, project_id, payload)
