"""Opt-in, bounded, in-memory observations. Never a business-data writer."""
import contextvars
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.config import settings

REDACTED = "[REDACTED]"
MAX_EVENT_BYTES = 2_000_000
MAX_RUN_BYTES = 8_000_000
MAX_TOTAL_BYTES = 64_000_000
MAX_EVENTS = 2000
TTL = 900
_lock = threading.RLock()
_runs: dict[str, dict] = {}
_aliases: dict[str, str] = {}


@dataclass
class Capture:
    run_id: str
    span_id: str
    enabled: bool = True


current_capture: contextvars.ContextVar[Capture | None] = contextvars.ContextVar("ts_capture", default=None)


def secret_values() -> list[str]:
    values = [settings.secret_key, settings.demo_password]
    for name in ("agent_api_key", "agent_callback_key", "diagnostics_key", "projects_database_url", "projects_write_database_url"):
        value = getattr(settings, name).get_secret_value()
        if value:
            values.append(value)
    return [v for v in values if v and len(v) >= 4]


def scrub(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 30:
        return "[DEPTH_LIMIT]"
    normalized = key.lower().replace("-", "_")
    if any(word in normalized for word in ("password", "secret", "cookie", "authorization", "api_key", "internal_key", "diagnostics_key", "lab_key", "dsn")) or normalized in {"token", "access_token", "refresh_token", "credential"}:
        return REDACTED
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k), depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, "", depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return json.dumps(scrub(parsed, depth=depth + 1), ensure_ascii=False)
    for secret in secret_values():
        text = text.replace(secret, REDACTED)
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r'(?i)(["\']?(?:password|api[_-]?key|access_token|secret)["\']?\s*[:=]\s*)[^\n,;]+', r"\1[REDACTED]", text)
    text = re.sub(r"\$argon2[^\s\"']+|SCRAM-SHA-256\$[^\s\"']+", REDACTED, text)
    return text


def _prune() -> None:
    now = time.monotonic()
    expired = [key for key, value in _runs.items() if now - value["clock"] > TTL]
    for key in expired:
        _runs.pop(key, None)
    for alias, key in list(_aliases.items()):
        if key not in _runs:
            _aliases.pop(alias, None)


def create_run(run_id: str, owner: str) -> bool:
    with _lock:
        _prune()
        if run_id in _runs:
            return False
        if len(_runs) >= 32:
            finished = next((k for k, v in _runs.items() if v["finished"]), None)
            if not finished:
                return False
            _runs.pop(finished)
        _runs[run_id] = {"run_id": run_id, "owner": owner, "clock": time.monotonic(), "events": [], "bytes": 0, "dropped": 0, "finished": False, "termination": None}
        return True


def emit(kind: str, channel: str, *, span_id: str | None = None, capture: Capture | None = None, **data) -> None:
    """Failure isolation applies ONLY to recording, not to the observed operation."""
    cap = capture or current_capture.get()
    if not cap or not cap.enabled:
        return
    try:
        safe = scrub(data)
        record = {"kind": kind, "channel": channel, "span_id": span_id or cap.span_id, **safe}
        size = len(json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"))
        with _lock:
            run = _runs.get(cap.run_id)
            if not run:
                return
            if size > MAX_EVENT_BYTES or run["bytes"] + size > MAX_RUN_BYTES or len(run["events"]) >= MAX_EVENTS or sum(v["bytes"] for v in _runs.values()) + size > MAX_TOTAL_BYTES:
                run["dropped"] += 1
                return
            record.update(seq=len(run["events"]) + 1, elapsed_ms=round((time.monotonic() - run["clock"]) * 1000, 2))
            run["events"].append(record)
            run["bytes"] += size
    except Exception:
        with _lock:
            if cap.run_id in _runs:
                _runs[cap.run_id]["dropped"] += 1


def warning(reason: str, capture: Capture | None = None) -> None:
    emit("capture.warning", "capture", capture=capture, reason=reason)
    cap = capture or current_capture.get()
    with _lock:
        if cap and cap.run_id in _runs:
            _runs[cap.run_id]["dropped"] += 1


def finish(cap: Capture, termination: str) -> None:
    with _lock:
        if cap.run_id in _runs:
            _runs[cap.run_id].update(finished=True, termination=termination)


def alias_trace(trace_id: str) -> None:
    cap = current_capture.get()
    if cap and cap.enabled and trace_id:
        with _lock:
            _aliases[trace_id] = cap.run_id


def callback_capture(trace_id: str) -> Capture | None:
    with _lock:
        _prune()
        run_id = _aliases.get(trace_id)
        if run_id and run_id in _runs:
            return Capture(run_id, uuid.uuid4().hex)
    return None


def snapshot(run_id: str, owner: str, after: int = 0) -> dict | None:
    with _lock:
        _prune()
        run = _runs.get(run_id)
        if not run or run["owner"] != owner:
            return None
        return {"run_id": run_id, "events": [dict(e) for e in run["events"] if e["seq"] > after], "cursor": len(run["events"]), "finished": run["finished"], "termination": run["termination"], "dropped": run["dropped"], "capture_complete": run["finished"] and run["dropped"] == 0, "scope": "Selected backend HTTP, Agent HTTP and application-consumed read-only DB results; Agent internals excluded."}


class ObservedCursor:
    def __init__(self, cursor, span_id, sensitive=False):
        self._cursor, self._span, self._sensitive = cursor, span_id, sensitive

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def _result(self, value, operation):
        emit("sql.result", "backend.database", span_id=self._span, operation=operation, result=REDACTED if self._sensitive else value)
        return value

    def fetchone(self):
        return self._result(self._cursor.fetchone(), "fetchone")

    def fetchall(self):
        return self._result(self._cursor.fetchall(), "fetchall")

    def fetchmany(self, *args, **kwargs):
        return self._result(self._cursor.fetchmany(*args, **kwargs), "fetchmany")

    def __iter__(self):
        for row in self._cursor:
            yield self._result(row, "iterate")


class ObservedConnection:
    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, query, params=None, **kwargs):
        span_id = uuid.uuid4().hex
        sensitive = bool(re.search(r"app_credentials|password|credential", str(query), re.I))
        emit("sql.execute", "backend.database", span_id=span_id, query=str(query), parameters=REDACTED if sensitive else params)
        start = time.monotonic()
        try:
            cursor = self._connection.execute(query, params, **kwargs)
        except Exception as error:
            emit("sql.error", "backend.database", span_id=span_id, error_type=type(error).__name__)
            raise
        emit("sql.executed", "backend.database", span_id=span_id, row_count=cursor.rowcount, duration_ms=round((time.monotonic() - start) * 1000, 2))
        return ObservedCursor(cursor, span_id, sensitive)


def observe_connection(connection):
    cap = current_capture.get()
    return ObservedConnection(connection) if cap and cap.enabled else connection
