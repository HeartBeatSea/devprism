"""历史记录与自定义规范的存储测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devprism import knowledge
from devprism.history import MAX_PER_REPO, HistoryStore


class TestHistoryDedupe(unittest.TestCase):
    """侧栏要收敛：同一个问题不该堆成好几条。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoryStore(Path(self.tmp.name) / "history.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_same_question_is_updated_not_duplicated(self):
        first = self.store.add("o", "r", "agent", "问题 A", 180, "旧答案", question="问题 A")
        second = self.store.add("o", "r", "agent", "问题 A", 180, "新答案", question="问题 A")

        self.assertEqual(first, second, "同一问题应复用同一条记录")
        groups = self.store.groups()
        self.assertEqual(groups[0]["count"], 1)
        self.assertEqual(self.store.get(first)["markdown"], "新答案", "内容要更新成最新的")

    def test_different_questions_are_kept(self):
        self.store.add("o", "r", "agent", "Q1", 180, "a", question="Q1")
        self.store.add("o", "r", "agent", "Q2", 180, "b", question="Q2")

        self.assertEqual(self.store.groups()[0]["count"], 2)

    def test_per_repo_cap(self):
        for i in range(MAX_PER_REPO + 6):
            self.store.add("o", "r", "agent", f"Q{i}", 180, "x", question=f"Q{i}")

        self.assertLessEqual(self.store.groups()[0]["count"], MAX_PER_REPO)

    def test_cap_is_per_repo(self):
        for i in range(MAX_PER_REPO + 4):
            self.store.add("o", "r1", "agent", f"Q{i}", 180, "x", question=f"Q{i}")
        for i in range(3):
            self.store.add("o", "r2", "agent", f"Q{i}", 180, "y", question=f"Q{i}")

        counts = {g["repo"]: g["count"] for g in self.store.groups()}
        self.assertLessEqual(counts["o/r1"], MAX_PER_REPO)
        self.assertEqual(counts["o/r2"], 3, "别的仓库不该被连带裁掉")

    def test_code_records_live_alongside_agent_records(self):
        self.store.add("o", "r", "agent", "Q", 180, "x", question="Q")
        self.store.add("o", "r", "code", "核心代码", 0, "", payload={"focus": "code"})

        kinds = {s["kind"] for s in self.store.groups()[0]["sessions"]}
        self.assertEqual(kinds, {"agent", "code"})

    def test_delete_single_record(self):
        record_id = self.store.add("o", "r", "agent", "Q", 180, "x", question="Q")

        self.assertTrue(self.store.delete(record_id))
        self.assertEqual(self.store.groups(), [])


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
        # 用不同标题，避免被「同一问题只留一条」的规则合并掉
        for i in range(3):
            self.store.add(owner="a", name="one", kind="report", title=f"t{i}", days=30, markdown="")
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
