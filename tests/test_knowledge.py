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


class _FakeEmbedder:
    """把语义固定的几段文本映射到不同方向，用来验证混合检索确实生效。"""

    name = "fake:semantic"

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            for key, vector in self.table.items():
                if key in text:
                    out.append(vector)
                    break
            else:
                out.append([0.0, 0.0, 1.0])
        return out


class _BoomEmbedder:
    name = "fake:boom"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("向量服务挂了")


class TestHybridSearch(unittest.TestCase):
    def _kb(self, embedder=None):
        kb = KnowledgeBase(None, embedder=embedder)
        kb.add_document("quality.md", "# 质量控制\n\n每个功能都要有对应的自动化验证。")
        kb.add_document("release.md", "# 发布流程\n\n发布前打标签并通知值班同学。")
        return kb

    def test_bm25_alone_misses_synonym_query(self):
        kb = self._kb()

        self.assertEqual(kb.search("测试怎么保证"), [])
        self.assertEqual(kb.stats()["vector"], "off")

    def test_vector_recovers_synonym_query(self):
        embedder = _FakeEmbedder({
            "验证": [1.0, 0.0],
            "测试": [1.0, 0.0],   # 查询里的「测试」和文档里的「验证」方向一致
            "标签": [0.0, 1.0],
        })
        kb = self._kb(embedder)

        hits = kb.search("测试怎么保证")

        self.assertTrue(hits, "向量应该把语义相近的那段召回来")
        self.assertIn("质量控制", hits[0]["source"])
        self.assertEqual(kb.stats()["vector"], "ready")

    def test_vector_failure_falls_back_to_bm25(self):
        kb = self._kb(_BoomEmbedder())

        hits = kb.search("发布流程")

        self.assertTrue(hits, "向量挂了也要能用 BM25 出结果")
        self.assertIn("发布流程", hits[0]["source"])
        self.assertTrue(kb.stats()["vector"].startswith("failed:"))

    def test_attach_embedder_after_indexing(self):
        kb = self._kb()
        kb.attach_embedder(_FakeEmbedder({"测试": [1.0, 0.0], "验证": [1.0, 0.0]}))

        hits = kb.search("测试怎么保证")

        self.assertTrue(hits)
        self.assertEqual(kb.stats()["vector"], "ready")


class TestLongDocumentChunking(unittest.TestCase):
    """用户从 Word/PDF 粘出来的规范通常没有 Markdown 标题，
    整篇会变成一块，检索退化成「要么全中要么全不中」。"""

    def _plain(self, count=40):
        return "\n\n".join(
            f"第 {i} 条：提交前必须先跑通本地测试，具体做法见附录。" for i in range(1, count + 1)
        )

    def test_headingless_long_document_is_split(self):
        kb = KnowledgeBase(None)
        kb.add_document("考核规范.md", self._plain())

        self.assertGreater(len(kb.chunks), 1, "长文档应被切成多块")

    def test_citations_stay_distinguishable(self):
        kb = KnowledgeBase(None)
        kb.add_document("考核规范.md", self._plain())

        sources = {chunk.citation for chunk in kb.chunks}
        self.assertEqual(len(sources), len(kb.chunks), "每个块的引用路径必须唯一")

    def test_piece_title_uses_first_sentence(self):
        kb = KnowledgeBase(None)
        kb.add_document("考核规范.md", self._plain())

        self.assertTrue(
            any("第 1 条" in chunk.heading for chunk in kb.chunks),
            "没有真实标题时应该用首句当引用名",
        )

    def test_short_document_kept_whole(self):
        kb = KnowledgeBase(None)
        kb.add_document("短规范.md", "# 类型\n\n只允许 feat/fix/docs。")

        self.assertEqual(len(kb.chunks), 1)

    def test_long_document_with_heading_keeps_path(self):
        kb = KnowledgeBase(None)
        kb.add_document("规范.md", "# 提交要求\n\n" + self._plain())

        self.assertTrue(all("提交要求" in chunk.heading for chunk in kb.chunks))


class TestCustomNormsIsolation(unittest.TestCase):
    """knowledge/custom/ 下的规范只能显式选用，不能混进内置索引。"""

    def test_custom_directory_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "team.md").write_text("# 提交规范\n\n只允许 feat/fix/docs。", encoding="utf-8")
            custom = root / "custom"
            custom.mkdir()
            (custom / "别家规范.md").write_text("# 别家\n\n用中文写提交信息。", encoding="utf-8")

            kb = KnowledgeBase(root)

            docs = {chunk.doc for chunk in kb.chunks}
            self.assertIn("team.md", docs)
            self.assertNotIn("别家规范.md", docs)


if __name__ == "__main__":
    unittest.main()
