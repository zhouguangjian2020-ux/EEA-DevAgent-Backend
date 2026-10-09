"""Request arrival timestamp, scoped to one ASGI request (never user supplied)."""
from contextvars import ContextVar
from datetime import datetime, timezone

_arrival: ContextVar[datetime | None] = ContextVar("task_request_arrival", default=None)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def received_at() -> datetime:
    return _arrival.get() or utcnow()


class RequestTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = _arrival.set(utcnow())
        try:
            await self.app(scope, receive, send)
        finally:
            _arrival.reset(token)
