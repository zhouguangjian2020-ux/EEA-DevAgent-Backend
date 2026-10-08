"""S4 task receipts against the disposable localhost PostgreSQL fixture only."""
import os
from uuid import UUID, uuid4

import pytest

from app.core.security import create_session_token
from app.schemas.workflows import ChatRequest
from app.services import chat_tasks, project_chat
from app.services.projects import ProjectReadError
from tests.test_manual_work_postgres import HEADERS, client, db
from tests.test_prd_creation_plan_postgres import new_project, new_function, peer
from tests.test_prd_creation_postgres import agents, start, choose
from tests.test_generation_append_postgres import uc_agent, uc_input
from tests.test_workflows_postgres import sources, agent


@pytest.fixture(autouse=True)
def task_schema_required():
    if os.getenv('TEST_TASK_RECORDS_ENABLED') != '1':
        pytest.skip('Disposable localhost S4 task schema required')


def thread(client, project):
    response = client.post(f'/v1/projects/{project}/conversations', headers=HEADERS,
                           json={'client_request_id': str(uuid4())})
    assert response.status_code == 200, response.text
    return UUID(response.json()['id'])


def tasks(client, project, tid, status=200):
    response = client.get(f'/v1/projects/{project}/conversations/{tid}/tasks')
    assert response.status_code == status, response.text
    return response.json()


def error(code, operation):
    with pytest.raises(ProjectReadError) as caught:
        operation()
    assert caught.value.code == code


def grant_function_reader(db, project, function_id):
    actor = db.execute("SELECT id FROM ee.app_users WHERE external_subject='engineer01'").fetchone()[0]
    role = db.execute("SELECT id FROM ee.access_roles WHERE code='function_viewer'").fetchone()[0]
    db.execute('''INSERT INTO ee.access_bindings
        (id,project_id,user_id,role_id,scope_kind,function_id,created_by,updated_by)
        VALUES(%s,%s,%s,%s,'function',%s,%s,%s)''',
        (uuid4(), project, actor, role, function_id, actor, actor))


def test_prd_receipt_history_title_and_trusted_context(client, db, agents):
    project = UUID(new_project(client))
    tid, cid = thread(client, project), uuid4()
    row = chat_tasks.begin('engineer01', project, tid, cid, 'PRD', {'prompt': '循环录制'})
    assert chat_tasks.begin('engineer01', project, tid, cid, 'PRD', {'prompt': '循环录制'})['id'] == row['id']
    error('task_request_conflict', lambda: chat_tasks.begin('engineer01', project, tid, cid, 'PRD', {'prompt': '不同'}))
    error('task_receipt_missing', lambda: chat_tasks.complete_saved('engineer01', project, tid, cid))
    intent = start(client, str(project), cid)
    saved = choose(client, str(project), intent)
    done = chat_tasks.complete_saved('engineer01', project, tid, cid)
    assert done['status'] == 'succeeded' and done['result_count'] == 1
    assert done['function_id'] == UUID(saved['function_id'])
    assert chat_tasks.complete_saved('engineer01', project, tid, cid)['id'] == row['id']
    assert db.execute('SELECT count(*) FROM ee.chat_turns WHERE thread_id=%s', (tid,)).fetchone()[0] == 0
    initial = tasks(client, project, tid)['tasks'][0]
    assert initial['result_count'] == 1 and not initial['result_available']
    assert initial['result_refs'] == [] and initial['function_id'] is None
    grant_function_reader(db, project, done['function_id'])
    listing = tasks(client, project, tid)['tasks']
    assert len(listing) == 1 and listing[0]['result_available']
    assert listing[0]['result_refs'] == [{'artifact_id': saved['artifact_id'], 'revision_id': saved['revision_id']}]

    payload = ChatRequest(client_request_id=uuid4(), message='第一问：请分析这个功能')
    context = project_chat.start_turn('engineer01', project, tid, payload)['context']
    assert context['task_receipts'] == [{'source': 'server_task_receipt', 'kind': 'PRD',
                                          'stage': 'completed', 'status': 'succeeded', 'count': 1,
                                          'result_refs': listing[0]['result_refs']}]
    assert context['messages'] == [] and context['attachments'] == []
    project_chat.finish_turn('engineer01', project, tid, payload.client_request_id, '回答', 'completed')
    names = client.get(f'/v1/projects/{project}/conversations').json()['threads']
    assert next(item for item in names if item['id'] == str(tid))['title'] == payload.message
    assert len(client.get(f'/v1/projects/{project}/conversations/{tid}').json()['turns']) == 1

    # This project is private to the test: revoke both create and read scopes.
    db.execute('''UPDATE ee.access_bindings
        SET role_id=(SELECT id FROM ee.access_roles WHERE code='project_api_reader')
        WHERE project_id=%s AND user_id=%s''', (project, done['user_id']))
    hidden = tasks(client, project, tid)['tasks'][0]
    assert hidden['result_count'] is None and hidden['result_refs'] == []
    assert hidden['function_id'] is None and not hidden['result_available']
    with project_chat.read_db() as conn:
        context = chat_tasks.context_in_db(conn, 'engineer01', project, tid)
    assert context[0]['count'] is None and context[0]['result_refs'] == []


def test_uc_batch_proves_real_new_count_and_does_not_duplicate(client, db, sources, uc_agent):
    fid, prd, _ = sources
    project, tid, cid = UUID('10d4c8a0-ff3a-56c3-abf6-18dc3b531e62'), None, uuid4()
    tid = thread(client, project)
    chat_tasks.begin('engineer01', project, tid, cid, 'UC', {'prd_revision_id': prd['revision_id']}, UUID(fid))
    error('task_receipt_missing', lambda: chat_tasks.complete_saved('engineer01', project, tid, cid))
    body = {**uc_input(prd), 'client_request_id': str(cid)}
    response = client.post(f'/v1/projects/{project}/functions/{fid}/uc-generations/append', headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    generated = response.json()
    done = chat_tasks.complete_saved('engineer01', project, tid, cid)
    assert done['result_count'] == len(generated['items']) == 2
    assert {ref['artifact_id'] for ref in done['result_refs']} == {item['artifact_id'] for item in generated['items']}
    assert chat_tasks.complete_saved('engineer01', project, tid, cid)['id'] == done['id']
    assert db.execute('SELECT count(*) FROM ee.chat_tasks WHERE thread_id=%s', (tid,)).fetchone()[0] == 1
    assert all(db.execute('SELECT accepted_revision_id FROM ee.artifacts WHERE id=%s',
                          (item['artifact_id'],)).fetchone()[0] is None for item in generated['items'])
    assert tasks(client, project, tid)['tasks'][0]['result_count'] == 2
    # Even with create permission, a partial/deleted batch no longer exposes count.
    db.execute('UPDATE ee.artifacts SET archived_at=now() WHERE id=%s',
               (generated['items'][0]['artifact_id'],))
    hidden = tasks(client, project, tid)['tasks'][0]
    assert hidden['result_count'] is None and hidden['result_refs'] == []
    assert not hidden['result_available']


def test_fr_batch_uses_committed_receipt_not_agent_candidates(client, db, sources, agent):
    fid, _, _ = sources
    project, cid = UUID('10d4c8a0-ff3a-56c3-abf6-18dc3b531e62'), uuid4()
    tid = thread(client, project)
    chat_tasks.begin('engineer01', project, tid, cid, 'FR', {'button': 'generate'}, UUID(fid))
    generated = client.post(f'/v1/projects/{project}/functions/{fid}/fr-generations/append', headers=HEADERS,
                            json={'client_request_id': str(cid)})
    assert generated.status_code == 200, generated.text
    done = chat_tasks.complete_saved('engineer01', project, tid, cid)
    assert done['result_count'] == len(generated.json()['items']) == 1
    summary = tasks(client, project, tid)['tasks'][0]
    assert summary['result_count'] == 1 and not summary['result_available']
    assert summary['result_refs'] == [] and summary['function_id'] is None
    grant_function_reader(db, project, UUID(fid))
    assert tasks(client, project, tid)['tasks'][0]['result_available']
    db.execute('UPDATE ee.artifacts SET archived_at=now() WHERE id=%s',
               (generated.json()['items'][0]['artifact_id'],))
    hidden = tasks(client, project, tid)['tasks'][0]
    assert hidden['result_count'] is None and hidden['result_refs'] == []
    assert not hidden['result_available']


def test_preview_failure_retry_empty_and_unsupported_success(client, db, sources):
    fid, _, _ = sources
    project = UUID('10d4c8a0-ff3a-56c3-abf6-18dc3b531e62')
    tid, cid = thread(client, project), uuid4()
    chat_tasks.begin('engineer01', project, tid, cid, 'FR_ALLOCATION', {'button': 'suggest'}, UUID(fid))
    error('task_receipt_unsupported', lambda: chat_tasks.complete_saved('engineer01', project, tid, cid))
    preview = chat_tasks.finish_unstored('engineer01', project, tid, cid, 'preview', count=2, receipt_id='review-1')
    assert preview['stage'] == 'review' and preview['result_count'] == 2
    assert chat_tasks.finish_unstored('engineer01', project, tid, cid, 'preview', count=2, receipt_id='review-1')['id'] == preview['id']
    error('task_retry_required', lambda: chat_tasks.advance('engineer01', project, tid, cid, 'generating'))
    again = chat_tasks.advance('engineer01', project, tid, cid, 'generating', retry=True)
    assert again['result_count'] is None and again['receipt_id'] is None
    failed = chat_tasks.advance('engineer01', project, tid, cid, 'completed', 'failed')
    assert failed['status'] == 'failed'
    error('task_retry_required', lambda: chat_tasks.advance('engineer01', project, tid, cid, 'generating'))
    chat_tasks.advance('engineer01', project, tid, cid, 'generating', retry=True)
    done = chat_tasks.finish_unstored('engineer01', project, tid, cid, 'empty')
    assert done['status'] == 'empty' and done['result_count'] == 0
    error('task_already_finished', lambda: chat_tasks.advance('engineer01', project, tid, cid, 'generating', retry=True))

    diagram = uuid4()
    # The task must not require a new UC.read grant absent from the diagram route.
    chat_tasks.begin('engineer01', project, tid, diagram, 'UC_DIAGRAM', {'refresh': True}, UUID(fid))
    assert chat_tasks.finish_unstored('engineer01', project, tid, diagram, 'preview', count=1, receipt_id='diagram-v1')['status'] == 'preview'
    error('task_receipt_unsupported', lambda: chat_tasks.complete_saved('engineer01', project, tid, diagram))


def test_isolation_revocation_and_archived_result(client, db):
    project = UUID(new_project(client))
    foreign = UUID(new_project(client))
    fid = UUID(new_function(client, str(project), '任务隔离-' + uuid4().hex[:8]))
    tid, cid = thread(client, project), uuid4()
    chat_tasks.begin('engineer01', project, tid, cid, 'PRD', {'button': 'generate'}, fid)
    chat_tasks.finish_unstored('engineer01', project, tid, cid, 'preview', count=1, receipt_id='mock-preview')
    _, peer_id, subject = peer(db, project)
    client.cookies.set('ts_session', create_session_token(subject))
    assert tasks(client, project, tid, 404)['detail']['code'] == 'thread_not_found'
    error('thread_not_found', lambda: chat_tasks.begin(subject, project, tid, uuid4(), 'PRD', {}))
    client.cookies.set('ts_session', create_session_token('engineer01'))
    assert tasks(client, foreign, tid, 404)['detail']['code'] == 'thread_not_found'
    role = db.execute("SELECT id FROM ee.access_roles WHERE code='project_api_reader'").fetchone()[0]
    actor = db.execute("SELECT id FROM ee.app_users WHERE external_subject='engineer01'").fetchone()[0]
    db.execute("UPDATE ee.access_bindings SET role_id=%s WHERE project_id=%s AND user_id=%s", (role, project, actor))
    hidden = tasks(client, project, tid)['tasks'][0]
    assert hidden['status'] == 'preview' and hidden['result_count'] is None and hidden['function_id'] is None
    assert not hidden['result_available'] and hidden['result_refs'] == []
    error('workflow_forbidden', lambda: chat_tasks.finish_unstored('engineer01', project, tid, cid, 'preview', count=1, receipt_id='mock-preview'))
    db.execute('UPDATE ee.business_functions SET archived_at=now() WHERE id=%s', (fid,))
    assert tasks(client, project, tid)['tasks'][0]['function_id'] is None


def test_bounded_tasks_and_context_without_truncation(client, db, monkeypatch):
    project = UUID(new_project(client))
    tid = thread(client, project)
    monkeypatch.setattr(chat_tasks, 'MAX_CONTEXT_TASKS', 2)
    cids = [uuid4(), uuid4()]
    for cid in cids:
        chat_tasks.begin('engineer01', project, tid, cid, 'PRD', {'button': 'generate'})
    error('conversation_task_full', lambda: chat_tasks.begin('engineer01', project, tid, uuid4(), 'PRD', {}))
    assert chat_tasks.begin('engineer01', project, tid, cids[0], 'PRD', {'button': 'generate'})['client_request_id'] == cids[0]
    assert len(tasks(client, project, tid)['tasks']) == 2
    with project_chat.read_db() as conn:
        assert len(chat_tasks.context_in_db(conn, 'engineer01', project, tid)) == 2
    original = chat_tasks.context_in_db
    monkeypatch.setattr(chat_tasks, 'context_in_db', lambda *args: [{'source': 'server_task_receipt', 'oversize': 'x' * 1_500_000}])
    error('context_too_large', lambda: project_chat.start_turn(
        'engineer01', project, tid, ChatRequest(client_request_id=uuid4(), message='不能静默截断')))
    monkeypatch.setattr(chat_tasks, 'context_in_db', original)
    assert db.execute('SELECT count(*) FROM ee.chat_turns WHERE thread_id=%s', (tid,)).fetchone()[0] == 0


# S5: exercise actual workflow producers rather than manually marking their receipts.
def test_producer_prd_multiphase_original_thread_and_replay(client, db, agents):
    project = UUID(new_project(client)); tid = thread(client, project); cid = uuid4()
    response = client.post(f'/v1/projects/{project}/prd-creations/intents', headers=HEADERS,
        json={'client_request_id': str(cid), 'message': '自动录制', 'thread_id': str(tid)})
    assert response.status_code == 200, response.text
    planned = response.json(); initial = tasks(client, project, tid)['tasks']
    assert len(initial) == 1 and initial[0]['status'] == 'waiting_choice'
    other = thread(client, project)
    response = client.post(f'/v1/projects/{project}/prd-creations/intents', headers=HEADERS,
        json={'client_request_id': str(cid), 'message': '自动录制', 'thread_id': str(other)})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'task_request_conflict'
    saved = choose(client, str(project), planned)
    done = tasks(client, project, tid)['tasks']
    assert len(done) == 1 and done[0]['id'] == initial[0]['id'] and done[0]['status'] == 'succeeded'
    assert tasks(client, project, other)['tasks'] == []
    # Retrying a pre-S5 body with no thread still returns the same business receipt.
    legacy = start(client, str(project), cid, '自动录制')
    assert legacy['artifact_id'] == saved['artifact_id']
    assert db.execute('SELECT count(*) FROM ee.chat_turns WHERE thread_id=%s', (tid,)).fetchone()[0] == 0


def test_producer_uc_commit_replay_and_cross_thread_conflict(client, db, sources, uc_agent):
    fid, prd, _ = sources; project = UUID('10d4c8a0-ff3a-56c3-abf6-18dc3b531e62')
    tid = thread(client, project); other = thread(client, project)
    body = {**uc_input(prd), 'thread_id': str(tid)}
    path = f'/v1/projects/{project}/functions/{fid}/uc-generations/append'
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    done = tasks(client, project, tid)['tasks']
    assert len(done) == 1 and done[0]['status'] == 'succeeded'
    assert done[0]['result_count'] == len(response.json()['items']) == 2
    assert done[0]['result_refs'] == [] and not done[0]['result_available']
    grant_function_reader(db, project, UUID(fid))
    assert tasks(client, project, tid)['tasks'][0]['result_count'] == len(response.json()['items']) == 2
    replay = client.post(path, headers=HEADERS, json=body)
    assert replay.status_code == 200 and replay.json()['replayed'] and len(uc_agent) == 1
    conflict = client.post(path, headers=HEADERS, json={**body, 'thread_id': str(other)})
    assert conflict.status_code == 409 and len(uc_agent) == 1
    assert tasks(client, project, other)['tasks'] == []
    legacy = client.post(path, headers=HEADERS, json={k:v for k,v in body.items() if k != 'thread_id'})
    assert legacy.status_code == 200 and legacy.json()['items'] == response.json()['items']



def test_producer_fr_receipt_failure_does_not_undo_committed_business(client, db, sources, agent, monkeypatch):
    fid, _, _ = sources; project = UUID('10d4c8a0-ff3a-56c3-abf6-18dc3b531e62')
    tid = thread(client, project); body = {'client_request_id': str(uuid4()), 'thread_id': str(tid)}
    original = chat_tasks.complete_saved
    def unavailable(*args): raise RuntimeError('private error must not escape')
    monkeypatch.setattr(chat_tasks, 'complete_saved', unavailable)
    path = f'/v1/projects/{project}/functions/{fid}/fr-generations/append'
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 200 and response.json()['items'], response.text
    assert tasks(client, project, tid)['tasks'][0]['status'] == 'running'
    monkeypatch.setattr(chat_tasks, 'complete_saved', original)
    replay = client.post(path, headers=HEADERS, json=body)
    assert replay.status_code == 200 and replay.json()['replayed']
    assert tasks(client, project, tid)['tasks'][0]['status'] == 'succeeded'


def test_producer_failure_retry_and_stale_observation(client, db, sources, uc_agent, monkeypatch):
    from app.services import uc_generation
    fid, prd, _ = sources; project = UUID('10d4c8a0-ff3a-56c3-abf6-18dc3b531e62')
    tid = thread(client, project); body = {**uc_input(prd), 'thread_id': str(tid)}
    path = f'/v1/projects/{project}/functions/{fid}/uc-generations/append'
    original = uc_generation.generate_uc_from_prd
    async def rejected(*args, **kwargs): raise uc_generation.UcGenerationError(502, 'agent_upstream_error', '失败')
    monkeypatch.setattr(uc_generation, 'generate_uc_from_prd', rejected)
    assert client.post(path, headers=HEADERS, json=body).status_code == 502
    assert tasks(client, project, tid)['tasks'][0]['status'] == 'failed'
    monkeypatch.setattr(uc_generation, 'generate_uc_from_prd', original)
    assert client.post(path, headers=HEADERS, json=body).status_code == 200
    assert tasks(client, project, tid)['tasks'][0]['status'] == 'succeeded'
    cid = uuid4()
    row = chat_tasks.begin('engineer01', project, tid, cid, 'FR', {}, UUID(fid))
    current = chat_tasks.advance('engineer01', project, tid, cid, 'generating', expected_updated_at=row['updated_at'])
    error('task_observation_stale', lambda: chat_tasks.advance('engineer01', project, tid, cid,
        'completed', 'failed', expected_updated_at=row['updated_at']))
    assert chat_tasks.find_request('engineer01', project, cid)['updated_at'] == current['updated_at']
