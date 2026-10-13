"""Authenticated JSON mutations. Business checks and transactions stay in services."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.routes.projects import _create_subject
from app.schemas.manual_work import AcceptRevision, CreateArtifact, FunctionCreate, SaveRevision, WriteResult
from app.schemas.projects import ProjectErrorResponse
from app.services import manual_work
from app.schemas.batch_review import ArtifactReviewBatch, AllocationReviewBatch, BatchReviewResult
from app.services import batch_review
from app.services.projects import ProjectReadError

router = APIRouter(responses={code: {"model": ProjectErrorResponse} for code in (403,404,409,503,504)})


def execute(response, identity, operation, *args):
    subject, request_id = identity
    try:
        result = operation(subject, *args, request_id)
    except ProjectReadError as error:
        raise HTTPException(error.status, detail={"code":error.code,"message":error.message,"request_id":request_id},
                            headers={"X-Request-ID":request_id,"Cache-Control":"no-store"}) from error
    response.status_code = 201 if result.created else 200
    return result


@router.post("/{project_id}/functions", response_model=WriteResult, status_code=201)
def create_function(project_id: UUID, payload: FunctionCreate, response: Response, identity=Depends(_create_subject)):
    return execute(response,identity,manual_work.create_function,project_id,payload)


@router.post("/{project_id}/functions/{function_id}/artifacts", response_model=WriteResult, status_code=201)
def create_artifact(project_id: UUID, function_id: UUID, payload: CreateArtifact, response: Response, identity=Depends(_create_subject)):
    return execute(response,identity,manual_work.create_artifact,project_id,function_id,payload)


@router.post("/{project_id}/artifacts/{artifact_id}/revisions", response_model=WriteResult, status_code=201)
def save_revision(project_id: UUID, artifact_id: UUID, payload: SaveRevision, response: Response, identity=Depends(_create_subject)):
    return execute(response,identity,manual_work.save_revision,project_id,artifact_id,payload)


@router.post("/{project_id}/artifacts/{artifact_id}/accept", response_model=WriteResult, status_code=201)
def accept_revision(project_id: UUID, artifact_id: UUID, payload: AcceptRevision, response: Response, identity=Depends(_create_subject)):
    return execute(response,identity,manual_work.accept_revision,project_id,artifact_id,payload)



def execute_batch(identity, operation, *args):
    subject, request_id = identity
    try:
        return operation(subject, *args, request_id)
    except ProjectReadError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message, "request_id": request_id},
                            headers={"X-Request-ID": request_id, "Cache-Control": "no-store"}) from error

@router.post("/{project_id}/functions/{function_id}/artifacts/batch-accept", response_model=BatchReviewResult)
def batch_accept_artifacts(project_id: UUID, function_id: UUID, payload: ArtifactReviewBatch, identity=Depends(_create_subject)):
    return execute_batch(identity, batch_review.accept_artifacts, project_id, function_id, payload)

@router.post("/{project_id}/functions/{function_id}/allocations/batch-accept", response_model=BatchReviewResult)
def batch_accept_allocations(project_id: UUID, function_id: UUID, payload: AllocationReviewBatch, identity=Depends(_create_subject)):
    return execute_batch(identity, batch_review.accept_allocations, project_id, function_id, payload)
