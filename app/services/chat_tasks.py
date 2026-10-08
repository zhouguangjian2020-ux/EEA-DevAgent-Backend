"""Server-owned task receipts, separate from user/assistant conversation turns."""
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.services.manual_work import write_transaction
from app.services.workflow_sources import allowed, digest, fail, read_db, visible
from app.services.projects import ProjectReadError

MAX_CONTEXT_TASKS = 100
PAGE_SIZE = 50
KINDS = frozenset({'PRD', 'UC', 'FR', 'FR_ALLOCATION', 'UC_DIAGRAM'})
STAGES = frozenset({'starting', 'planning', 'awaiting_choice', 'creating_function', 'generating', 'saving', 'review', 'completed'})
STATUSES = frozenset({'running', 'waiting_choice', 'preview', 'succeeded', 'empty', 'failed', 'needs_verification'})
TERMINAL = frozenset({'succeeded', 'empty'})


def _thread(db, subject, project_id, thread_id, lock=False):
    # Ownership is checked on every read and every transition, not only at creation.
    from app.services.project_chat import owned
    return owned(db, subject, project_id, thread_id, lock)


def begin(subject, project_id, thread_id, client_request_id, kind, source, function_id=None):
    """Reserve a single task card for one server-originated request."""
    if kind not in KINDS or not isinstance(source, dict):
        raise ValueError('Unsupported task source')
    request_hash = digest({'kind': kind, 'function_id': str(function_id) if function_id else None, 'source': source})
    with write_transaction() as db:
        _thread(db, subject, project_id, thread_id, True)
        actor = visible(db, subject, project_id)
        if kind != 'PRD' and not function_id:
            raise ValueError('Function ID is required for this task')
        _authorize_task(db, subject, project_id, kind, function_id)
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                   (f'chat-task:{project_id}:{actor}:{client_request_id}',))
        if not db.execute("SELECT to_regclass('ee.chat_tasks') AS present").fetchone()['present']:
            fail(503, 'task_schema_unavailable', '当前数据库尚未安装会话任务记录增量')
        previous = db.execute('''SELECT * FROM ee.chat_tasks
            WHERE project_id=%s AND user_id=%s AND client_request_id=%s''',
            (project_id, actor, client_request_id)).fetchone()
        if previous:
            if (previous['thread_id'] != thread_id or previous['project_id'] != project_id or previous['user_id'] != actor
                    or previous['request_hash'] != request_hash):
                fail(409, 'task_request_conflict', '该任务请求标识已绑定其他输入')
            return previous
        if db.execute('SELECT count(*) AS task_count FROM ee.chat_tasks WHERE thread_id=%s',
                      (thread_id,)).fetchone()['task_count'] >= MAX_CONTEXT_TASKS:
            fail(413, 'conversation_task_full', '此会话任务过多，请新建会话；历史仍保留')
        if function_id and not db.execute('SELECT 1 FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL',
                                          (function_id, project_id)).fetchone():
            fail(404, 'function_not_found', '功能不存在或已删除')
        return db.execute('''INSERT INTO ee.chat_tasks
            (id,thread_id,project_id,user_id,client_request_id,request_hash,task_kind,stage,status,function_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'starting','running',%s) RETURNING *''',
            (uuid4(), thread_id, project_id, actor, client_request_id, request_hash, kind, function_id)).fetchone()


def advance(subject, project_id, thread_id, client_request_id, stage, status='running', *, function_id=None,
            retry=False, expected_updated_at=None):
    """Record a server-observed phase; business success uses complete_saved()."""
    if stage not in STAGES or status not in {'running', 'waiting_choice', 'failed', 'needs_verification'}:
        raise ValueError('Unsupported task transition')
    if ((status == 'waiting_choice') != (stage == 'awaiting_choice')
            or (status in {'failed', 'needs_verification'} and stage != 'completed')
            or (status == 'running' and stage in {'review', 'completed'})):
        raise ValueError('Inconsistent task stage and status')
    with write_transaction() as db:
        _thread(db, subject, project_id, thread_id, True)
        row = db.execute('SELECT * FROM ee.chat_tasks WHERE thread_id=%s AND client_request_id=%s FOR UPDATE',
                         (thread_id, client_request_id)).fetchone()
        if not row:
            fail(404, 'task_not_found', '任务记录不存在')
        if expected_updated_at is not None and row['updated_at'] != expected_updated_at:
            fail(409, 'task_observation_stale', '较早请求不能覆盖更新的任务状态')
        if row['stage'] == stage and row['status'] == status and (function_id is None or row['function_id'] == function_id):
            return row
        if row['status'] in TERMINAL:
            fail(409, 'task_already_finished', '该任务已有最终回执')
        if row['status'] in {'failed', 'needs_verification', 'preview'} and status in {'running', 'waiting_choice'} and not retry:
            fail(409, 'task_retry_required', '请显式重试原任务后继续')
        if function_id and row['function_id'] and function_id != row['function_id']:
            fail(409, 'task_function_conflict', '任务功能身份不能改变')
        if function_id and not db.execute('SELECT 1 FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL',
                                          (function_id, project_id)).fetchone():
            fail(404, 'function_not_found', '功能不存在或已删除')
        effective_function_id = function_id or row['function_id']
        _authorize_task(db, subject, project_id, row['task_kind'], effective_function_id)
        return db.execute('''UPDATE ee.chat_tasks SET stage=%s,status=%s,function_id=coalesce(%s,function_id),
            result_count=NULL,receipt_id=NULL,result_refs='[]'::jsonb,updated_at=now() WHERE id=%s RETURNING *''',
            (stage, status, function_id, row['id'])).fetchone()


def finish_unstored(subject, project_id, thread_id, client_request_id, status, *, count=0, receipt_id=None, expected_updated_at=None):
    """Record a server-side preview or observed empty result, never a saved success."""
    if status == 'preview':
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100 or not isinstance(receipt_id, str) or not 1 <= len(receipt_id) <= 120:
            raise ValueError('Preview requires a bounded count and server receipt')
        stage = 'review'
    elif status == 'empty':
        if type(count) is not int or count != 0 or receipt_id is not None:
            raise ValueError('Empty result cannot carry a receipt')
        stage = 'completed'
    else:
        raise ValueError('Unsupported unsaved result')
    with write_transaction() as db:
        _thread(db, subject, project_id, thread_id, True)
        row = db.execute('SELECT * FROM ee.chat_tasks WHERE thread_id=%s AND client_request_id=%s FOR UPDATE',
                         (thread_id, client_request_id)).fetchone()
        if not row:
            fail(404, 'task_not_found', '任务记录不存在')
        _authorize_task(db, subject, project_id, row['task_kind'], row['function_id'])
        if expected_updated_at is not None and row['updated_at'] != expected_updated_at:
            fail(409, 'task_observation_stale', '较早请求不能覆盖更新的任务状态')
        if row['status'] == status and row['result_count'] == count and row['receipt_id'] == receipt_id:
            return row
        if row['status'] not in {'running', 'waiting_choice'}:
            fail(409, 'task_already_finished', '任务需要明确重试或已完成')
        return db.execute('''UPDATE ee.chat_tasks SET stage=%s,status=%s,result_count=%s,receipt_id=%s,
            updated_at=now() WHERE id=%s RETURNING *''',
            (stage, status, count, receipt_id, row['id'])).fetchone()


def complete_saved(subject, project_id, thread_id, client_request_id):
    """Only a matching, committed PRD intent or UC/FR batch can prove success."""
    with write_transaction() as db:
        _thread(db, subject, project_id, thread_id, True)
        row = db.execute('SELECT * FROM ee.chat_tasks WHERE thread_id=%s AND client_request_id=%s FOR UPDATE',
                         (thread_id, client_request_id)).fetchone()
        if not row:
            fail(404, 'task_not_found', '任务记录不存在')
        if row['status'] == 'succeeded':
            allowed(db, subject, project_id, row['function_id'],
                    _write_capability(row['task_kind'], row['function_id']))
            return row
        if row['task_kind'] == 'PRD':
            receipt = db.execute('''SELECT id,function_id,artifact_id,revision_id FROM ee.prd_creation_intents
                WHERE project_id=%s AND user_id=%s AND client_request_id=%s AND state='saved' ''',
                (project_id, row['user_id'], client_request_id)).fetchone()
            if not receipt or not all(receipt[x] for x in ('function_id', 'artifact_id', 'revision_id')):
                fail(409, 'task_receipt_missing', '尚无已保存PRD回执，不能标记完成')
            refs = [{'artifact_id': str(receipt['artifact_id']), 'revision_id': str(receipt['revision_id'])}]
            function_id = receipt['function_id']
            receipt_id = str(receipt['id'])
        elif row['task_kind'] in ('UC', 'FR'):
            # The first generated object carries the atomic batch receipt. A client-supplied
            # count, Agent candidate list or successful HTTP status is never sufficient.
            from app.services.generation_append import batch_id
            root = batch_id(subject, row['task_kind'], client_request_id)
            batch = db.execute('''SELECT function_id,created_by,metadata FROM ee.artifacts
                WHERE id=%s AND project_id=%s AND type_code=%s AND archived_at IS NULL''',
                (root, project_id, row['task_kind'])).fetchone()
            proof = (batch['metadata'].get('generation_batch') if batch else None)
            result = proof.get('result') if isinstance(proof, dict) else None
            if (not isinstance(result, dict) or batch['created_by'] != row['user_id'] or result.get('status') != 'succeeded'
                    or result.get('generation_id') != str(client_request_id)
                    or result.get('project_id') != str(project_id)
                    or result.get('function_id') != str(batch['function_id'])
                    or result.get('kind') != row['task_kind']):
                fail(409, 'task_receipt_missing', '尚无匹配的已提交生成回执')
            items = result.get('items')
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                fail(409, 'task_receipt_invalid', '生成回执数量或对象不完整')
            refs = []
            for index, item in enumerate(items):
                try:
                    if not isinstance(item, dict) or item.get('created') is not True:
                        raise ValueError('Item not created')
                    artifact_id = UUID(item['artifact_id'])
                    revision_id = UUID(item['revision_id'])
                    item_function_id = UUID(item['function_id'])
                except (ValueError, TypeError, KeyError, AttributeError):
                    fail(409, 'task_receipt_invalid', '生成回执对象标识无效')
                if (artifact_id != batch_id(subject, row['task_kind'], client_request_id, index)
                        or item_function_id != batch['function_id']):
                    fail(409, 'task_receipt_mismatch', '生成回执对象不属于本批次')
                refs.append({'artifact_id': str(artifact_id), 'revision_id': str(revision_id)})
            function_id = batch['function_id']
            receipt_id = str(client_request_id)
        else:
            fail(409, 'task_receipt_unsupported', '此任务没有持久化业务结果回执')
        if row['function_id'] and row['function_id'] != function_id:
            fail(409, 'task_receipt_mismatch', '回执所属功能与任务不一致')
        allowed(db, subject, project_id, function_id, _write_capability(row['task_kind'], function_id))
        for ref in refs:
            evidence = db.execute('''SELECT a.type_code,a.created_by,a.metadata,r.created_by AS revision_creator
                FROM ee.artifacts a JOIN ee.business_functions f ON f.id=a.function_id
                JOIN ee.artifact_revisions r ON r.artifact_id=a.id AND r.id=%s AND r.project_id=%s
                WHERE a.id=%s AND a.project_id=%s AND a.function_id=%s AND a.archived_at IS NULL
                  AND f.archived_at IS NULL''',
                (ref['revision_id'], project_id, ref['artifact_id'], project_id, function_id)).fetchone()
            if (not evidence or evidence['type_code'] != row['task_kind']
                    or evidence['created_by'] != row['user_id'] or evidence['revision_creator'] != row['user_id']
                    or (row['task_kind'] == 'PRD' and
                        (evidence['metadata'] or {}).get('prd_creation_intent_id') != receipt_id)):
                fail(409, 'task_receipt_mismatch', '回执对象已失效，不能标记生成成功')
        return db.execute('''UPDATE ee.chat_tasks SET stage='completed',status='succeeded',function_id=%s,
            result_count=%s,result_refs=%s,receipt_id=%s,updated_at=now() WHERE id=%s RETURNING *''',
            (function_id, len(refs), Jsonb(refs), receipt_id, row['id'])).fetchone()


def _active_function(db, project_id, function_id):
    return bool(function_id and db.execute(
        'SELECT 1 FROM ee.business_functions WHERE id=%s AND project_id=%s AND archived_at IS NULL',
        (function_id, project_id),
    ).fetchone())


def _readable(db, subject, project_id, row):
    function_id = row['function_id']
    if not function_id:
        return True
    if not _active_function(db, project_id, function_id):
        return False
    try:
        allowed(db, subject, project_id, function_id, _capability(row['task_kind']))
    except ProjectReadError as error:
        if error.status in (403, 404):
            return False
        raise
    return True


def _summary_readable(db, subject, project_id, row):
    """Allow a producer to see safe counts without exposing object contents.

    Generation is authorized by a create/read capability that may be broader than
    the artifact's read permission (for example, a project author can create FRs
    but not read FR contents). The count is only a task receipt summary; refs and
    function IDs still require the normal artifact read permission.
    """
    function_id = row['function_id']
    if not _active_function(db, project_id, function_id):
        return False
    try:
        allowed(db, subject, project_id, function_id, _write_capability(row['task_kind'], function_id))
    except ProjectReadError as error:
        if error.status in (403, 404):
            return False
        raise
    return True


def _capability(kind):
    # Match the existing read_accepted_ucs route: project visibility + active function.
    return 'project.read' if kind == 'UC_DIAGRAM' else kind + '.read'


def _authorize_task(db, subject, project_id, kind, function_id):
    if kind == 'PRD' and function_id is None:
        # Planning can also reuse an existing function with scoped PRD.create.
        from app.services.prd_creation_plan import planning_snapshot_in_db
        planning_snapshot_in_db(db, subject, project_id)
    else:
        allowed(db, subject, project_id, function_id, _write_capability(kind, function_id))


def find_request(subject, project_id, client_request_id):
    """Resolve the original thread, never bind an execute/retry to the current UI."""
    with read_db() as db:
        actor = visible(db, subject, project_id)
        if not db.execute("SELECT to_regclass('ee.chat_tasks') AS present").fetchone()['present']:
            return None
        return db.execute('''SELECT * FROM ee.chat_tasks
            WHERE project_id=%s AND user_id=%s AND client_request_id=%s''',
            (project_id, actor, client_request_id)).fetchone()


def _write_capability(kind, function_id):
    if kind == 'PRD' and function_id is None:
        return 'function.create'
    if kind == 'UC_DIAGRAM':
        return 'project.read'
    return kind + '.create'


def _safe(db, subject, project_id, row):
    permitted = _readable(db, subject, project_id, row)
    summary_permitted = permitted or (
        row['result_count'] is not None and _summary_readable(db, subject, project_id, row)
    )
    verified_refs = []
    if summary_permitted:
        for ref in row['result_refs']:
            if not isinstance(ref, dict) or set(ref) != {'artifact_id', 'revision_id'}:
                fail(409, 'task_receipt_invalid', '任务结果引用异常')
            item = db.execute('''SELECT a.type_code FROM ee.artifacts a
                JOIN ee.artifact_revisions r ON r.artifact_id=a.id AND r.id=%s AND r.project_id=%s
                WHERE a.id=%s AND a.project_id=%s AND a.function_id=%s AND a.archived_at IS NULL
                  AND EXISTS(SELECT 1 FROM ee.business_functions f WHERE f.id=a.function_id
                    AND f.project_id=a.project_id AND f.archived_at IS NULL)''',
                (ref['revision_id'], project_id, ref['artifact_id'], project_id, row['function_id'])).fetchone()
            if item and item['type_code'] == row['task_kind']:
                verified_refs.append(ref)
    # A deleted or mismatched result cannot be disclosed through its old count.
    summary_permitted = summary_permitted and len(verified_refs) == len(row['result_refs'])
    refs = verified_refs if permitted else []
    return {'id': row['id'], 'client_request_id': row['client_request_id'], 'task_kind': row['task_kind'],
            'stage': row['stage'], 'status': row['status'],
            'function_id': row['function_id'] if permitted else None,
            'result_count': row['result_count'] if summary_permitted else None, 'result_refs': refs,
            'result_available': permitted and bool(refs) and len(refs) == len(row['result_refs']),
            'unchanged': row['task_kind'] == 'UC_DIAGRAM' and str(row['receipt_id'] or '').startswith('unchanged:'),
            'created_at': row['created_at'], 'updated_at': row['updated_at']}


def list_tasks(subject, project_id, thread_id, offset=0):
    with read_db() as db:
        _thread(db, subject, project_id, thread_id)
        if not db.execute("SELECT to_regclass('ee.chat_tasks') AS task_table").fetchone()['task_table']:
            fail(503, 'task_schema_unavailable', '当前数据库尚未安装会话任务记录增量')
        rows = db.execute('''SELECT * FROM ee.chat_tasks WHERE thread_id=%s AND project_id=%s
            ORDER BY created_at,id LIMIT %s OFFSET %s''',
            (thread_id, project_id, PAGE_SIZE + 1, offset)).fetchall()
        return {'tasks': [_safe(db, subject, project_id, row) for row in rows[:PAGE_SIZE]],
                'next_offset': offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None}


def context_in_db(db, subject, project_id, thread_id):
    # Pre-S4 preview databases still serve ordinary conversation turns.
    if not db.execute("SELECT to_regclass('ee.chat_tasks') AS task_table").fetchone()['task_table']:
        return []
    rows = db.execute('''SELECT * FROM ee.chat_tasks WHERE thread_id=%s AND project_id=%s
        ORDER BY created_at,id LIMIT %s''', (thread_id, project_id, MAX_CONTEXT_TASKS + 1)).fetchall()
    if len(rows) > MAX_CONTEXT_TASKS:
        fail(413, 'conversation_task_full', '此会话任务过多，请新建会话；历史仍保留')
    return [{'source': 'server_task_receipt', 'kind': task['task_kind'], 'stage': task['stage'],
             'status': task['status'], 'count': task['result_count'],
             'result_refs': task['result_refs'] if task['result_available'] else []}
            for row in rows for task in [_safe(db, subject, project_id, row)]]
