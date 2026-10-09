"""Optional durable per-attempt timing; no timers in business metadata or Agent context.

Old schemas/rows deliberately return no duration. Reads never start an attempt.
The database increment is independently reviewed and is not applied by this code.
"""
from datetime import datetime
from uuid import uuid4

from app.core.request_timing import received_at, utcnow

COLUMNS = ("attempt_id", "attempt_started_at", "attempt_finished_at", "duration_ms")
MAX_DURATION_MS = 9_007_199_254_740_991
FINISHED = frozenset({"completed", "succeeded", "preview", "empty", "failed"})


def schema_available(db, table: str) -> bool:
    if table not in {"ee.chat_tasks", "ee.chat_turns"}:
        raise ValueError("Unsupported timing table")
    row = db.execute("""SELECT count(*) = 4 AS timing_ready FROM pg_catalog.pg_attribute
        WHERE attrelid=to_regclass(%s) AND attnum>0 AND NOT attisdropped
          AND (attname,atttypid) IN
            (('attempt_id','uuid'::regtype),('attempt_started_at','timestamptz'::regtype),
             ('attempt_finished_at','timestamptz'::regtype),('duration_ms','int8'::regtype))""",
        (table,)).fetchone()
    return bool(row and row.get("timing_ready") is True)


def has_columns(row: dict) -> bool:
    return all(key in row for key in COLUMNS)


def new_attempt() -> dict:
    return dict(zip(COLUMNS, (uuid4(), received_at(), None, None)))


def duration_value(value, status: str):
    return value if status in FINISHED and type(value) is int and 0 <= value <= MAX_DURATION_MS else None


def completion(row: dict, observed_at: datetime | None, expected_attempt_id) -> dict:
    """Finish once, only for this attempt and a directly observed end (not replay/poll)."""
    if (not has_columns(row) or observed_at is None or expected_attempt_id is None
            or row["attempt_id"] != expected_attempt_id or row["attempt_finished_at"] is not None):
        return {}
    start = row["attempt_started_at"]
    if (not isinstance(start, datetime) or not isinstance(observed_at, datetime)
            or start.utcoffset() is None or observed_at.utcoffset() is None):
        return {}
    delta = observed_at - start
    milliseconds = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    # A backwards clock does not prove a zero-second task. Keep timing unknown.
    if not 0 <= milliseconds <= MAX_DURATION_MS:
        return {}
    return {"attempt_finished_at": observed_at, "duration_ms": milliseconds}


def insert_parts(values: dict):
    _check_columns(values)
    return ("".join("," + key for key in values), ",%s" * len(values), tuple(values.values()))


def update_part(values: dict):
    _check_columns(values)
    return "".join("," + key + "=%s" for key in values), tuple(values.values())


def _check_columns(values):
    if any(key not in COLUMNS for key in values):
        raise ValueError("Only dedicated timing fields may be changed")
