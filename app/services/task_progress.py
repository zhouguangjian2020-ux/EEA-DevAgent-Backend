"""Small workflow-to-receipt adapter; no queue, business writes or Agent calls."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from app.services import chat_tasks, task_timing

log = logging.getLogger(__name__)


@dataclass
class TaskProgress:
    subject: str
    project_id: object
    row: dict | None
    completion_at: datetime | None = None

    @classmethod
    async def begin(cls, subject, project_id, payload, kind, function_id=None):
        thread = getattr(payload, 'thread_id', None)
        row = None
        if thread is not None:
            source = payload.model_dump(mode='json', exclude={'thread_id'} |
                {field for field in ('target_function_id', 'new_function_name') if not getattr(payload, field, None)})
            # Strict before starting paid/work-producing requests: never silently lose ownership.
            row = await asyncio.to_thread(chat_tasks.begin, subject, project_id, thread,
                payload.client_request_id, kind, source, function_id)
        return cls(subject, project_id, row)

    @classmethod
    async def original(cls, subject, project_id, client_request_id):
        row = await asyncio.to_thread(chat_tasks.find_request, subject, project_id, client_request_id)
        return cls(subject, project_id, row)

    async def _record(self, operation, *args, **kwargs):
        if self.row is None:
            return
        try:
            row = await asyncio.to_thread(operation, self.subject, self.project_id,
                self.row['thread_id'], self.row['client_request_id'], *args, **kwargs)
            self.row = row
        except Exception as error:
            # Recording is secondary to business truth, especially after a commit.
            # Do not emit content, URLs, credentials or raw database exceptions.
            log.warning('task_receipt_update_failed task=%s type=%s', self.row['id'], type(error).__name__)
            if getattr(error, 'code', None) == 'task_observation_stale':
                self.row = None

    async def advance(self, stage, status='running', *, function_id=None, retry=False):
        if self.row is None or self.row['status'] in chat_tasks.TERMINAL:
            return
        await self._record(chat_tasks.advance, stage, status, function_id=function_id,
            retry=retry, expected_updated_at=self.row['updated_at'])

    def observe_completion(self):
        # Called only after an actual successful execution, never on cached read/replay.
        self.completion_at = task_timing.utcnow()

    async def saved(self):
        if self.row is None:
            return
        await self._record(chat_tasks.complete_saved, observed_at=self.completion_at,
            expected_attempt_id=self.row.get('attempt_id'))

    async def preview(self, count, receipt_id):
        if self.row is None:
            return
        await self._record(chat_tasks.finish_unstored, 'preview', count=count,
            receipt_id=str(receipt_id), expected_updated_at=self.row['updated_at'])

    async def empty(self):
        if self.row is None:
            return
        await self._record(chat_tasks.finish_unstored, 'empty', expected_updated_at=self.row['updated_at'])

    async def failed(self, *, uncertain=False):
        await self.advance('completed', 'needs_verification' if uncertain else 'failed')

    async def prd_result(self, result):
        if result.status == 'saved':
            await self.saved()
        elif result.status == 'planned':
            await self.advance('awaiting_choice', 'waiting_choice')
        elif result.status in {'failed', 'interrupted', 'blocked'}:
            await self.failed(uncertain=result.status == 'interrupted')
        elif result.status in {'planning', 'generating'}:
            await self.advance(result.status, function_id=result.function_id)
