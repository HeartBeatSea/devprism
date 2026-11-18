"""离线快照客户端测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from devprism.fixture import FIXTURE_VERSION, FixtureClient


def make_fixture(**overrides):
    base = {
        "version": FIXTURE_VERSION,
        "source": {"repo": "o/r"},
        "repo": {"full_name": "o/r", "default_branch": "main"},
        "tree": [
            {"path": "src/main/java/A.java", "type": "blob", "size": 100},
            {"path": "README.md", "type": "blob", "size": 50},
        ],
        "tree_truncated": False,
        "commits": [
            {
                "sha": "a1",
                "author": {"login": "alice"},
                "commit": {"message": "feat: x", "author": {"date": "2026-05-02T00:00:00Z", "name": "Alice"}},
            },
            {
                "sha": "b2",
                "author": {"login": "bob"},
                "commit": {"message": "fix: y", "author": {"date": "2026-01-01T00:00:00Z", "name": "Bob"}},
            },
        ],
        "pulls": [
            {"number": 1, "created_at": "2026-05-03T00:00:00Z", "merged_at": "2026-05-04T00:00:00Z",
             "comments": 0, "review_count": 2},
            {"number": 2, "created_at": "2025-12-01T00:00:00Z", "merged_at": None,
             "comments": 0, "review_count": 0},
        ],
        "files": {"README.md": "# 标题"},
    }
    base.update(overrides)
    return base


class TestFixtureClient(unittest.TestCase):
    def _client(self, payload=None, raw=None) -> FixtureClient:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write(raw if raw is not None else json.dumps(payload or make_fixture(), ensure_ascii=False))
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return FixtureClient(Path(tmp.name))

    def test_repo_and_tree(self):
        client = self._client()
        self.assertEqual(client.repo("o", "r")["full_name"], "o/r")
        self.assertEqual(len(client.tree("o", "r")), 2)

    def test_commits_filtered_by_author_date(self):
        client = self._client()
        result = client.commits("o", "r", "2026-05-01T00:00:00Z")

        self.assertEqual(len(result), 1, "只应留下窗口内的提交")
        self.assertEqual(result[0]["sha"], "a1")
        self.assertTrue(result.window_complete)

    def test_pulls_filtered_by_created_at(self):
        client = self._client()
        result = client.pulls("o", "r", since="2026-05-01T00:00:00Z")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 1)

    def test_graphql_shape_reuses_pulls(self):
        client = self._client()
        self.assertEqual(len(client.pulls_via_graphql("o", "r")), 2)

    def test_file_text(self):
        client = self._client()
        self.assertEqual(client.file_text("o", "r", "README.md"), "# 标题")
        self.assertIsNone(client.file_text("o", "r", "不存在.md"))

    def test_raw_text_truncates(self):
        client = self._client()
        self.assertEqual(client.raw_text("o", "r", "README.md", limit=2), "# ")

    def test_no_network_stats(self):
        client = self._client()
        self.assertEqual(client.requests_made, 0)
        self.assertIsNone(client.remaining)

    def test_arbitrary_get_is_rejected(self):
        client = self._client()
        with self.assertRaises(RuntimeError):
            client.get("/repos/o/r")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            FixtureClient(Path("不存在的演示数据.json"))

    def test_version_mismatch_raises(self):
        with self.assertRaises(ValueError):
            self._client(raw=json.dumps({"version": 999}))

    def test_now_is_pinned_to_export_time(self):
        """窗口按「现在」往前推，演示若用真实时间，数字会随日期漂走。"""
        payload = make_fixture(source={"repo": "o/r", "exported_at": "2026-10-31T00:00:00Z"})

        client = self._client(payload)

        self.assertEqual(client.now().isoformat(), "2026-10-31T00:00:00+00:00")

    def test_now_falls_back_when_missing(self):
        client = self._client(make_fixture(source={"repo": "o/r"}))

        self.assertIsNotNone(client.now())


class TestBundledFixture(unittest.TestCase):
    """仓库自带的演示快照必须能直接跑通分析链路。"""

    def test_demo_file_is_usable(self):
        from devprism.config import ROOT
        from devprism.codegraph import build_graph
        from devprism.metrics import summarize
        from datetime import datetime, timezone

        path = ROOT / "demo" / "gson.json"
        self.assertTrue(path.is_file(), "缺少 demo/gson.json")

        client = FixtureClient(path)
        repo = client.repo("google", "gson")
        commits = client.commits("google", "gson", "2026-05-01T00:00:00Z")
        pulls = client.pulls_via_graphql("google", "gson")

        summary = summarize(repo, commits, pulls, 180, datetime(2026, 10, 24, tzinfo=timezone.utc))
        self.assertGreater(summary["commits"]["total"], 0)
        self.assertGreater(summary["pulls"]["created"], 0)

        graph = build_graph("google/gson", client.tree("google", "gson"))
        self.assertGreater(len(graph["nodes"]), 5, "演示快照应能画出星图")
        self.assertTrue(client.file_text("google", "gson", "pom.xml"))

    def test_demo_window_is_stable(self):
        """同一个快照反复跑，窗口和提交数必须一致，否则 CI 会随日期变红。"""
        from datetime import timedelta

        from devprism.config import ROOT

        client = FixtureClient(ROOT / "demo" / "gson.json")
        since_iso = (client.now() - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")

        first = client.commits("google", "gson", since_iso)
        second = client.commits("google", "gson", since_iso)

        self.assertEqual(len(first), len(second))
        self.assertGreater(len(first), 0, "演示快照里应该有窗口内的提交")


if __name__ == "__main__":
    unittest.main()
