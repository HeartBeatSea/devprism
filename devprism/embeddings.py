"""可插拔的向量化后端。

默认不启用：没配 `EMBED_MODEL` 时整个项目仍走 BM25，保持零第三方依赖。
配置后启用混合检索——BM25 兜住专有名词与代码标识符，向量兜住同义改写。

任何一步失败都退回 BM25，并且把原因记在 `KnowledgeBase.vector_status` 里，
而不是静默降级或让整次分析失败。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .net import build_opener


class EmbedError(RuntimeError):
    """向量化失败。错误信息要能指出是哪一层出的问题。"""


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度。长度为 0 或维度不一致时返回 0，不抛异常。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    na = math.sqrt(sum(a * a for a in left))
    nb = math.sqrt(sum(b * b for b in right))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class RemoteEmbedder:
    """OpenAI 兼容的 /embeddings 接口。"""

    base_url: str
    api_key: str
    model: str
    timeout: float = 30.0
    proxy: str | None = None
    batch_size: int = 32
    _opener: Any = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self._opener = build_opener(self.proxy)

    @property
    def name(self) -> str:
        return f"remote:{self.model}"

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start:start + self.batch_size]))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": batch}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            if exc.code == 404:
                raise EmbedError(
                    f"向量接口不存在（404）：请检查 EMBED_BASE_URL 是否为 OpenAI 兼容地址。{detail}"
                ) from exc
            raise EmbedError(f"向量接口返回 {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise EmbedError(f"无法连接向量接口（{self.base_url}）：{exc}") from exc

        items = body.get("data")
        if not isinstance(items, list) or len(items) != len(batch):
            raise EmbedError(
                f"向量接口返回的条数不对：期望 {len(batch)}，实际 {len(items) if isinstance(items, list) else '非列表'}"
            )
        items = sorted(items, key=lambda item: item.get("index", 0))
        return [[float(x) for x in item["embedding"]] for item in items]


class EmbeddingCache:
    """按「模型 + 文本」哈希缓存向量，避免每次启动重新计费。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            "  key TEXT PRIMARY KEY,"
            "  model TEXT NOT NULL,"
            "  dim INTEGER NOT NULL,"
            "  vector TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    def get(self, key: str) -> list[float] | None:
        row = self._conn.execute("SELECT vector FROM vectors WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            return [float(x) for x in json.loads(row[0])]
        except (ValueError, TypeError):
            return None

    def put(self, key: str, model: str, vector: list[float]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO vectors (key, model, dim, vector) VALUES (?, ?, ?, ?)",
            (key, model, len(vector), json.dumps(vector, separators=(",", ":"))),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class CachedEmbedder:
    """给任意 Embedder 套一层缓存。只对没见过的文本发请求。"""

    def __init__(self, inner: Embedder, cache: EmbeddingCache) -> None:
        self.inner = inner
        self.cache = cache

    @property
    def name(self) -> str:
        return self.inner.name

    def close(self) -> None:
        self.cache.close()

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.name}\x00{text}".encode("utf-8")).hexdigest()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        keys = [self._key(text) for text in texts]
        found: dict[str, list[float]] = {}
        for key in set(keys):
            cached = self.cache.get(key)
            if cached is not None:
                found[key] = cached

        missing = [i for i, key in enumerate(keys) if key not in found]
        if missing:
            fresh = self.inner.embed([texts[i] for i in missing])
            for i, vector in zip(missing, fresh):
                found[keys[i]] = vector
                self.cache.put(keys[i], self.name, vector)

        return [found[key] for key in keys]


def make_embedder(settings: Any, cache_path: Path | None = None) -> Embedder | None:
    """按配置构造 Embedder。未配置或配置不全时返回 None，调用方继续走 BM25。"""
    if not getattr(settings, "embed_enabled", False):
        return None
    inner = RemoteEmbedder(
        base_url=settings.embed_base_url,
        api_key=settings.embed_api_key,
        model=settings.embed_model,
        timeout=settings.timeout,
        proxy=settings.proxy,
    )
    if cache_path is None:
        return inner
    return CachedEmbedder(inner, EmbeddingCache(cache_path))
