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

from .embeddings import cosine

# 混合检索里向量占的比重。BM25 对专有名词和代码标识符更稳，所以向量给 0.6。
VECTOR_WEIGHT = 0.6

# 单块硬上限。超过就把一整段按句号再切。
MAX_CHUNK_CHARS = 480

# 中文比英文信息密度高：400 多字已经能装下好几条互不相干的规则，
# 所以超过这个长度且有多段时，按段落一块一块切，而不是合并成一大块。
TARGET_CHUNK_CHARS = 220

# 太短的段落并到下一段，避免索引里全是碎片。
MIN_PARA_CHARS = 40

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
    return _split_long_chunks(chunks, path.stem)


def _pack(text: str, limit: int) -> list[str]:
    """把长文本按段落打包成不超过 limit 的若干块。单段超长时再按句号切。"""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= limit:
            units.append(paragraph)
            continue
        # 一整段都超长（常见于从 Word 粘出来、中间没有空行的情况）
        sentences = [s for s in re.split(r"(?<=[。！？；.!?])", paragraph) if s.strip()]
        buffer = ""
        for sentence in sentences:
            if buffer and len(buffer) + len(sentence) > limit:
                units.append(buffer)
                buffer = ""
            buffer += sentence
        if buffer.strip():
            units.append(buffer)

    pieces: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) > limit:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{unit}".strip() if current else unit
    if current.strip():
        pieces.append(current)
    return pieces


def _chunk_text(text: str) -> list[str]:
    """把一段正文切成若干块。

    规则：先按空行分段，过短的段落并入相邻段；如果整段已经够短且只有一两个
    段落，就保持原样。否则一段一块——中文规范里「一条规则一段」是最常见写法，
    按段切出来的块才具备检索意义。
    """
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return [text]

    units: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        buffer = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if len(buffer) >= MIN_PARA_CHARS:
            units.append(buffer)
            buffer = ""
    if buffer:
        units.append(buffer)

    if len(units) <= 1 and len(text) <= TARGET_CHUNK_CHARS:
        return [text]

    pieces: list[str] = []
    for unit in units:
        pieces.extend(_pack(unit, MAX_CHUNK_CHARS))
    return pieces


def _piece_title(text: str, index: int) -> str:
    """给没有标题的块起一个可读的名字，用于引用路径。"""
    first = text.strip().splitlines()[0] if text.strip() else ""
    first = re.sub(r"^[#>\-*+\d.、\s]+", "", first).strip()
    first = first.split("。")[0].strip()
    if not first:
        return f"第 {index} 段"
    return first[:24] + ("…" if len(first) > 24 else "")


def _split_long_chunks(chunks: list[Chunk], stem: str) -> list[Chunk]:
    """按标题切完还不够：用户导入的规范常常没有 Markdown 标题，
    整篇会变成一块，检索就退化成了「要么全中要么全不中」。
    """
    result: list[Chunk] = []
    for chunk in chunks:
        pieces = _chunk_text(chunk.text)
        if len(pieces) <= 1:
            result.append(chunk)
            continue
        has_real_heading = chunk.heading not in ("", stem)
        seen: set[str] = set()
        for index, piece in enumerate(pieces, 1):
            heading = f"{chunk.heading} · {index}" if has_real_heading else _piece_title(piece, index)
            while heading in seen:  # 首句撞车时补序号，保证引用路径唯一
                heading = f"{heading}({index})"
            seen.add(heading)
            result.append(Chunk(doc=chunk.doc, heading=heading, text=piece))
    return result


class KnowledgeBase:
    def __init__(self, root: Path | None = None, top_k: int = 3, embedder: Any = None) -> None:
        self.root = root
        self.default_top_k = top_k
        self.chunks: list[Chunk] = []
        self._document_frequency: Counter = Counter()
        self._average_length = 0.0
        self.embedder = embedder
        self._vectors: list[list[float]] = []
        self.vector_status = "pending" if embedder is not None else "off"
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
            # knowledge/custom/ 下是用户导入的规范，只能通过 custom:名字 显式选用，
            # 不能混进内置团队规范的索引里，否则不同团队的规范会互相污染。
            relative = path.relative_to(self.root)
            if "custom" in relative.parts[:-1]:
                continue
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
        bm25: list[float] = []

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
            bm25.append(score)

        # 有向量就混合检索：向量管同义改写，BM25 管专有名词。
        # 向量这一步失败时保持纯 BM25，并把原因记在 vector_status 里。
        vector_scores = self._query_vectors(query)
        if vector_scores is None:
            combined = [(score, i) for i, score in enumerate(bm25) if score > 0]
        else:
            peak = max(bm25) if bm25 else 0.0
            combined = []
            for i, score in enumerate(bm25):
                bm25_norm = (score / peak) if peak > 0 else 0.0
                cos = (vector_scores[i] + 1) / 2  # -1..1 映射到 0..1
                mixed = VECTOR_WEIGHT * cos + (1 - VECTOR_WEIGHT) * bm25_norm
                if mixed > 0:
                    combined.append((mixed, i))

        combined.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "source": self.chunks[i].citation,
                "score": round(score, 3),
                "text": self.chunks[i].text[:600],
            }
            for score, i in combined[:k]
        ]

    def attach_embedder(self, embedder: Any) -> None:
        """挂上向量后端。已建好的索引会在下次检索时补算向量。"""
        self.embedder = embedder
        self._vectors = []
        self.vector_status = "off" if embedder is None else "pending"

    def _ensure_vectors(self) -> bool:
        if self.embedder is None or not self.chunks:
            return False
        if len(self._vectors) == len(self.chunks):
            return self.vector_status == "ready"
        try:
            self._vectors = self.embedder.embed(
                [f"{chunk.heading}\n{chunk.text}" for chunk in self.chunks]
            )
            self.vector_status = "ready"
            return True
        except Exception as exc:  # noqa: BLE001 向量化失败不该让整个检索失败
            self._vectors = []
            self.vector_status = f"failed: {exc}"
            return False

    def _query_vectors(self, query: str) -> list[float] | None:
        """返回每个块的余弦相似度；不可用时返回 None，调用方退回 BM25。"""
        if not self._ensure_vectors():
            return None
        try:
            query_vector = self.embedder.embed([query])[0]
        except Exception as exc:  # noqa: BLE001
            self.vector_status = f"failed: {exc}"
            return None
        return [cosine(query_vector, vector) for vector in self._vectors]

    def stats(self) -> dict[str, Any]:
        return {
            "documents": len({chunk.doc for chunk in self.chunks}),
            "chunks": len(self.chunks),
            "vocabulary": len(self._document_frequency),
            "vector": self.vector_status,
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
