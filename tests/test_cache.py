"""缓存测试：TTL 与断网兜底。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from devprism.cache import HttpCache


class TestHttpCache(unittest.TestCase):
    def _cache(self, tmp: str, ttl: int = 3600) -> HttpCache:
        return HttpCache(Path(tmp) / "cache.sqlite3", ttl=ttl)

    def test_roundtrip(self):
        with TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            try:
                cache.put("k", {"a": 1}, "etag")
                self.assertEqual(cache.get("k"), ({"a": 1}, "etag"))
            finally:
                cache.close()

    def test_expired_entry_is_hidden_from_get(self):
        with TemporaryDirectory() as tmp:
            cache = self._cache(tmp, ttl=0)
            try:
                cache.put("k", {"a": 1}, "etag")
                self.assertIsNone(cache.get("k"))
            finally:
                cache.close()

    def test_peek_ignores_ttl(self):
        """断网兜底全靠它：过期了也得能把上次的数据取出来。"""
        with TemporaryDirectory() as tmp:
            cache = self._cache(tmp, ttl=0)
            try:
                cache.put("k", {"a": 1}, "etag")
                peeked = cache.peek("k")
                self.assertIsNotNone(peeked)
                self.assertEqual(peeked[0], {"a": 1})
            finally:
                cache.close()

    def test_peek_missing_key(self):
        with TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            try:
                self.assertIsNone(cache.peek("nope"))
            finally:
                cache.close()


if __name__ == "__main__":
    unittest.main()
