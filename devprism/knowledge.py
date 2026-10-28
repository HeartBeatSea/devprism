"""知识库：Markdown 切分 + BM25 检索，零第三方依赖。

**为什么先上 BM25 而不是向量检索**

团队规范通常只有几篇文档，向量检索的相对优势体现不出来；BM25 命中哪个词
一目了然，调参和排错都直观，而且不需要 embedding 服务和额外依赖。
等文档规模上百篇、开始出现同义不同词的召回问题，再引入向量做混合检索。

**为什么用字符二元组**

中文没有空格，不装分词器就无法切词。用「单字 + 相邻字二元组」建索引，
既不需要词典，又能保住「提交规范」这类短语的局部顺序，召回够用。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ASCII_WORD = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_HTML_TAG = re.compile(r"<[^>]+>")


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _ASCII_WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class Chunk:
    doc: str
    heading: str
    text: str
    tokens: Counter = field(default_factory=Counter)

    @property
    def length(self) -> int:
        return sum(self.tokens.values())

    @property
    def citation(self) -> str:
        return f"{self.doc}#{self.heading}"


def split_markdown(path: Path, text: str) -> list[Chunk]:
    """按标题层级切块，保留「一级 / 二级」这样的路径，便于精确引用。"""
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    heading_path = ""

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
            chunks.append(Chunk(doc=path.name, heading=heading_path or path.stem, text=body))

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            # 去掉 <a name="..."></a> 这类锚点，否则引用路径会变成
            # 「Contributing to Angular / <a name="submit"></a> Submission Guidelines」，模型写不对
            title = _HTML_TAG.sub("", match.group(2)).strip()
            if not title:
                continue
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading_path = " / ".join(item for _, item in stack)
        else:
            buffer.append(line)
    flush()
    return chunks


class KnowledgeBase:
    def __init__(self, root: Path | None = None, top_k: int = 3) -> None:
        self.root = root
        self.default_top_k = top_k
        self.chunks: list[Chunk] = []
        self._document_frequency: Counter = Counter()
        self._average_length = 0.0
        if root is not None:
            self._load()

    def add_document(self, name: str, text: str) -> None:
        """从内存加载一篇文档，用于「用目标仓库自己的规范」这种场景。"""
        for chunk in split_markdown(Path(name), text):
            chunk.tokens = Counter(tokenize(f"{chunk.heading}\n{chunk.text}"))
            self.chunks.append(chunk)
        self._reindex()

    def _reindex(self) -> None:
        self._document_frequency.clear()
        for chunk in self.chunks:
            for term in chunk.tokens:
                self._document_frequency[term] += 1
        total = sum(chunk.length for chunk in self.chunks)
        self._average_length = total / len(self.chunks) if self.chunks else 0.0

    def _load(self) -> None:
        if not self.root or not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for chunk in split_markdown(path, text):
                chunk.tokens = Counter(tokenize(f"{chunk.heading}\n{chunk.text}"))
                self.chunks.append(chunk)
        self._reindex()

    def search(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        k = top_k or self.default_top_k
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []

        total_docs = len(self.chunks)
        k1, b = 1.5, 0.75
        average = self._average_length or 1.0
        scored: list[tuple[float, Chunk]] = []

        for chunk in self.chunks:
            score = 0.0
            for term in terms:
                frequency = chunk.tokens.get(term, 0)
                if not frequency:
                    continue
                df = self._document_frequency.get(term, 0)
                idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
                denominator = frequency + k1 * (1 - b + b * chunk.length / average)
                score += idf * frequency * (k1 + 1) / denominator
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "source": chunk.citation,
                "score": round(score, 3),
                "text": chunk.text[:600],
            }
            for score, chunk in scored[:k]
        ]

    def stats(self) -> dict[str, Any]:
        return {
            "documents": len({chunk.doc for chunk in self.chunks}),
            "chunks": len(self.chunks),
            "vocabulary": len(self._document_frequency),
        }


# ---------------------------------------------------------------- 用户自定义规范

CUSTOM_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "custom"
_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff.-]+")


def _slugify(name: str) -> str:
    cleaned = _SAFE_NAME.sub("-", (name or "").strip()).strip("-.")
    return cleaned or "custom"


def list_custom() -> list[dict[str, Any]]:
    """列出用户导入的规范文档。"""
    if not CUSTOM_DIR.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(CUSTOM_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        items.append({
            "slug": path.stem,
            "name": path.stem,
            "chars": len(text),
            "updated_at": path.stat().st_mtime,
        })
    return items


def save_custom(name: str, content: str) -> str:
    """保存一份用户规范，返回可引用的 slug。"""
    if not content.strip():
        raise ValueError("规范内容不能为空")
    slug = _slugify(name)
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    (CUSTOM_DIR / f"{slug}.md").write_text(content, encoding="utf-8")
    return slug


def delete_custom(slug: str) -> bool:
    path = CUSTOM_DIR / f"{_slugify(slug)}.md"
    if path.is_file():
        path.unlink()
        return True
    return False


def load_custom(slug: str) -> KnowledgeBase | None:
    path = CUSTOM_DIR / f"{_slugify(slug)}.md"
    if not path.is_file():
        return None
    base = KnowledgeBase(None)
    base.add_document(f"{path.stem}.md", path.read_text(encoding="utf-8"))
    return base


CONTRIBUTING_PATHS = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "docs/CONTRIBUTING.md",
    "CONTRIBUTING",
)


def load_conventions_from_repo(client, owner: str, name: str):
    """抓取目标仓库自己的贡献规范。

    用它对照它自己的提交，规范归属才对得上——拿本团队的规范去评判别人的仓库
    是没道理的。找不到时返回 (None, None)，由调用方决定回退策略。
    """
    for path in CONTRIBUTING_PATHS:
        text = client.file_text(owner, name, path)
        if text:
            base = KnowledgeBase(None)
            base.add_document(f"{name}/{path.rsplit('/', 1)[-1]}", text)
            return base, path
    return None, None
