"""从源码里抽出类、方法、函数等符号，用于「结构 + 功能」展示。

用正则而不是语法树：这里只需要给用户一个可读的骨架，不需要精确到 AST。
多语言各一套规则，抽不到就返回空，不抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any

MAX_SYMBOLS = 60

_LANG_BY_EXT = {
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rb": "ruby", ".rs": "rust",
}

_RULES: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "java": [
        ("class", re.compile(r"^\s*(?:public\s+|final\s+|abstract\s+|sealed\s+)*(?:class|interface|enum|record)\s+(\w+)")),
        ("method", re.compile(r"^\s*(?:public|protected|private)\s+(?:static\s+|final\s+|synchronized\s+|abstract\s+)*[\w.<>\[\], ]+?\s+(\w+)\s*\(")),
    ],
    "kotlin": [
        ("class", re.compile(r"^\s*(?:data\s+|sealed\s+|abstract\s+|open\s+)*class\s+(\w+)")),
        ("function", re.compile(r"^\s*fun\s+(?:<[^>]+>\s*)?(\w+)\s*\(")),
    ],
    "python": [
        ("class", re.compile(r"^\s*class\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")),
    ],
    "javascript": [
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(")),
        ("function", re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(?[^=]*?=>")),
    ],
    "go": [
        ("type", re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)")),
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(")),
    ],
    "rust": [
        ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*\(")),
    ],
}


@dataclass
class Symbol:
    name: str
    kind: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def language_of(path: str) -> str | None:
    lowered = path.lower()
    for ext, lang in _LANG_BY_EXT.items():
        if lowered.endswith(ext):
            return lang
    return None


def extract_symbols(path: str, text: str, limit: int = MAX_SYMBOLS) -> list[dict[str, Any]]:
    lang = language_of(path)
    if not lang:
        return []
    rules = _RULES.get(lang, [])

    found: list[Symbol] = []
    seen: set[tuple[str, str]] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if len(found) >= limit:
            break
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "*", "/*", "#")):
            continue
        for kind, pattern in rules:
            match = pattern.match(line)
            if not match:
                continue
            name = match.group(1)
            key = (kind, name)
            if key in seen:
                break
            seen.add(key)
            found.append(Symbol(name=name, kind=kind, line=number))
            break

    return [symbol.to_dict() for symbol in found]


def summarize_symbols(symbols: list[dict[str, Any]]) -> str:
    """给 LLM 用的一行摘要，比如「3 个类、12 个方法」。"""
    classes = sum(1 for s in symbols if s["kind"] in ("class", "type", "struct"))
    functions = len(symbols) - classes
    parts = []
    if classes:
        parts.append(f"{classes} 个类型")
    if functions:
        parts.append(f"{functions} 个函数/方法")
    return "、".join(parts) if parts else "未识别到符号"
