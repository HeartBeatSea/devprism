"""极简 YAML 子集解析器 —— 只为工作流定义服务。

**为什么不用 PyYAML**：核心引擎承诺零第三方依赖，clone 下来
`python -m devprism run ...` 就能跑。工作流的语法是我们自己定的、范围可控，
用一百多行换掉一个依赖是划算的。

**支持的语法**

- 行注释（行首或空白之后的 `#`）
- 嵌套映射，靠缩进表达（统一用 2 空格）
- 列表（`- ` 开头，元素可以是标量或映射）
- 块文本（`|`，保留换行与空行）
- 单/双引号字符串、整数、浮点、true/false/null

**不支持的语法（遇到会直接报错，不静默出错）**

- 流式写法 `{a: 1}`、`[1, 2]`
- 锚点/别名 `&`、`*`，多文档 `---`，标签 `!!`
- Tab 缩进
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class YamlError(ValueError):
    pass


@dataclass
class _Line:
    indent: int
    text: str
    number: int
    block: list[str] | None = None


def _strip_comment(raw: str) -> str:
    in_single = in_double = False
    for index, char in enumerate(raw):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or raw[index - 1] in " \t":
                return raw[:index]
    return raw


def _split_key(text: str) -> tuple[str | None, str | None]:
    """按第一个「后面跟空格或行尾」的冒号切分键值。"""
    in_single = in_double = False
    for index, char in enumerate(text):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == ":" and not in_single and not in_double:
            if index + 1 == len(text) or text[index + 1] in " \t":
                return text[:index].strip(), text[index + 1:].strip()
    return None, None


def _scalar(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        if text[0] == '"':
            return inner.replace('\\"', '"').replace("\\n", "\n")
        return inner
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _tokenize(text: str) -> list[_Line]:
    raw_lines = text.splitlines()
    lines: list[_Line] = []
    index = 0

    while index < len(raw_lines):
        raw = raw_lines[index]
        number = index + 1
        index += 1

        if not raw.strip() or raw.strip().startswith("#"):
            continue

        content = _strip_comment(raw).rstrip()
        if not content.strip():
            continue

        indent = len(content) - len(content.lstrip(" "))
        if "\t" in content[:indent]:
            raise YamlError(f"第 {number} 行：缩进不能用 Tab")

        body = content.strip()
        block: list[str] | None = None

        if body.endswith("|"):
            block = []
            base: int | None = None
            while index < len(raw_lines):
                following = raw_lines[index]
                if not following.strip():
                    block.append("")
                    index += 1
                    continue
                following_indent = len(following) - len(following.lstrip(" "))
                if following_indent <= indent:
                    break
                if base is None:
                    base = following_indent
                block.append(following[base:])
                index += 1
            while block and not block[-1]:
                block.pop()

        lines.append(_Line(indent, body, number, block))

    return lines


def _parse_map(lines: list[_Line], pos: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    while pos < len(lines):
        line = lines[pos]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YamlError(f"第 {line.number} 行：缩进不一致")
        if line.text.startswith("- "):
            break

        key, rest = _split_key(line.text)
        if key is None:
            raise YamlError(f"第 {line.number} 行：不是合法的键值对 —— {line.text!r}")
        pos += 1

        if line.block is not None:
            result[key] = "\n".join(line.block)
        elif rest:
            result[key] = _scalar(rest)
        elif pos < len(lines) and lines[pos].indent > indent:
            result[key], pos = _parse(lines, pos, lines[pos].indent)
        else:
            result[key] = None
    return result, pos


def _parse_list(lines: list[_Line], pos: int, indent: int) -> tuple[list, int]:
    items: list[Any] = []
    while pos < len(lines):
        line = lines[pos]
        if line.indent != indent or not line.text.startswith("-"):
            break

        rest = line.text[1:].lstrip()
        if not rest:
            pos += 1
            if pos < len(lines) and lines[pos].indent > indent:
                value, pos = _parse(lines, pos, lines[pos].indent)
            else:
                value = None
            items.append(value)
            continue

        key, _ = _split_key(rest)
        if key is None:
            items.append(_scalar(rest))
            pos += 1
            continue

        # `- id: x` 实际是「列表元素是一张映射表」：把这一行改写成缩进 +
        # 2 的映射行，再交给 _parse_map 吃掉后续同缩进的键。
        nested_indent = indent + 2
        lines[pos] = _Line(nested_indent, rest, line.number, line.block)
        value, pos = _parse_map(lines, pos, nested_indent)
        items.append(value)

    return items, pos


def _parse(lines: list[_Line], pos: int, indent: int):
    if lines[pos].text.startswith("- "):
        return _parse_list(lines, pos, indent)
    return _parse_map(lines, pos, indent)


def loads(text: str) -> Any:
    lines = _tokenize(text)
    if not lines:
        return {}
    value, pos = _parse(lines, 0, lines[0].indent)
    if pos != len(lines):
        raise YamlError(f"第 {lines[pos].number} 行：无法解析（缩进或语法问题）")
    return value


def load_file(path) -> Any:
    from pathlib import Path

    return loads(Path(path).read_text(encoding="utf-8"))
