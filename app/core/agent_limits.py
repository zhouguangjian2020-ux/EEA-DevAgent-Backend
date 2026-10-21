"""Shared Agent reference-data and bounded generation-wait contracts."""
import json
import math

MAX_REFERENCE_BYTES = 512 * 1024
CONVERSATION_SOFT_REFERENCE_BYTES = 400 * 1024


def reference_bytes(value):
    """Match the Agent compact-JSON UTF-8 count, not characters or tokens."""
    return len(json.dumps(value, ensure_ascii=False, default=str,
                          separators=(',', ':')).encode('utf-8'))


MAX_GENERATION_TIMEOUT_SECONDS = 2000
FR_GENERATION_SCENES = frozenset({"FROM_UC_CREATE_FR", "FROM_FR_CREATE_SS_ASSIGNMENT"})
GENERATION_SCENES = FR_GENERATION_SCENES | frozenset({
    "FEATURE_DEFINITION", "FROM_DEFINITION_CREATE_PRD", "FROM_PRD_CREATE_UC",
    "FROM_UC_CREATE_UC_DIAGRAM", "FROM_SS_ASSIGNMENT_CREATE_REQ_DIAGRAM",
})


def generation_timeout_seconds(*, fr=False):
    """A generation wait, independent of the legacy prompt-expansion budget."""
    from app.core.config import settings
    return settings.agent_fr_timeout_seconds if fr else settings.agent_generation_timeout_seconds


def generation_timeout_budget(*, fr=False):
    """Agent accepts integer seconds; do not truncate a fractional local budget."""
    return min(MAX_GENERATION_TIMEOUT_SECONDS, max(1, math.ceil(generation_timeout_seconds(fr=fr))))
