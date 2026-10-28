"""极简 Markdown → HTML，只覆盖本项目的输出形态。

为什么不用 marked.js / python-markdown：
  * 浏览器端引 CDN 意味着演示时断网就白屏；
  * 服务端引入依赖又破坏零依赖的承诺。

支持：标题、表格、列表、代码块、引用、分割线、加粗/斜体/行内代码/链接。
不支持嵌套列表、图片、脚注——我们的输出现阶段用不到。
"""

from __future__ import annotations

import html
import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_LIST_ITEM = re.compile(r"^([-*+]|\d+\.)\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
_FENCE = re.compile(r"^```")

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = _INLINE_CODE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC.sub(r"<em>\1</em>", escaped)
    escaped = _LINK.sub(
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', escaped
    )
    return escaped


def _cells(row: str) -> list[str]:
    body = row.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _table(header: str, rows: list[str]) -> str:
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in _cells(header))
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in _cells(row)) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _starts_block(text: str) -> bool:
    return bool(
        _HEADING.match(text)
        or _FENCE.match(text)
        or _LIST_ITEM.match(text)
        or text.startswith("|")
        or text.startswith(">")
        or _HR.match(text)
    )


def render(markdown: str) -> str:
    lines = markdown.splitlines()
    total = len(lines)
    out: list[str] = []
    index = 0

    while index < total:
        stripped = lines[index].strip()

        if not stripped:
            index += 1
            continue

        if _FENCE.match(stripped):
            language = stripped[3:].strip()
            index += 1
            buffer: list[str] = []
            while index < total and not _FENCE.match(lines[index].strip()):
                buffer.append(lines[index])
                index += 1
            index += 1
            attr = f' class="language-{html.escape(language)}"' if language else ""
            code = html.escape("\n".join(buffer))
            out.append(f"<pre><code{attr}>{code}</code></pre>")
            continue

        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if _HR.match(stripped):
            out.append("<hr>")
            index += 1
            continue

        if (
            stripped.startswith("|")
            and index + 1 < total
            and _TABLE_SEP.match(lines[index + 1].strip())
        ):
            header = stripped
            index += 2
            rows: list[str] = []
            while index < total and lines[index].strip().startswith("|"):
                rows.append(lines[index].strip())
                index += 1
            out.append(_table(header, rows))
            continue

        if stripped.startswith(">"):
            buffer = []
            while index < total and lines[index].strip().startswith(">"):
                buffer.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append(f"<blockquote>{_inline(' '.join(buffer))}</blockquote>")
            continue

        if _LIST_ITEM.match(stripped):
            ordered = bool(re.match(r"^\d+\.", stripped))
            items: list[str] = []
            while index < total:
                item = _LIST_ITEM.match(lines[index].strip())
                if not item:
                    break
                items.append(item.group(2))
                index += 1
            tag = "ol" if ordered else "ul"
            body = "".join(f"<li>{_inline(text)}</li>" for text in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        buffer = []
        while index < total:
            current = lines[index].strip()
            if not current or _starts_block(current):
                break
            buffer.append(current)
            index += 1
        out.append(f"<p>{_inline(' '.join(buffer))}</p>")

    return "\n".join(out)
