"""向量化后端测试：不触网，用假 opener 模拟接口。"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

from devprism.embeddings import (
    CachedEmbedder,
    EmbeddingCache,
    EmbedError,
    RemoteEmbedder,
    cosine,
    make_embedder,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, payload: dict | None = None, code: int | None = None) -> None:
        self.payload = payload or {}
        self.code = code
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.code is not None:
            raise urllib.error.HTTPError(
                request.full_url, self.code, "boom", {}, io.BytesIO(b'{"error":"nope"}')
            )
        return _Response(self.payload)


class _CountingEmbedder:
    """按文本长度给出确定向量，便于断言调用次数。"""

    name = "fake:counting"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]


class TestCosine(unittest.TestCase):
    def test_identical_vectors(self):
        self.assertAlmostEqual(cosine([1, 2, 3], [1, 2, 3]), 1.0)

    def test_orthogonal_vectors(self):
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)

    def test_zero_length(self):
        self.assertEqual(cosine([], [1]), 0.0)
        self.assertEqual(cosine([0, 0], [1, 1]), 0.0)

    def test_dimension_mismatch(self):
        self.assertEqual(cosine([1, 2], [1, 2, 3]), 0.0)


class TestRemoteEmbedder(unittest.TestCase):
    def _embedder(self) -> RemoteEmbedder:
        return RemoteEmbedder("http://x/v1/", "key", "text-embedding-3-small")

    def test_endpoint_join(self):
        self.assertEqual(self._embedder().endpoint, "http://x/v1/embeddings")

    def test_parses_and_sorts_by_index(self):
        embedder = self._embedder()
        embedder._opener = _FakeOpener({"data": [
            {"index": 1, "embedding": [0.3, 0.4]},
            {"index": 0, "embedding": [0.1, 0.2]},
        ]})

        vectors = embedder.embed(["a", "b"])

        self.assertEqual(vectors, [[0.1, 0.2], [0.3, 0.4]])

    def test_404_gives_actionable_message(self):
        embedder = self._embedder()
        embedder._opener = _FakeOpener(code=404)

        with self.assertRaises(EmbedError) as ctx:
            embedder.embed(["a"])

        self.assertIn("EMBED_BASE_URL", str(ctx.exception))

    def test_count_mismatch_is_rejected(self):
        embedder = self._embedder()
        embedder._opener = _FakeOpener({"data": [{"index": 0, "embedding": [1.0]}]})

        with self.assertRaises(EmbedError) as ctx:
            embedder.embed(["a", "b"])

        self.assertIn("条数", str(ctx.exception))

    def test_empty_input_makes_no_request(self):
        embedder = self._embedder()
        opener = _FakeOpener({"data": []})
        embedder._opener = opener

        self.assertEqual(embedder.embed([]), [])
        self.assertEqual(opener.calls, 0)


class TestCachedEmbedder(unittest.TestCase):
    def test_second_call_hits_cache(self):
        with TemporaryDirectory() as tmp:
            inner = _CountingEmbedder()
            cached = CachedEmbedder(inner, EmbeddingCache(Path(tmp) / "v.sqlite3"))
            try:
                first = cached.embed(["hello", "world"])
                second = cached.embed(["hello", "world"])

                self.assertEqual(first, second)
                self.assertEqual(len(inner.calls), 1, "重复文本不应再次请求")
            finally:
                cached.close()

    def test_only_uncached_texts_are_sent(self):
        with TemporaryDirectory() as tmp:
            inner = _CountingEmbedder()
            cached = CachedEmbedder(inner, EmbeddingCache(Path(tmp) / "v.sqlite3"))
            try:
                cached.embed(["a"])
                cached.embed(["a", "bb"])

                self.assertEqual(inner.calls[0], ["a"])
                self.assertEqual(inner.calls[1], ["bb"])
            finally:
                cached.close()

    def test_cache_survives_reopen(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.sqlite3"
            cache = EmbeddingCache(path)
            cache.put("k", "model", [1.0, 2.0])
            cache.close()

            reopened = EmbeddingCache(path)
            self.assertEqual(reopened.get("k"), [1.0, 2.0])
            reopened.close()


class TestFactory(unittest.TestCase):
    def test_disabled_without_model(self):
        class S:
            embed_enabled = False
            embed_base_url = "http://x/v1"
            embed_api_key = "k"
            embed_model = ""
            timeout = 1.0
            proxy = None

        self.assertIsNone(make_embedder(S()))


if __name__ == "__main__":
    unittest.main()
