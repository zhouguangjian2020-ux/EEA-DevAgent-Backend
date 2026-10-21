"""Explicit human adoption of one AI proposal; no Agent calls or new tables.

The immutable revision and its review event are the durable receipt. A candidate
exists only in the browser until this command commits both writes together.
"""
from contextlib import contextmanager

import psycopg

from app.core.database import ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole
from app.core.project_write_database import project_write_connection
from app.services.project_read_errors import database_read_error
from app.schemas.manual_work import AcceptRevision, AiReviewReceipt, FrContent, UcContent, WriteResult
from app.services import manual_work
from app.services.business_access import authorize
from app.services.projects import ProjectReadError
from app.services.workflow_sources import object_content, visible


def _same_sources(content, baseline):
    if isinstance(content, UcContent):
        return str(content.prd_revision_id) == str(baseline['prd_revision_id'])
    if isinstance(content, FrContent):
        actual = {(str(s.uc_id), str(s.uc_revision_id)) for s in content.sources}
        expected = {(str(s['uc_id']), str(s['uc_revision_id'])) for s in baseline['sources']}
        return actual == expected
    return True


def accept_proposal(subject, project_id, artifact_id, payload, request_id):
    """Save and accept in ONE transaction; publication never occurs in chat."""
    with manual_work.write_transaction() as db:
        manual_work.lock_project(db, project_id)
        row = manual_work.artifact(db, project_id, artifact_id)
        actor = authorize(db, subject, project_id, row['type_code'] + '.update',
                          function_id=row['function_id'])
        authorize(db, subject, project_id, row['type_code'] + '.accept',
                  function_id=row['function_id'])
        if row['type_code'] != payload.content.kind:
            manual_work.conflict('对象类型不匹配')
        fingerprint = manual_work.fingerprint(project_id, row['function_id'], payload)
        previous = db.execute('SELECT * FROM ee.artifact_revisions WHERE id=%s',
                              (payload.mutation_id,)).fetchone()
        if previous:
            if (previous['artifact_id'] != artifact_id or previous['created_by'] != actor
                    or previous['metadata'].get('manual_mutation_hash') != fingerprint):
                manual_work.conflict('确认保存标识已绑定其他对象或内容')
            review = db.execute(
                'SELECT revision_id,actor_id FROM ee.review_events WHERE artifact_id=%s AND request_key=%s',
                (artifact_id, str(payload.mutation_id)),
            ).fetchone()
            if not review or review['revision_id'] != payload.mutation_id or review['actor_id'] != actor:
                manual_work.conflict('该标识已有修订但没有对应确认回执，请核对原操作')
            # Do not recheck a historical success against newer heads/sources.
            return WriteResult(function_id=row['function_id'], artifact_id=artifact_id,
                               revision_id=payload.mutation_id, created=False)
        if row['head_revision_id'] != payload.base_revision_id:
            manual_work.conflict('对象已有新版本，AI建议已过期；请保留预览并重新生成')
        baseline = object_content(db, project_id, artifact_id, payload.base_revision_id)['content']
        # Keep omission distinct from an intentional empty value. UC/FR request
        # models provide defaults for legacy manual editing; AI adoption must not
        # silently use those defaults to erase fields omitted by a caller.
        if payload.content.model_fields_set != set(baseline):
            manual_work.conflict('AI建议缺少完整对象字段，请保留预览并重新生成')
        if not _same_sources(payload.content, baseline):
            manual_work.conflict('AI建议不得更换对象的上游来源，请重新生成或使用人工编辑流程')
        # Check every actual dependency in the same project lock/transaction.
        if isinstance(payload.content, UcContent):
            prd = db.execute('SELECT prd_id FROM ee.use_cases WHERE artifact_id=%s',
                             (artifact_id,)).fetchone()
            manual_work.current_source(db, project_id, row['function_id'], 'PRD',
                                       payload.content.prd_revision_id, prd['prd_id'])
        elif isinstance(payload.content, FrContent):
            for source in sorted(payload.content.sources, key=lambda item: str(item.uc_id)):
                manual_work.current_source(db, project_id, row['function_id'], 'UC',
                                           source.uc_revision_id, source.uc_id)
        saved = manual_work.save_revision_in_db(
            db, subject, project_id, artifact_id, payload, request_id,
        )
        manual_work.accept_revision_in_db(
            db, subject, project_id, artifact_id,
            AcceptRevision(mutation_id=payload.mutation_id, revision_id=saved.revision_id),
            request_id,
        )
        return saved


@contextmanager
def read_receipt_db():
    # The existing RO role is not granted review_events. Reuse the narrow RW
    # role in an enforced read-only transaction; never broaden database grants.
    try:
        with project_write_connection(read_only=True) as db:
            yield db
    except ProjectReadError:
        raise
    except (psycopg.Error, ProjectDatabaseNotConfigured, UnsafeProjectDatabaseRole) as error:
        raise database_read_error(error) from error


def receipt(subject, project_id, artifact_id, mutation_id):
    """Read the original actor's durable receipt, even if a newer head exists."""
    with read_receipt_db() as db:
        actor = visible(db, subject, project_id)
        row = db.execute(
            'SELECT a.function_id FROM ee.artifacts a JOIN ee.business_functions f ON f.id=a.function_id '
            'WHERE a.id=%s AND a.project_id=%s AND a.archived_at IS NULL AND f.archived_at IS NULL',
            (artifact_id, project_id),
        ).fetchone()
        if not row:
            raise ProjectReadError(404, 'artifact_not_found', '对象不存在或已删除')
        confirmed = db.execute(
            'SELECT r.id FROM ee.artifact_revisions r JOIN ee.review_events e '
            'ON e.artifact_id=r.artifact_id AND e.revision_id=r.id '
            'WHERE r.id=%s AND r.artifact_id=%s AND r.project_id=%s '
            'AND r.created_by=%s AND e.actor_id=%s AND e.request_key=%s',
            (mutation_id, artifact_id, project_id, actor, actor, str(mutation_id)),
        ).fetchone()
        return AiReviewReceipt(status='confirmed' if confirmed else 'not_found',
                               artifact_id=artifact_id, function_id=row['function_id'],
                               revision_id=mutation_id if confirmed else None)
