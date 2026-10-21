"""Safe explanations for failed Agent conversation events; never echo raw errors."""
import json
import logging
from typing import NoReturn

from app.services.projects import ProjectReadError
from app.services.workflow_output_errors import safe_trace_id

logger = logging.getLogger(__name__)

# Agent's LLM_UPSTREAM_ERROR also wraps graph/node exceptions. Do not claim it
# proves a model provider failure, and never forward its raw str(exception).
_ERRORS = {
    'INVOKE_TIMEOUT': (504, 'agent_timeout', 'Agent等待执行事件超时'),
    'TASK_TIMEOUT': (504, 'agent_timeout', 'Agent执行达到任务时限'),
    'LLM_UPSTREAM_ERROR': (502, 'agent_llm_upstream_error', 'Agent模型调用或执行节点失败，请核对Agent运行记录'),
    'SKILL_INPUT_MISSING': (502, 'agent_skill_input_missing', 'Agent技能缺少必填输入，未完成本次修改建议'),
    'SKILL_OUTPUT_INVALID': (502, 'agent_skill_output_invalid', 'Agent技能结果未通过完整性或格式校验'),
    'OUTPUT_FORMAT_CONFLICT': (502, 'agent_output_format_conflict', 'Agent场景输出格式与本次对话契约冲突'),
    'REFERENCE_DATA_TOO_LARGE': (502, 'agent_context_too_large', 'Agent参考资料超过限制，请减少当前操作对象或缩小范围'),
    'RATE_LIMITED': (503, 'agent_busy', 'Agent当前繁忙或触发调用限流'),
    'UNAUTHORIZED': (502, 'agent_auth_failed', 'Agent服务鉴权失败，请核对服务端配置'),
    'FORBIDDEN': (502, 'agent_auth_failed', 'Agent拒绝服务端访问，请核对服务端权限'),
    'SCENE_NOT_FOUND': (502, 'agent_scene_not_found', 'Agent未注册所需场景，请核对服务版本'),
    'SKILL_NOT_FOUND': (502, 'agent_skill_not_found', 'Agent未注册所需技能，请核对服务版本'),
}
_DEFAULT = (502, 'agent_stream_error', 'Agent流式执行失败，上游未提供可识别的错误类型')
_SUFFIX = '；保留已收到内容，未开放修改预览，未自动重试'


def reject_conversation_stream_error(event: dict, request_id: object = None) -> NoReturn:
    """Classify by allow-listed code and log only bounded correlation metadata.

    Partial tokens remain display-only. This function always raises, including
    for unknown/malformed codes; it never converts an upstream failure to done.
    """
    # Some compatible endpoints terminate with done.error rather than type=error.
    # Never inspect or forward its raw message; nested codes follow the same whitelist.
    nested = event.get('error')
    details = nested if isinstance(nested, dict) else event
    supplied_code = details.get('code')
    code = supplied_code if isinstance(supplied_code, str) and supplied_code in _ERRORS else 'UNKNOWN'
    status, public_code, message = _ERRORS.get(code, _DEFAULT)
    logger.warning('conversation_agent_stream_error %s', json.dumps({
        'upstream_code': code,
        'code': public_code,
        'request_id': safe_trace_id(request_id),
        'trace_id': safe_trace_id(details.get('trace_id') or event.get('trace_id')),
    }, sort_keys=True))
    raise ProjectReadError(status, public_code, message + _SUFFIX)
