"""Conservative compaction of generated history for an outbound Agent context.

The durable conversation body must remain untouched.  This helper does not
summarize ordinary discussion, repair JSON, infer adoption, or produce writable
object content.  It recognizes only existing local response wrappers/notices.
"""
from __future__ import annotations

import json
import re

_INCOMPLETE_NOTICE = "Agent调用已结束，但修改建议未完成：返回JSON的正文字符串或结构未闭合。"
_INVALID_NOTICE = "Agent调用已结束，但返回的对象修改JSON存在语法错误，未形成有效提案。"
_UNSUPPORTED_NOTICE = "本次对象相关JSON仅作为文字保留：未识别到完整修改提案，未开放AI修改预览，也未修改任何对象。"
_FAILURE_DETAIL = "未开放AI修改预览，也未修改任何对象；不会自动补齐或重试。"
_PRD_EDIT_HINT = "长PRD的局部修改可使用精确片段替换，避免重复输出整篇正文。"
_UNSUPPORTED_SUFFIXES = (
    "预览需要完整changes列表，每项包含当前对象的artifact_id、base_revision_id和完整content；"
    "生成技能原始结果、示例或不完整JSON不会自动转为修改建议。",
    "预览需要完整changes列表，每项包含当前对象的artifact_id、base_revision_id及完整content；"
    "生成技能原始结果、示例或不完整JSON不会自动转为修改建议。",
    "预览需要完整changes列表，每项包含当前对象的artifact_id、base_revision_id及完整content，或PRD专用且可核验的edits；"
    "生成技能原始结果、示例或不完整JSON不会自动转为修改建议。",
    "预览需要完整changes列表，每项包含当前对象的artifact_id、base_revision_id及完整content、PRD专用且可核验的edits或UC/FR受限字段替换fields之一；"
    "生成技能原始结果、示例或不完整JSON不会自动转为修改建议。",
)
_LOCAL_FAILURE_NOTICES = {
    _INCOMPLETE_NOTICE + _FAILURE_DETAIL + _PRD_EDIT_HINT,
    _INVALID_NOTICE + _FAILURE_DETAIL + _PRD_EDIT_HINT,
    _INVALID_NOTICE + _FAILURE_DETAIL,
    *(_UNSUPPORTED_NOTICE + suffix for suffix in _UNSUPPORTED_SUFFIXES),
}
_OBJECT_MARKER = re.compile(
    r'"(?:changes|base_revision_id|markdown_body|use_cases|edits)"\s*:'
    r'|"kind"\s*:\s*"(?:modification|PRD|UC|FR)"'
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Non-finite JSON value")


def _parse(text):
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _document_text(text):
    source = text.strip()
    if source.startswith("```json\n") and source.endswith("\n```"):
        return source[len("```json\n"):-len("\n```")].strip()
    if source.startswith("```\n") and source.endswith("\n```"):
        return source[len("```\n"):-len("\n```")].strip()
    return source


def _smaller(body, receipt, reason):
    if len(receipt.encode("utf-8")) >= len(body.encode("utf-8")):
        return body, None
    return receipt, reason


def compact_history_body(body: str) -> tuple[str, str | None]:
    """Return (outbound_text, reason), leaving unrelated text exactly unchanged.

    Callers must preserve the original in storage and may bypass this helper
    when the current request explicitly requires the historical raw output.
    A reason only signals an outbound-context transformation, never an approval
    or a durable state transition.
    """
    # Successful proposal wrappers and ordinary conversation are never packed.
    # The orchestration appends one local failure notice after a blank line.
    # Do not search/extract a JSON fragment from normal prose or code examples.
    raw, separator, notice = body.rpartition("\n\n")
    if not separator or "\n" in notice.strip():
        return body, None
    notice = notice.strip()
    if notice not in _LOCAL_FAILURE_NOTICES:
        return body, None
    source = _document_text(raw)
    if not source.startswith(("{", "[")) or not _OBJECT_MARKER.search(source):
        return body, None

    try:
        document = _parse(source)
    except json.JSONDecodeError as error:
        incomplete = error.msg.startswith("Unterminated string") or error.pos >= len(source) - 1
        reason = "incomplete_proposal_json" if incomplete else "invalid_proposal_json"
        receipt = (
            "该轮返回的对象修改JSON不完整，未形成有效修改提案，未开放预览。"
            if incomplete else
            "该轮返回的对象修改JSON有语法错误，未形成有效修改提案，未开放预览。"
        )
    except (ValueError, RecursionError):
        # Duplicate keys / excessive nesting are not evidence of truncation.
        return body, None
    else:
        # A complete unsupported generation result is distinct from an actual
        # proposal.  Never discard arbitrary valid JSON just for having braces.
        if not notice.startswith(_UNSUPPORTED_NOTICE):
            return body, None
        if isinstance(document, dict) and (
            set(document) == {"changes"} or
            (set(document) == {"kind", "changes"} and document.get("kind") == "modification")
        ):
            return body, None  # Never discard a potentially valid explicit proposal.
        is_object = isinstance(document, dict) and (
            (isinstance(document.get("kind"), str) and document["kind"] in {"modification", "PRD", "UC", "FR"})
            or isinstance(document.get("changes"), list)
            or isinstance(document.get("use_cases"), list)
        )
        if not is_object:
            return body, None
        reason = "unsupported_proposal_json"
        receipt = "该轮对象相关JSON未满足修改提案格式，未开放修改预览。"
    receipt += (
        "该回复未写入任何对象；本轮上下文未重复携带生成正文，原始回复仍保留。"
        "需要分析原始JSON时请读取对应历史记录，不可推断或补齐缺失内容。"
    )
    return _smaller(body, receipt, reason)



def compact_failed_proposal_text(body: str, *, allow_compaction: bool = False) -> tuple[str, bool]:
    """Compatibility entry point for outbound context packing.

    Recent/explicitly referenced output is kept verbatim.  Valid historic
    proposals are also kept because their details can be part of follow-up
    discussion; the compacted receipt denotes only locally rejected output.
    """
    if not allow_compaction:
        return body, False
    text, reason = compact_history_body(body)
    if reason is None:
        return body, False
    return text, True
