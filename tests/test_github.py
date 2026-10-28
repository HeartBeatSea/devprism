"""GitHub 客户端的离线行为测试（不触网）。"""

from __future__ import annotations

import unittest

from devprism.github import GitHubClient


class TestCacheFallback(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient(token=None, cache=None, timeout=1)

    def test_no_cache_means_no_fallback(self):
        self.assertFalse(self.client._fallback_to_cache(None))
        self.assertEqual(self.client.stale_hits, 0)
        self.assertEqual(self.client.cache_hits, 0)

    def test_cached_payload_is_used_on_network_failure(self):
        result = self.client._fallback_to_cache(({"ok": True}, "etag"))

        self.assertTrue(result)
        self.assertEqual(self.client.stale_hits, 1)
        self.assertEqual(self.client.cache_hits, 1)

    def test_counters_start_at_zero(self):
        self.assertEqual(self.client.requests_made, 0)
        self.assertEqual(self.client.stale_hits, 0)


if __name__ == "__main__":
    unittest.main()
