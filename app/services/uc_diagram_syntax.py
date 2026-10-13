"""Bounded UC notation adapter; never edits Agent code or invents relationships."""
import re

UC_DIAGRAM_CONTRACT = "uc-diagram-mermaid-v3"
UC_HEADER = "---\nconfig:\n  theme: default\n  look: classic\n---\n"
MAX_CHARACTERS = 100_000
MAX_EDGES = 500
ID = r"[A-Za-z_][A-Za-z0-9_-]*"
LABEL = r'"([^"\r\n]*)"'


class UcDiagramSyntaxError(ValueError):
    pass


def _fail(message: str) -> None:
    raise UcDiagramSyntaxError(message)


def _safe_text(code: str) -> str:
    value = code.replace("\r\n", "\n").strip()
    if not value or len(value) > MAX_CHARACTERS or "\x00" in value:
        _fail("UC图代码为空或超过安全渲染限制")
    if any(x in value.lower() for x in ("%%{", "@{", "<script", "<iframe", "<img", "<style", "<foreignobject", "javascript:", "data:", "https:", "http:", "url(")):
        _fail("UC图包含不允许的配置、脚本或外部交互")
    if re.search(r"^\s*click\b", value, re.I | re.M) or re.search(r"</?[A-Za-z][^>]*>", value):
        _fail("UC图包含不允许的HTML或交互")
    return value


def _without_frontmatter(value: str) -> str:
    lines = value.splitlines()
    if lines[0].strip() != "---":
        return value
    close = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if close is None or close > 8:
        _fail("UC图配置只允许theme: default和look: classic")
    config = [line for line in lines[1:close] if line.strip()]
    if not config or config[0] != "config:":
        _fail("UC图配置只允许theme: default和look: classic")
    seen: set[str] = set()
    for line in config[1:]:
        match = re.fullmatch(r"[ ]+(theme: default|look: classic)[ ]*", line)
        if not match or match[1].split(":")[0] in seen:
            _fail("UC图配置只允许theme: default和look: classic")
        seen.add(match[1].split(":")[0])
    return "\n".join(lines[close + 1:]).strip()


def _label(value: str) -> str:
    if not value.strip() or any(x in value for x in ('<', '>', '\\')):
        _fail("UC图标签必须是非空纯文本，不支持HTML或转义脚本")
    return value


def _validate_native(body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("%%")]
    if not lines or lines[0] != "usecase-beta":
        _fail("UC图必须使用usecase-beta语法")
    nodes: dict[str, str] = {}
    boundaries: set[str] = set()
    inside = False
    direction = False
    edges: list[tuple[str, str, str]] = []
    for line in lines[1:]:
        if re.fullmatch(r"direction (?:LR|RL|TB|TD|BT)", line):
            if direction or inside or nodes or boundaries:
                _fail("UC图仅允许在节点前声明一次方向")
            direction = True
            continue
        boundary = re.fullmatch(rf"systemBoundary ({ID})\[{LABEL}\]", line)
        if boundary:
            if inside or boundaries or boundary[1] in nodes:
                _fail("UC图必须只包含一个不嵌套的系统边界")
            _label(boundary[2])
            boundaries.add(boundary[1])
            inside = True
            continue
        if line == "end":
            if not inside:
                _fail("UC图系统边界不匹配")
            inside = False
            continue
        node = re.fullmatch(rf"(?:(actor) )?({ID})\({LABEL}\)", line)
        if node:
            kind = "actor" if node[1] else "uc"
            if (kind == "actor" and inside) or (kind == "uc" and not inside):
                _fail("参与者必须在系统边界外，用例必须在系统边界内")
            if node[2] in nodes or node[2] in boundaries:
                _fail("UC图存在重复节点ID")
            _label(node[3])
            nodes[node[2]] = kind
            continue
        relation = re.fullmatch(rf"({ID}) (?:\.\.>\s*:\s*(include|extend)\s+|(--\|>|-->|--|---)\s+)({ID})", line)
        if relation:
            if inside or len(edges) >= MAX_EDGES:
                _fail("UC图关系必须位于系统边界外且不能超过500条")
            edges.append((relation[1], relation[4], relation[2] or relation[3]))
            continue
        _fail("UC图包含不支持的语句；仅允许参与者、用例、系统边界和明确关系")
    if inside or len(boundaries) != 1 or "uc" not in nodes.values():
        _fail("UC图缺少完整的系统边界或用例")
    for start, end, relation in edges:
        if start not in nodes or end not in nodes:
            _fail("UC图关系引用了未声明的节点")
        if relation in ("include", "extend") and (nodes[start] != "uc" or nodes[end] != "uc"):
            _fail("include/extend关系只能连接用例")
    return "\n".join(lines)


def _legacy_statements(body: str) -> list[str]:
    # Semicolons inside quoted labels are text, not statement separators.
    result: list[str] = []
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("%%"):
            continue
        quote = False
        start = 0
        for i, char in enumerate(line):
            if char == '"':
                quote = not quote
            elif char == ";" and not quote:
                if line[start:i].strip():
                    result.append(line[start:i].strip())
                start = i + 1
        if line[start:].strip():
            result.append(line[start:].strip())
    return result


def _convert_legacy(body: str) -> str:
    lines = _legacy_statements(body)
    heading = re.fullmatch(r"(?:flowchart|graph) +(TD|TB|BT|LR|RL)", lines[0], re.I) if lines else None
    if not heading:
        _fail("UC图必须使用usecase-beta语法")
    converted = ["usecase-beta", "direction " + heading[1].upper()]
    inside = False
    for line in lines[1:]:
        boundary = re.fullmatch(rf"subgraph ({ID})\[{LABEL}\]", line)
        if boundary:
            converted.append(f'systemBoundary {boundary[1]}["{boundary[2]}"]')
            inside = True
            continue
        if line == "end":
            converted.append(line)
            inside = False
            continue
        actor = re.fullmatch(rf"(actor_[A-Za-z0-9_-]+)\[{LABEL}\]", line, re.I)
        uc = re.fullmatch(rf"({ID})\(\[{LABEL}\]\)", line) or re.fullmatch(rf"({ID})\({LABEL}\)", line)
        if actor and not inside:
            converted.append(f'actor {actor[1]}("{actor[2]}")')
            continue
        if uc and inside:
            converted.append(f'{uc[1]}("{uc[2]}")')
            continue
        relation = re.fullmatch(rf"({ID})\s*(-\.->|-->|---)\s*(?:\|([^|]+)\|\s*)?({ID})", line)
        if relation and not inside:
            start, arrow, label, end = relation.groups()
            if arrow == "-.->" and label in ("include", "extend"):
                converted.append(f"{start} ..> : {label} {end}")
            elif arrow == "-->" and label == "generalize":
                converted.append(f"{start} --|> {end}")
            elif arrow in ("---", "-->") and label is None:
                converted.append(f"{start} {'--' if arrow == '---' else arrow} {end}")
            else:
                _fail("旧UC图关系无法无损转换，请由Agent返回usecase-beta")
            continue
        _fail("旧UC图包含无法明确转换的语句，请由Agent返回usecase-beta")
    # Unknown nodes, duplicate IDs, nesting, or misplaced actors are never guessed.
    return _validate_native("\n".join(converted))


# Split documents, not Mermaid statements. Mermaid itself validates notation.
_DIAGRAM_START = re.compile(r"^(?:usecase-beta(?=[;\s]|$)|(?:flowchart|graph)\s+(?:LR|RL|TB|TD|BT)(?=[;\s]|$))", re.I)
MAX_DIAGRAMS = 32


def _split_diagrams(value: str) -> list[str]:
    documents: list[str] = []
    current: list[str] = []
    has_header = False
    in_frontmatter = False
    in_fence = False
    quoted = False

    def flush() -> None:
        nonlocal current, has_header
        if any(line.strip() and not line.lstrip().startswith("%%") for line in current):
            documents.append("\n".join(current).strip())
            if len(documents) > MAX_DIAGRAMS:
                _fail("UC图数量超过安全渲染限制（32张）")
        current = []
        has_header = False

    for line in value.splitlines():
        stripped = line.strip()
        if not quoted and not in_frontmatter and re.fullmatch(r"```(?:mermaid)?", stripped, re.I):
            if in_fence:
                if stripped != "```":
                    _fail("UC图代码围栏未正确闭合")
                flush()
                in_fence = False
            else:
                if current:
                    flush()
                in_fence = True
            continue
        if not quoted and stripped == "---":
            if not in_frontmatter and has_header:
                flush()
            in_frontmatter = not in_frontmatter
            current.append(line)
            continue
        if in_frontmatter:
            current.append(line)
            continue
        if not quoted and _DIAGRAM_START.match(stripped):
            if has_header:
                flush()
            has_header = True
        current.append(line)
        if not stripped.startswith("%%"):
            escaped = False
            for char in line:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = not quoted
    if in_fence or in_frontmatter:
        _fail("UC图代码围栏或前置配置未正确闭合")
    flush()
    return documents


def normalize_uc_diagram(code: str) -> str:
    documents = _split_diagrams(_safe_text(code))
    results: list[str] = []
    for document in documents:
        # Leading comments are legal and do not configure the renderer.
        lines = document.splitlines()
        while lines and (not lines[0].strip() or lines[0].lstrip().startswith("%%")):
            lines.pop(0)
        body = _without_frontmatter("\n".join(lines))
        first = next((line.strip() for line in body.splitlines() if line.strip() and not line.lstrip().startswith("%%")), "")
        if not _DIAGRAM_START.match(first):
            _fail("UC图需要usecase-beta或flowchart/graph图类型声明")
        if re.match(r"^(?:flowchart|graph)\b", first, re.I):
            try:
                # Preserve the existing lossless actor-to-stick-figure adapter.
                body = _convert_legacy(body)
            except UcDiagramSyntaxError:
                # Valid legacy syntax outside that small adapter is not rejected.
                pass
        # Legacy fallback stays byte-for-byte notation-compatible with existing callers.
        results.append(body if re.match(r"^(?:flowchart|graph)\b", body, re.I) else UC_HEADER + body)
    result = "\n\n".join(results)
    if not result or len(result) > MAX_CHARACTERS:
        _fail("UC图代码为空或超过安全渲染限制")
    return result
