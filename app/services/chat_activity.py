"""Bounded public Agent progress, separate from answer text and model reasoning.

Only the Agent's explicit status/stage contract is exposed. Raw tool payloads,
internal reasoning and diagnostic fields are deliberately not copied through.
"""
import json
import re

MAX_ACTIVITY_EVENTS = 80
MAX_ACTIVITY_BYTES = 64 * 1024
MAX_STATUS_CHARACTERS = 240
MAX_STAGE_CHARACTERS = 80
MAX_TITLE_CHARACTERS = 200
MAX_BODY_CHARACTERS = 4000

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-\n]*PRIVATE KEY-----|$)")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"'<>]+")
# Match quoted JSON and ordinary key=value/header forms without exposing the value.
_SECRET_VALUE = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|x-api-key|access[_-]?token|refresh[_-]?token|"
    r"authorization|password|passwd|secret|client[_-]?secret)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;<>}]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@")
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")


def _public_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Bound work even for an oversized optional progress payload. Redact before
    # the final trim so truncation cannot reveal part of a recognised secret.
    text = value[:MAX_BODY_CHARACTERS * 4].replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _PRIVATE_KEY.sub("[已隐藏私钥]", text)
    text = _BEARER.sub("Bearer [已隐藏]", text)
    text = _SECRET_VALUE.sub(lambda match: match.group(1) + "[已隐藏]", text)
    text = _URL_CREDENTIALS.sub(r"\1[已隐藏]@", text)
    text = _API_KEY.sub("[已隐藏]", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def normalize_activity_event(event: dict) -> dict | None:
    """Ignore malformed optional progress rather than failing a valid answer."""
    if event.get("type") == "status":
        # Agent uses stage; the business SSE protocol has always used message.
        message = _public_text(event.get("message") or event.get("stage"), MAX_STATUS_CHARACTERS)
        return {"type": "status", "message": message} if message else None
    if event.get("type") == "stage":
        stage = _public_text(event.get("stage"), MAX_STAGE_CHARACTERS)
        title = _public_text(event.get("title"), MAX_TITLE_CHARACTERS) or stage
        body = _public_text(event.get("body"), MAX_BODY_CHARACTERS)
        if not title:
            return None
        return {"type": "stage", "stage": stage, "title": title, "body": body}
    return None


class ActivityStream:
    """Per-stream budget; progress must not crowd out the final answer."""

    def __init__(self):
        self._count = 0
        self._size = 0
        self._previous = None

    def accept(self, event: dict) -> dict | None:
        if self._count >= MAX_ACTIVITY_EVENTS:
            return None
        normalized = normalize_activity_event(event)
        if normalized is None or normalized == self._previous:
            return None
        size = len(json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
        if self._size + size > MAX_ACTIVITY_BYTES:
            return None
        self._count += 1
        self._size += size
        self._previous = normalized
        return normalized
