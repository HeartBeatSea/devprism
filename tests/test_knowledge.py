"""知识库测试：切分、分词、检索。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devprism.knowledge import KnowledgeBase, split_markdown, tokenize


class TestTokenizer(unittest.TestCase):
    def test_chinese_uses_bigrams(self):
        tokens = tokenize("提交规范")
        self.assertIn("提交", tokens)
        self.assertIn("交规", tokens)
        self.assertIn("规范", tokens)

    def test_ascii_words_kept_whole(self):
        tokens = tokenize("Conventional Commits 格式")
        self.assertIn("conventional", tokens)
        self.assertIn("commits", tokens)

    def test_mixed_case_normalized(self):
        self.assertEqual(tokenize("GitHub"), tokenize("github"))


class TestSplitMarkdown(unittest.TestCase):
    def test_heading_path_is_kept(self):
        text = "# 总则\n\nintro\n\n## 格式\n\nbody\n\n### 细节\n\nmore\n"
        chunks = split_markdown(Path("a.md"), text)

        headings = [c.heading for c in chunks]
        self.assertIn("总则 / 格式", headings)
        self.assertIn("总则 / 格式 / 细节", headings)

    def test_empty_sections_are_dropped(self):
        text = "# 标题\n\n## 空章节\n\n## 有内容\n\n内容\n"
        chunks = split_markdown(Path("a.md"), text)

        self.assertTrue(all(chunk.text for chunk in chunks))


class TestKnowledgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "git.md").write_text(
            "# 提交规范\n\n## 类型\n\nfeat 新功能，fix 缺陷修复。\n", encoding="utf-8"
        )
        (root / "review.md").write_text(
            "# 评审清单\n\n## 时机\n\nPR 提交后 24 小时内给出反馈。\n", encoding="utf-8"
        )
        self.kb = KnowledgeBase(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stats(self):
        stats = self.kb.stats()
        self.assertEqual(stats["documents"], 2)
        self.assertGreaterEqual(stats["chunks"], 2)

    def test_search_hits_expected_document(self):
        hits = self.kb.search("提交类型 feat fix")

        self.assertTrue(hits)
        self.assertEqual(hits[0]["source"].split("#")[0], "git.md")

    def test_search_other_document(self):
        hits = self.kb.search("评审 反馈 时间")

        self.assertTrue(hits)
        self.assertEqual(hits[0]["source"].split("#")[0], "review.md")

    def test_top_k_limits_results(self):
        hits = self.kb.search("提交 评审", top_k=1)
        self.assertLessEqual(len(hits), 1)

    def test_unrelated_query_returns_empty(self):
        self.assertEqual(self.kb.search("量子计算 芯片 光刻"), [])

    def test_missing_directory_is_safe(self):
        empty = KnowledgeBase(Path(self.tmp.name) / "nope")
        self.assertEqual(empty.search("任意"), [])
        self.assertEqual(empty.stats()["chunks"], 0)


if __name__ == "__main__":
    unittest.main()
