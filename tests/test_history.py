"""历史记录与自定义规范的存储测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devprism import knowledge
from devprism.history import HistoryStore


class TestHistoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoryStore(Path(self.tmp.name) / "history.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_and_group_by_repo(self):
        self.store.add(owner="a", name="one", kind="report", title="指标报告",
                       days=180, markdown="# a")
        self.store.add(owner="a", name="one", kind="agent", title="有什么风险",
                       days=90, markdown="# b", question="有什么风险")
        self.store.add(owner="b", name="two", kind="report", title="指标报告",
                       days=30, markdown="# c")

        groups = self.store.groups()

        self.assertEqual(len(groups), 2)
        first = next(g for g in groups if g["repo"] == "a/one")
        self.assertEqual(first["count"], 2)
        self.assertEqual(len(first["sessions"]), 2)
        # 组内按时间倒序，最新的一条在最前
        self.assertEqual(first["sessions"][0]["title"], "有什么风险")

    def test_get_returns_full_record(self):
        record_id = self.store.add(owner="a", name="one", kind="agent", title="问题",
                                   days=90, markdown="# 结论", question="问题",
                                   payload={"trace": [{"type": "thought"}]})

        record = self.store.get(record_id)

        self.assertIsNotNone(record)
        self.assertEqual(record["repo"], "a/one")
        self.assertEqual(record["markdown"], "# 结论")
        self.assertEqual(record["payload"]["trace"][0]["type"], "thought")

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("不存在的id"))

    def test_delete_one(self):
        record_id = self.store.add(owner="a", name="one", kind="report", title="t",
                                   days=30, markdown="")
        self.assertTrue(self.store.delete(record_id))
        self.assertIsNone(self.store.get(record_id))
        self.assertFalse(self.store.delete(record_id))

    def test_delete_repo_removes_all_sessions(self):
        for _ in range(3):
            self.store.add(owner="a", name="one", kind="report", title="t", days=30, markdown="")
        self.store.add(owner="b", name="two", kind="report", title="t", days=30, markdown="")

        removed = self.store.delete_repo("a", "one")

        self.assertEqual(removed, 3)
        self.assertEqual(len(self.store.groups()), 1)


class TestCustomConventions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = knowledge.CUSTOM_DIR
        knowledge.CUSTOM_DIR = Path(self.tmp.name) / "custom"

    def tearDown(self):
        knowledge.CUSTOM_DIR = self.original
        self.tmp.cleanup()

    def test_save_list_load_delete(self):
        slug = knowledge.save_custom("我的规范", "# 提交规范\n\n## 类型\n\n只允许 feat。")

        self.assertEqual(slug, "我的规范")
        self.assertEqual([i["slug"] for i in knowledge.list_custom()], ["我的规范"])

        base = knowledge.load_custom(slug)
        self.assertIsNotNone(base)
        hits = base.search("提交规范 类型")
        self.assertTrue(hits)

        self.assertTrue(knowledge.delete_custom(slug))
        self.assertEqual(knowledge.list_custom(), [])
        self.assertIsNone(knowledge.load_custom(slug))

    def test_empty_content_rejected(self):
        with self.assertRaises(ValueError):
            knowledge.save_custom("空", "   ")

    def test_unsafe_name_is_slugified(self):
        slug = knowledge.save_custom("../../etc/passwd", "# x")
        self.assertNotIn("/", slug)
        self.assertNotIn("..", slug)

    def test_missing_file_returns_none(self):
        self.assertIsNone(knowledge.load_custom("不存在"))


if __name__ == "__main__":
    unittest.main()
