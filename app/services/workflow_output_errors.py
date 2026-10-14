"""Allow-listed upstream validation diagnostics; never echo model output or secrets."""
import json
import re

from app.core.workflow_limits import MAX_FR_BATCH_ITEMS
from app.services.projects import ProjectReadError

_GENERIC = "Agent技能输出未通过校验，未保存生成结果"
_FIELD = re.compile(
    r"(?:\$\.)?(functional_requirements(?:\[[0-9]{1,3}\])?"
    r"(?:\.(?:kind|description|acceptance_criteria|sources|uc_id|uc_revision_id)"
    r"(?:\[[0-9]{1,3}\])?)*)"
)


def invalid_output_message(value: object) -> str:
    """Return controlled explanations only, not the upstream error's raw text."""
    if not isinstance(value, str) or len(value) > 16_384:
        return _GENERIC
    # JSON parse failures can include a raw output excerpt. Never inspect that excerpt.
    text = value.split("；原始文本", 1)[0]
    if "输出达到 token 上限" in text and "被截断" in text:
        return "Agent生成内容达到输出长度上限，JSON被截断；未保存任何生成结果"
    for limit in (100, MAX_FR_BATCH_ITEMS):
        if f"functional_requirements 超过 {limit} 条上限" in text:
            return f"Agent生成的FR超过单批{limit}条上限；未保存任何生成结果，也未截断内容"
    if "不在输入 reference_data.sources 中" in text:
        return "Agent生成的FR引用了不属于本次输入的UC或修订版本；未保存任何生成结果"
    if "无法解析为 JSON" in text:
        return "Agent生成内容不是完整有效的JSON；未保存任何生成结果"
    if "functional_requirements 不能为空" in text:
        return "Agent未生成任何FR；未保存生成结果"
    field = _FIELD.search(text)
    if field:
        if "类型应为" in text or "必须是字符串" in text or "必须是数组" in text or "必须是对象" in text:
            reason = "字段类型不符合契约"
        elif "缺少必填字段" in text or "不能为空" in text or "至少需要" in text:
            reason = "缺少必填内容或来源"
        elif "重复" in text:
            reason = "同一FR重复引用UC"
        elif "超过" in text:
            reason = "字段长度或条目数量超限"
        elif "必须为" in text:
            reason = "字段值不符合契约"
        else:
            return _GENERIC
        return f"Agent生成的FR{reason}（{field.group(1)}）；未保存任何生成结果"
    return _GENERIC


# Only these machine-classified output failures may trigger allocation recovery.
# Unknown/schema/identity failures remain terminal even when their UI code matches.
class AgentOutputError(ProjectReadError):
    def __init__(self, code: str, message: str, diagnostics: dict):
        super().__init__(502, code, message)
        self.diagnostics = dict(diagnostics)

    @property
    def recoverable_output(self) -> bool:
        return self.diagnostics.get("category") in {"json_syntax", "output_limit"}


def safe_trace_id(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) else "unavailable"


_JSON_REASONS = {
    "Expecting value": "expecting_value",
    "Expecting property name enclosed in double quotes": "property_name",
    "Expecting ':' delimiter": "missing_colon",
    "Expecting ',' delimiter": "missing_comma",
    "Unterminated string starting at": "unterminated_string",
    "Invalid control character at": "control_character",
    "Extra data": "extra_data",
}


def _json_reason(text: str) -> str:
    return next((code for prefix, code in _JSON_REASONS.items() if text.startswith(prefix)), "other_json_syntax")


def upstream_output_diagnostics(value: object, trace_id: object) -> dict:
    """Extract only fixed categories and numeric offsets, never model excerpts."""
    info = {"origin": "agent_validation", "category": "validation", "trace_id": safe_trace_id(trace_id)}
    if not isinstance(value, str) or len(value) > 16_384:
        return info
    text = value.split("；原始文本", 1)[0]
    text = re.sub(r"^技能 [A-Za-z0-9_.-]{1,80} 输出无效：", "", text, count=1)
    prefix = "结构化技能输出无法解析为 JSON："
    if text.startswith(prefix):
        detail = text[len(prefix):]
        info.update(category="json_syntax", parse_reason=_json_reason(detail))
        position = re.search(r": line ([0-9]{1,9}) column ([0-9]{1,9}) \(char ([0-9]{1,9})\)$", detail)
        if position:
            info.update(zip(("line", "column", "offset"), map(int, position.groups())))
    elif re.match(r"^模型输出达到 token 上限（max_tokens=[0-9?]{1,9}）被截断，JSON 不完整。", text):
        info.update(category="output_limit", finish_reason="length")
        length = re.search(r"已生成 ([0-9]{1,9}) 字符", text)
        if length:
            info["output_chars"] = int(length.group(1))
    return info


def local_json_diagnostics(error: json.JSONDecodeError, output_chars: int, trace_id: object) -> dict:
    return {"origin": "output_json", "category": "json_syntax", "trace_id": safe_trace_id(trace_id),
            "parse_reason": _json_reason(error.msg), "line": error.lineno, "column": error.colno,
            "offset": error.pos, "output_chars": output_chars}
