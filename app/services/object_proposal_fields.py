"""Expand explicit UC/FR field replacements against a trusted turn snapshot.

This helper never infers a target, parses prose, repairs JSON, or writes an
object. The resulting full content must still pass the ordinary modification
validator and the current-version/permission check before a preview is offered.
"""
from copy import deepcopy

from app.services.workflow_sources import fail


_UC_FIELDS = frozenset({
    'name', 'description', 'actors', 'preconditions', 'main_flow', 'branches',
    'exceptions', 'postconditions', 'extensions', 'hmi', 'regulations',
    'performance', 'nonfunctional',
})
_FR_FIELDS = frozenset({'description', 'acceptance_criteria'})
_UC_LIST_FIELDS = frozenset({'branches', 'exceptions'})


def _invalid(message):
    fail(502, 'agent_invalid_field_proposal', message)


def expand_object_fields(change, available):
    """Materialize explicit field replacements without changing either input.

    Omitted fields and upstream references come only from this turn's trusted
    base snapshot. Lists replace the whole field; there is no element patch,
    implicit append, null deletion, or guessed object/version association.
    Existing full-content and PRD-edits proposals pass through unchanged.
    """
    if not isinstance(change, dict) or 'fields' not in change:
        return change
    if set(change) != {'artifact_id', 'base_revision_id', 'fields'}:
        _invalid('字段修改必须只包含对象、基础版本和fields；不能混用content、edits或其他字段')
    original = available.get(str(change['artifact_id']))
    if (not original
            or str(change['base_revision_id']) != str(original['revision_id'])):
        _invalid('字段修改的对象或基础版本与本轮附件不一致；未生成预览')
    content = original.get('content')
    kind = content.get('kind') if isinstance(content, dict) else None
    if kind not in {'UC', 'FR'}:
        _invalid('字段替换仅支持本轮关联的UC或FR；PRD请使用完整正文或精确片段修改')
    fields = change['fields']
    if not isinstance(fields, dict) or not fields:
        _invalid('字段修改必须提供非空fields对象；未生成预览')
    allowed = _UC_FIELDS if kind == 'UC' else _FR_FIELDS
    if not set(fields).issubset(allowed):
        _invalid('字段修改包含不支持的字段；不得修改对象类型、上游来源或版本信息')
    for name, value in fields.items():
        if kind == 'UC' and name in _UC_LIST_FIELDS:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                _invalid('UC分支和异常流程必须提供完整文本数组；不能使用null或按索引修改')
        elif not isinstance(value, str):
            _invalid('字段修改的文本值必须是字符串；清空文本请明确使用空字符串，不得使用null')
    expanded = deepcopy(content)
    expanded.update(deepcopy(fields))
    return {
        'artifact_id': change['artifact_id'],
        'base_revision_id': change['base_revision_id'],
        'content': expanded,
    }
