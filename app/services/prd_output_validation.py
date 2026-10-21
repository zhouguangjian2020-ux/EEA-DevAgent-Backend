"""Conservative document admission for automatic full-PRD creation only.

This does not grade engineering correctness, rewrite Markdown, or apply to manual
PRD drafts, conversations, edits, or the legacy generation-preview endpoint.
The eight logical chapters follow the existing automotive PRD template. Heading
syntax and approved aliases are normalized for inspection only; output is intact.
"""
from dataclasses import dataclass
import re
import unicodedata

from app.services.prd_generation import MAX_PRD_OUTPUT_CHARACTERS, PrdGenerationError

CHAPTERS = (
    "文档说明", "需求背景", "需求描述", "功能详细说明",
    "验收指标", "埋点要求", "项目规划", "附录",
)
_ALIASES = (
    ("文档说明", "文档概述", "文档信息", "文档介绍", "document overview", "document information", "document description"),
    ("需求背景", "项目背景", "业务背景", "产品背景", "背景与目标", "背景与目的", "requirements background", "background and objectives"),
    ("需求描述", "需求概述", "需求定义", "功能需求", "业务需求", "requirements description", "requirements", "functional requirements"),
    ("功能详细说明", "功能详细描述", "详细功能说明", "功能详述", "功能详情", "详细功能设计", "功能设计", "功能详细设计", "functional details", "detailed functional specification", "detailed functional description"),
    ("验收指标", "验收标准", "验收要求", "验收条件", "测试与验收", "验收测试标准", "acceptance criteria", "acceptance requirements"),
    ("埋点要求", "埋点需求", "数据埋点", "埋点设计", "数据采集要求", "analytics requirements", "telemetry requirements", "tracking requirements"),
    ("项目规划", "项目计划", "实施规划", "实施计划", "开发计划", "项目排期", "project plan", "project planning", "implementation plan"),
    ("附录", "附录与参考资料", "附录及参考资料", "appendix", "appendices"),
)


def _plain(value):
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"!?\[([^]\n]*)\]\([^\n)]*\)", r"\1", value)
    return re.sub(r"[*_`]", "", value).strip()


def _key(value):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", _plain(value)).casefold()


_ALIAS_MAP = {_key(alias): index for index, aliases in enumerate(_ALIASES) for alias in aliases}
_NUMBER = re.compile(r"^(?:第\s*[0-9一二三四五六七八九十]+\s*[章节篇]|chapter\s+\d+|[0-9]+(?:\.[0-9]+)*[.、:：)）]?|[一二三四五六七八九十]+[、.：:])\s*", re.I)
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_PLACEHOLDER = re.compile(r"^(?:待(?:填写|补充|确认|定|完善|调研)|暂无|无|不适用|n\s*/?\s*a|tbd|todo|tbc|placeholder|[.。…—_-]+)$", re.I)
_LABELS = {
    "功能名称", "功能描述", "前置条件", "触发条件", "基本事件", "异常事件", "用户交互示意",
    "需求描述", "功能定义", "应用场景", "功能列表", "用户体验要求", "验收项", "验收标准", "测试方法",
    "指标名称", "指标值", "序号", "优先级", "备注", "说明", "内容", "条件", "结果", "场景",
    "l1功能", "l2功能", "l3功能", "需求编号", "requirement", "description", "acceptance criteria",
}
_LABEL_KEYS = {_key(value) for value in _LABELS}
_FAILURE = re.compile(
    r"(?:本次|这次|此次|我|抱歉|很抱歉|对不起).{0,45}(?:未能|没能|无法|不能|未成功|失败).{0,25}(?:生成|完成|撰写|编写|PRD|文档)"
    r"|(?:未能|没能|无法|不能).{0,12}(?:生成|撰写|编写).{0,30}(?:PRD|文档)"
    r"|(?:本次|这次|此次).{0,30}(?:生成|撰写|编写).{0,12}失败"
    r"|(?:sorry|i am unable|i cannot|i couldn't|failed to generate|unable to generate).{0,80}(?:prd|document)", re.I,
)
_NEEDS_INPUT = re.compile(
    r"(?:请|需要您|需要你).{0,25}(?:补充|提供|确认|说明|重新发送|再发).{0,60}(?:需求|范围|功能|信息|资料|文档|交互)"
    r"|(?:please|could you).{0,30}(?:provide|clarify|specify|resend)", re.I,
)
_TRUNCATION = re.compile(
    r"^(?:输出长度达到模型上限\s*[（(]max_?tokens\s*=\s*[\d?]+[)）][,，]?\s*"
    r"文档在此处被截断[,，]后续内容未生成[。.]"
    r"|(?:本次输出|本次生成的PRD|本文档|生成结果)(?:已被截断|在此处被截断)[。.!！]?\s*$"
    r"|(?:this output|this document|this response) (?:was|has been) truncated[.!]?\s*$)", re.I,
)


def _is_truncation_notice(line):
    # Match the Agent's explicit completion warning, not business rules about
    # truncation, quoted UI messages, table cells or conditional requirements.
    text = _plain(line).lstrip("> ⚠️!")
    return bool(_TRUNCATION.match(text))


@dataclass(frozen=True)
class _Heading:
    start: int
    end: int
    level: int
    title: str
    chapter: int | None


def _chapter(title):
    value = _plain(title)
    # A subsection named e.g. 3.2 功能定义 is not a separate top-level chapter.
    if re.match(r"^\d+\.\d+(?:\D|$)", value):
        return None
    value = _NUMBER.sub("", value)
    value = re.sub(r"[（(][^()（）]*[)）]", "", value)
    return _ALIAS_MAP.get(_key(value))


def _document_lines(markdown):
    # Exclude examples and comments from structural evidence, never from storage.
    text = re.sub(r"<!--.*?(?:-->|$)", "", markdown, flags=re.S)
    lines = text.splitlines()
    visible = list(lines)
    fence = None
    for index, line in enumerate(lines):
        match = _FENCE.match(line)
        if fence:
            visible[index] = ""
            if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*$", line):
                fence = None
            continue
        if match:
            fence = (match[1][0], len(match[1]))
            visible[index] = ""
    return visible, fence is not None


def _headings(lines):
    result = []
    index = 0
    in_toc, toc_chapters = False, set()
    while index < len(lines):
        line = lines[index]
        match = _ATX.match(line)
        candidate = match[2] if match else line.strip()
        if not any(h.chapter is not None for h in result) and _key(candidate) in {"目录", "tableofcontents", "contents"}:
            in_toc, toc_chapters = True, set()
            index += 1
            continue
        if in_toc:
            entry = re.sub(r"^[-+*]\s+", "", _plain(candidate))
            chapter = _chapter(entry)
            if chapter is None:
                # Word exports may repeat the number: '1. 1 文档说明'.
                chapter = _chapter(_NUMBER.sub("", entry))
            if chapter is not None and chapter not in toc_chapters:
                toc_chapters.add(chapter)
                index += 1
                continue
            if chapter is None and not match:
                index += 1
                continue
            in_toc = False  # Repeated chapter or next Markdown heading: body.
        if re.fullmatch(r"\s*(?:[-+*]|\d+[.)])?\s*\[[^]]*\]\([^)]*\)\s*", line):
            index += 1
            continue
        title, level, end = None, 0, index + 1
        if match:
            title, level = match[2], len(match[1])
        elif line.strip() and not line.lstrip().startswith((">", "|", "- ", "* ")):
            if index + 1 < len(lines) and re.fullmatch(r" {0,3}(?:={3,}|-{3,})\s*", lines[index + 1]):
                title, level, end = line.strip(), 1 if "=" in lines[index + 1] else 2, index + 2
            elif (_NUMBER.match(_plain(line)) or re.fullmatch(r"\s*(?:\*\*|__)(.+)(?:\*\*|__)\s*", line)) and _chapter(line) is not None:
                # Numbered or bold headings are common historical PRD exports.
                title, level = line.strip(), 2
        if title is not None:
            result.append(_Heading(index, end, level, title, _chapter(title)))
            index = end
        else:
            index += 1
    return result


def _content_cell(value):
    text = _plain(value)
    text = re.sub(r"^\s*[-+*]\s+", "", text)
    text = _NUMBER.sub("", text)
    text = re.sub(r"^\[[ xX]\]\s*", "", text)
    text = re.sub(r"\{[^{}]*\}", "", text).strip(" :：;；-—")
    if not text or _PLACEHOLDER.fullmatch(text) or _key(text) in _LABEL_KEYS:
        return False
    # Label: placeholder and prose consisting only of a missing-information note
    # are not a substantive requirement, event or acceptance criterion.
    if re.fullmatch(r"[^:：]{1,30}[:：]\s*(?:待填写|待补充|待确认|待定|N/A|TBD|TODO)?", text, re.I):
        return False
    return bool(re.search(r"[\w\u4e00-\u9fff]", text))


def _table_separator(line):
    cells = re.sub(r"^(?:>\s*)+", "", line.strip()).strip("|").split("|")
    return len(cells) > 1 and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)


def _has_body(lines, start, end, heading_lines):
    for index in range(start, end):
        text = lines[index].strip()
        if index in heading_lines or not text:
            continue
        text = re.sub(r"^(?:>\s*)+", "", text)
        if index + 1 < end and _table_separator(lines[index + 1]):
            continue  # Markdown table column names are not requirement content.
        if re.fullmatch(r"[|: \t-]+", text) or re.match(r"^!?\[[^]]*\]\([^)]*\)\s*$", text):
            continue
        cells = text.strip("|").split("|") if "|" in text else [text]
        if any(_content_cell(cell) for cell in cells):
            return True
    return False


def _reject(code, message):
    raise PrdGenerationError(502, code, message + "；未创建PRD版本，原需求与任务已保留。")


def validate_generated_prd(markdown: str) -> None:
    """Inspect only; successful validation does not modify even one character.

    Completion/scene/error checks remain in the adapter. Only the durable full
    creation path calls this function; it never applies to manual/partial work.
    """
    if not isinstance(markdown, str) or not markdown.strip() or "\x00" in markdown or len(markdown) > MAX_PRD_OUTPUT_CHARACTERS:
        _reject("agent_prd_invalid_content", "Agent未返回有效的PRD正文")
    if markdown.lstrip().startswith(("{", "[")):
        # A Markdown link/list is allowed; a parseable object/array is not a PRD.
        import json
        try:
            wrapped = json.loads(markdown)
        except (ValueError, RecursionError):
            wrapped = None
        if isinstance(wrapped, (dict, list)):
            _reject("agent_prd_invalid_content", "Agent返回了JSON包装，未返回约定的PRD Markdown正文")
    lines, open_fence = _document_lines(markdown)
    headings = _headings(lines)
    heading_lines = {line for h in headings for line in range(h.start, h.end)}
    bodies = {}
    for position, heading in enumerate(headings):
        if heading.chapter is None:
            continue
        end = len(lines)
        for following in headings[position + 1:]:
            if following.chapter is not None or following.level <= heading.level:
                end = following.start
                break
        bodies.setdefault(heading.chapter, []).append((heading.end, end))
    core_present = all(any(_has_body(lines, start, end, heading_lines) for start, end in bodies.get(chapter, [])) for chapter in (2, 3, 4))
    # Classification hints only. Words describing business failures in a valid
    # PRD never trigger this branch; no whole-document keyword blacklist.
    if not core_present:
        prose = "\n".join(line for i, line in enumerate(lines) if i not in heading_lines and not line.lstrip().startswith(("|", ">")))
        if _FAILURE.search(prose):
            _reject("agent_prd_invalid_content", "Agent返回的是生成失败说明，没有生成有效PRD")
        if _NEEDS_INPUT.search(prose):
            _reject("agent_prd_needs_input", "Agent仅返回补充信息请求，尚未生成完整PRD")
    # Only explicit standalone completion notices, not quoted UI error strings.
    if any(_is_truncation_notice(line) for line in lines):
        _reject("agent_prd_incomplete", "Agent明确提示输出被截断，PRD尚未生成完整")
    if open_fence:
        _reject("agent_prd_incomplete", "PRD包含未闭合的代码块，结果可能不完整")
    missing = [name for index, name in enumerate(CHAPTERS) if index not in bodies]
    if missing:
        _reject("agent_prd_incomplete", "完整PRD缺少正文章节：" + "、".join(missing))
    empty = [CHAPTERS[index] for index in (2, 3, 4) if not any(_has_body(lines, start, end, heading_lines) for start, end in bodies[index])]
    if empty:
        _reject("agent_prd_invalid_content", "PRD核心章节只有标题、占位或空模板：" + "、".join(empty))
