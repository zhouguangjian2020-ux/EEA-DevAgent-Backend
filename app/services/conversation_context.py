"""Request-only context packing. Stored history and current edit bases stay intact.

No summaries, history window, fuzzy object matching, model calls or business
writes. References identify an exact immutable version; historical snapshots
never become current modification targets.
"""
from copy import deepcopy
import re

from app.core.agent_limits import (
    CONVERSATION_SOFT_REFERENCE_BYTES, MAX_REFERENCE_BYTES, reference_bytes,
)
from app.services.conversation_history import compact_failed_proposal_text
from app.services.workflow_sources import fail

RECENT_TURNS_PRESERVED = 6
CONTEXT_POLICY = (
    '本轮attachments是唯一可提出修改的对象及可信基础版本。'
    '历史messages的附件引用按artifact_id和revision_id在本轮attachments或historical_objects中定位正文。'
    'historical_objects仅为过去版本的只读资料，历史状态不代表当前状态，不得作为新增修改目标；'
    '不同版本不可互相替代。普通对话原文仍保留。标注未提供的旧无效响应正文不可猜测；'
    '需要该正文时请明确说明缺少资料，不能将其视为已完成或已采纳的修改。'
)


def needs_original_machine_outputs(message):
    """Conservative preservation hint, never intent classification or permission.

    The most recent six turns always keep original responses. Explicit analysis
    or references to older output additionally disable optional old-error packing.
    A missed reference still has a missing-content notice, never fabricated text.
    """
    return bool(re.search(
        r'json|报错|失败|异常|历史|原文|原始|之前|前面|那次|那段|上次|上轮|第.{0,8}轮'
        r'|分析|解释|检查|为什么|earlier|previous|history|error|fail|output|response|review|analy',
        message, flags=re.IGNORECASE,
    ))


def _snapshot_key(snapshot):
    if not isinstance(snapshot, dict):
        fail(409, 'conversation_snapshot_invalid', '历史对象快照不完整；未截断或猜测参考资料')
    artifact_id, revision_id = snapshot.get('artifact_id'), snapshot.get('revision_id')
    if not artifact_id or not revision_id or not isinstance(snapshot.get('content'), dict):
        fail(409, 'conversation_snapshot_invalid', '历史对象快照缺少版本或正文；请核对会话记录')
    return str(artifact_id), str(revision_id)


def _assert_same_content(first, other):
    # head/accepted status can legitimately change without changing this revision.
    if first['content'] != other['content']:
        fail(409, 'conversation_snapshot_conflict',
             '同一对象版本的正文快照不一致；未猜测采用哪份资料或发起Agent请求')


def _over_limit_message(context, usage):
    def kib(value):
        return f'{value / 1024:.1f}KiB'
    current_too_large = usage['current_object_bytes'] > usage['limit_bytes']
    opening = ('当前操作对象正文已超过Agent参考资料容量；'
               if current_too_large else '去重后的会话参考资料仍超过Agent 512KiB限制；')
    totals = (f"本轮共{kib(usage['reference_bytes'])}（上限512KiB），"
              f"当前对象{kib(usage['current_object_bytes'])}、"
              f"历史对象{kib(usage['historical_object_bytes'])}、"
              f"对话文字及引用{kib(usage['dialogue_bytes'])}、"
              f"任务回执{kib(usage['task_receipt_bytes'])}。")
    # Object indices match the current selection order; never echo names/body/IDs.
    largest = sorted(((index + 1, reference_bytes(item))
                      for index, item in enumerate(context['attachments'])),
                     key=lambda entry: entry[1], reverse=True)[:3]
    detail = ('当前对象中占用较大的项：' + '、'.join(
        f'第{index}项{kib(size)}' for index, size in largest) + '。') if largest else ''
    guidance = ('请减少当前操作对象或明确缩小处理范围；'
                if current_too_large else
                '请减少当前操作对象；若主要占用来自历史，可新建会话并带入必要结论，原历史仍保留；')
    return opening + totals + detail + guidance + '未截断正文、未新增本轮会话记录，也未发起Agent请求。'


def pack_conversation_context(context, *, preserve_machine_outputs=False):
    """Return packed references and content-free usage stats before starting a turn.

    Identical snapshot content is sent once per (artifact_id, revision_id).
    All distinct historical versions and ordinary messages remain available;
    reaching a budget does not cause silent deletion or truncation.
    """
    current = context.get('attachments', [])
    originals = {}
    for snapshot in current:
        key = _snapshot_key(snapshot)
        if key in originals:
            fail(409, 'conversation_snapshot_invalid', '当前操作对象存在重复版本；请重新核对选择')
        originals[key] = snapshot

    messages = context.get('messages', [])
    total_turns = sum(message.get('role') == 'user' for message in messages)
    turn_number = 0
    historical_objects, packed_messages = [], []
    duplicate_count, compacted_count = 0, 0
    for message in messages:
        if message.get('role') == 'user':
            turn_number += 1
        copied = {k: deepcopy(v) for k, v in message.items() if k != 'attachments'}
        if 'attachments' in message:
            references = []
            for snapshot in message['attachments']:
                key = _snapshot_key(snapshot)
                previous = originals.get(key)
                if previous is not None:
                    _assert_same_content(previous, snapshot)
                    duplicate_count += 1
                else:
                    originals[key] = snapshot
                    historical_objects.append(deepcopy(snapshot))
                # Preserve historical metadata such as status, but do not repeat
                # the version's content in every old user message.
                references.append({k: deepcopy(v) for k, v in snapshot.items() if k != 'content'})
            copied['attachments'] = references
        if message.get('role') == 'assistant' and isinstance(message.get('content'), str):
            allow = (not preserve_machine_outputs and turn_number > 0
                     and turn_number <= total_turns - RECENT_TURNS_PRESERVED)
            copied['content'], compacted = compact_failed_proposal_text(
                message['content'], allow_compaction=allow,
            )
            if compacted:
                copied['context_representation'] = 'invalid_proposal_receipt'
                compacted_count += 1
        packed_messages.append(copied)

    packed = {k: deepcopy(v) for k, v in context.items()
              if k not in {'messages', 'attachments', 'task_receipts'}}
    packed.update(messages=packed_messages, attachments=deepcopy(current),
                  task_receipts=deepcopy(context.get('task_receipts', [])),
                  historical_objects=historical_objects, context_policy=CONTEXT_POLICY)
    usage = {
        'reference_bytes': reference_bytes(packed),
        'limit_bytes': MAX_REFERENCE_BYTES,
        'soft_limit_bytes': CONVERSATION_SOFT_REFERENCE_BYTES,
        'current_object_bytes': reference_bytes(packed['attachments']),
        'historical_object_bytes': reference_bytes(historical_objects),
        'dialogue_bytes': reference_bytes(packed_messages),
        'task_receipt_bytes': reference_bytes(packed['task_receipts']),
        'current_object_count': len(current),
        'historical_object_count': len(historical_objects),
        'deduplicated_snapshot_count': duplicate_count,
        'compacted_response_count': compacted_count,
    }
    usage['framing_bytes'] = usage['reference_bytes'] - sum(
        usage[key] for key in ('current_object_bytes', 'historical_object_bytes',
                               'dialogue_bytes', 'task_receipt_bytes'))
    usage['near_limit'] = usage['reference_bytes'] > usage['soft_limit_bytes']
    if usage['reference_bytes'] > MAX_REFERENCE_BYTES:
        fail(413, 'agent_context_too_large', _over_limit_message(packed, usage))
    return packed, usage
