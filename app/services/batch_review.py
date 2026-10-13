"""One connection/transaction, item savepoints, no new database objects or parallel writes."""
import psycopg
from app.schemas.batch_review import BatchReviewResult, ReviewItemResult
from app.schemas.manual_work import AcceptRevision
from app.services import manual_work, workflow_allocations
from app.services.business_access import authorize
from app.services.resource_locks import lock_project, lock_function
from app.services.projects import ProjectReadError

_ITEM_ERRORS = (ProjectReadError, psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable,
                psycopg.errors.UniqueViolation, psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)

def _error(exc):
    if isinstance(exc, ProjectReadError):
        return exc.code, exc.message
    if isinstance(exc, (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable)):
        return "business_write_timeout", "操作超时，请保留原请求重试"
    return "business_write_conflict", "数据或版本已变化，请刷新核对后重试；原输入已保留"

def _scope(db, subject, project_id, function_id, capability):
    # Hold the same project/function mutation locks outside all item savepoints.
    authorize(db, subject, project_id, "project.read")
    lock_project(db, project_id)
    lock_function(db, project_id, function_id)
    authorize(db, subject, project_id, capability, function_id=function_id)

def accept_artifacts(subject, project_id, function_id, payload, request_id):
    results = []
    with manual_work.write_transaction() as db:
        _scope(db, subject, project_id, function_id, payload.kind + ".accept")
        for item in payload.items:
            try:
                with db.transaction():
                    receipt = manual_work.accept_revision_in_db(db, subject, project_id, item.artifact_id,
                        AcceptRevision(mutation_id=item.mutation_id, revision_id=item.revision_id), request_id,
                        expected_function=function_id, expected_kind=payload.kind)
                results.append(ReviewItemResult(artifact_id=item.artifact_id, revision_id=receipt.revision_id, accepted=True))
            except _ITEM_ERRORS as exc:
                code, message = _error(exc)
                results.append(ReviewItemResult(artifact_id=item.artifact_id, accepted=False, error_code=code, error_message=message))
    # Do not publish successes before the outer commit succeeds.
    return BatchReviewResult(function_id=function_id, items=results)

def accept_allocations(subject, project_id, function_id, payload, request_id):
    results = []
    with manual_work.write_transaction() as db:
        _scope(db, subject, project_id, function_id, "FR_ALLOCATION.accept")
        # Catalogue pointer cannot change while a shared source digest is reused.
        db.execute("SELECT current_version_id FROM ee.project_subsystem_designs WHERE project_id=%s FOR SHARE", (project_id,)).fetchone()
        source_digest_cache = {}
        for item in payload.items:
            revision, saved = item.revision_id, False
            try:
                if item.save is not None:
                    with db.transaction():
                        receipt = workflow_allocations.save_allocation_in_db(db, subject, project_id, function_id,
                            item.save, request_id, source_digest_cache=source_digest_cache)
                    revision, saved = receipt.revision_id, True
                # Preserve the existing saved-draft result if its acceptance fails.
                with db.transaction():
                    workflow_allocations.accept_allocation_in_db(db, subject, project_id, item.allocation_id,
                        AcceptRevision(mutation_id=item.mutation_id, revision_id=revision), request_id,
                        expected_function=function_id)
                results.append(ReviewItemResult(artifact_id=item.allocation_id, revision_id=revision, accepted=True, saved=saved))
            except _ITEM_ERRORS as exc:
                code, message = _error(exc)
                results.append(ReviewItemResult(artifact_id=item.allocation_id, revision_id=revision if saved else None,
                    accepted=False, saved=saved, error_code=code, error_message=message))
    return BatchReviewResult(function_id=function_id, items=results)
