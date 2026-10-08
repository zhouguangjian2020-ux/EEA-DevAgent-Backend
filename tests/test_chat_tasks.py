"""Pure validation and pre-S4 read compatibility for task receipts."""
from contextlib import contextmanager
from uuid import uuid4

import pytest

from app.services import chat_tasks
from app.services.projects import ProjectReadError


@pytest.mark.parametrize('stage,status', [
    ('review', 'running'), ('completed', 'running'),
    ('awaiting_choice', 'running'), ('generating', 'waiting_choice'),
    ('generating', 'failed'), ('starting', 'needs_verification'),
])
def test_invalid_stage_status_rejected_before_database(stage, status):
    with pytest.raises(ValueError, match='Inconsistent task'):
        chat_tasks.advance('nobody', uuid4(), uuid4(), uuid4(), stage, status)


@pytest.mark.parametrize('status,count,receipt', [
    ('preview', 0, 'r'), ('preview', True, 'r'), ('preview', 1, None),
    ('empty', 0, 'r'), ('empty', 0.0, None), ('succeeded', 1, 'r'),
])
def test_unstored_result_cannot_claim_business_success(status, count, receipt):
    with pytest.raises(ValueError):
        chat_tasks.finish_unstored('nobody', uuid4(), uuid4(), uuid4(), status,
                                   count=count, receipt_id=receipt)


def test_legacy_conversations_have_empty_task_context_and_explicit_read_error(monkeypatch):
    class Database:
        def execute(self, statement, args=None):
            assert "to_regclass('ee.chat_tasks')" in statement
            return self

        def fetchone(self):
            return {'task_table': None}

    database = Database()

    @contextmanager
    def old_database():
        yield database

    monkeypatch.setattr(chat_tasks, 'read_db', old_database)
    monkeypatch.setattr(chat_tasks, '_thread', lambda *args: None)
    assert chat_tasks.context_in_db(database, 'legacy-user', uuid4(), uuid4()) == []
    with pytest.raises(ProjectReadError) as caught:
        chat_tasks.list_tasks('legacy-user', uuid4(), uuid4())
    assert caught.value.code == 'task_schema_unavailable'
