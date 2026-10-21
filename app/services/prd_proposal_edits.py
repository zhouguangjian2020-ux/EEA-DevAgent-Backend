"""Expand explicit PRD text edits against one trusted, immutable base snapshot.

This is not a JSON repairer, fuzzy matcher or business write. A complete compact
proposal can be converted to the existing full-content preview contract only
when each original fragment identifies exactly one non-overlapping source span.
"""
from pydantic import ValidationError

from app.schemas.manual_work import PrdContent
from app.services.workflow_sources import fail


def _invalid(message):
    fail(502, 'agent_prd_edits_invalid', message)


def expand_prd_edits(change, available):
    """Return a full-content change, or pass legacy content changes unchanged.

    ``available`` contains this request's trusted attachment snapshots, keyed by
    artifact ID. No source lookup, persistence, guessing or retry occurs here.
    Existing downstream target/version/content validation remains mandatory.
    """
    if not isinstance(change, dict) or 'edits' not in change:
        return change
    if set(change) != {'artifact_id', 'base_revision_id', 'edits'}:
        _invalid('PRD片段修改必须只包含对象、基础版本和edits；不能同时返回content')
    original = available.get(str(change['artifact_id']))
    if (not original
            or str(change['base_revision_id']) != str(original['revision_id'])):
        _invalid('PRD片段修改的对象或基础版本与本轮附件不一致；未生成预览')
    content = original.get('content')
    if (not isinstance(content, dict) or content.get('kind') != 'PRD'
            or not isinstance(content.get('markdown_body'), str)):
        _invalid('片段替换仅支持本轮关联的PRD；UC和FR仍须返回完整对象')
    edits = change['edits']
    if not isinstance(edits, list) or not 1 <= len(edits) <= 20:
        _invalid('每个PRD必须提供1至20个完整片段修改')

    source = content['markdown_body']
    spans = []
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != {'old_text', 'new_text'}
                or not isinstance(edit['old_text'], str)
                or not isinstance(edit['new_text'], str)
                or not edit['old_text']):
            _invalid('PRD片段修改必须包含非空old_text和文本new_text；未生成预览')
        old, new = edit['old_text'], edit['new_text']
        start = source.find(old)
        if start < 0:
            _invalid('PRD片段原文未精确匹配本轮基础版本；未猜测位置或生成预览')
        # Searching from start + 1 also detects overlapping repeated matches.
        # str.count() would incorrectly accept e.g. "aa" in "aaa".
        if source.find(old, start + 1) >= 0:
            _invalid('PRD片段原文出现多次，无法唯一定位；需要包含更多原文上下文')
        spans.append((start, start + len(old), new))

    spans.sort(key=lambda span: span[0])
    previous_end = 0
    for start, end, _ in spans:
        if start < previous_end:
            _invalid('PRD片段修改在基础版本中相互重叠；未合并或生成预览')
        previous_end = end
    # Bound memory before joining all replacement text. The final typed check
    # remains authoritative for body length, forbidden NULs and field integrity.
    final_length = len(source) + sum(len(new) - (end - start) for start, end, new in spans)
    maximum = next(value.max_length for value in PrdContent.model_fields['markdown_body'].metadata
                   if getattr(value, 'max_length', None) is not None)
    if final_length > maximum:
        _invalid('PRD片段修改合并后的完整正文超过长度限制；未生成预览')
    parts, cursor = [], 0
    for start, end, new in spans:
        parts.extend((source[cursor:start], new))
        cursor = end
    parts.append(source[cursor:])
    expanded = {**content, 'markdown_body': ''.join(parts)}
    try:
        PrdContent.model_validate(expanded)
    except (ValidationError, ValueError, TypeError):
        _invalid('PRD片段修改合并后的完整对象未通过字段校验；未生成预览')
    return {
        'artifact_id': change['artifact_id'],
        'base_revision_id': change['base_revision_id'],
        'content': expanded,
    }
