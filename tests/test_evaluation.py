"""评测框架测试：用假知识库和假 Agent，不触网也不花钱。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from devprism import evaluation


class FakeKB:
    """按问题返回固定结果，方便验证指标算得对不对。"""

    def __init__(self, mapping):
        self.mapping = mapping

    def search(self, question, top_k=3):
        return [{"source": source} for source in self.mapping.get(question, [])][:top_k]


def cases(*items):
    return [evaluation.Case(id=i, question=q, expect_sources=s, expect_tools=t) for i, q, s, t in items]


class TestLoadCases(unittest.TestCase):
    def test_loads_and_skips_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps({"cases": [
                {"id": "a", "question": "问题一", "expect_sources": ["x#y"]},
                {"id": "b", "question": "   "},
            ]}, ensure_ascii=False), encoding="utf-8")

            loaded = evaluation.load_cases(path)

            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].id, "a")


class TestRetrievalEval(unittest.TestCase):
    def test_recall_and_mrr(self):
        data = cases(
            ("a", "问一", ["doc#1"], []),
            ("b", "问二", ["doc#2"], []),
            ("c", "问三", ["doc#3"], []),
        )
        kb = FakeKB({
            "问一": ["doc#1", "x", "y"],   # 第 1 位
            "问二": ["x", "doc#2", "y"],   # 第 2 位
            "问三": ["x", "y", "z"],       # 未命中
        })

        result = evaluation.evaluate_retrieval(kb, data, k=3)

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["hits"], 2)
        self.assertAlmostEqual(result["recall"], 0.667, places=3)
        self.assertAlmostEqual(result["mrr"], round((1 + 0.5 + 0) / 3, 3), places=3)
        self.assertEqual(len(result["misses"]), 1)
        self.assertEqual(result["misses"][0]["id"], "c")

    def test_cases_without_sources_are_skipped(self):
        data = cases(("a", "问一", [], ["commit_activity"]))
        result = evaluation.evaluate_retrieval(FakeKB({}), data)

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["recall"], 0.0)

    def test_multiple_acceptable_sources(self):
        data = cases(("a", "问一", ["doc#1", "doc#2"], []))
        kb = FakeKB({"问一": ["doc#2"]})

        self.assertEqual(evaluation.evaluate_retrieval(kb, data)["hits"], 1)


class TestToolEval(unittest.TestCase):
    def test_accuracy_and_precision(self):
        data = cases(
            ("a", "问一", [], ["pull_health"]),
            ("b", "问二", [], ["commit_convention", "search_knowledge"]),
        )
        called = {
            "问一": ["pull_health"],
            "问二": ["commit_convention"],   # 少调一个
        }

        result = evaluation.evaluate_tools(lambda q: called[q], data)

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["correct"], 1)
        self.assertAlmostEqual(result["accuracy"], 0.5)
        self.assertEqual(result["cases"][1]["missing"], ["search_knowledge"])

    def test_helper_tools_do_not_count_as_extra(self):
        data = cases(("a", "问一", [], ["pull_health"]))
        result = evaluation.evaluate_tools(
            lambda q: ["list_skills", "load_skill", "pull_health"], data
        )

        self.assertTrue(result["cases"][0]["ok"])
        self.assertAlmostEqual(result["tool_precision"], 1.0, places=3)

    def test_runner_failure_is_tolerated(self):
        data = cases(("a", "问一", [], ["pull_health"]))

        def boom(question):
            raise RuntimeError("模型挂了")

        with self.assertRaises(RuntimeError):
            evaluation.evaluate_tools(boom, data)

    def test_limit(self):
        data = cases(*[(f"c{i}", f"问{i}", [], ["t"]) for i in range(5)])
        result = evaluation.evaluate_tools(lambda q: ["t"], data, limit=2)

        self.assertEqual(result["total"], 2)


class TestFormatting(unittest.TestCase):
    def test_retrieval_report_mentions_numbers(self):
        result = evaluation.evaluate_retrieval(
            FakeKB({"问一": ["doc#1"]}), cases(("a", "问一", ["doc#1"], []))
        )
        text = evaluation.format_retrieval(result)

        self.assertIn("recall@3", text)
        self.assertIn("100.0%", text)

    def test_tools_report_lists_misses(self):
        result = evaluation.evaluate_tools(
            lambda q: [], cases(("a", "问一", [], ["pull_health"]))
        )
        text = evaluation.format_tools(result)

        self.assertIn("准确率", text)
        self.assertIn("pull_health", text)


class TestProjectCases(unittest.TestCase):
    """仓库自带的用例集必须能解析，且引用的片段真实存在。"""

    def test_cases_file_is_valid(self):
        from devprism.config import ROOT
        from devprism.knowledge import KnowledgeBase

        loaded = evaluation.load_cases(ROOT / "eval" / "cases.json")
        self.assertGreaterEqual(len(loaded), 25, "用例太少，评出来的数字没有意义")

        kb = KnowledgeBase(ROOT / "knowledge")
        known = {chunk.citation for chunk in kb.chunks}
        for case in loaded:
            for source in case.expect_sources:
                with self.subTest(case=case.id, source=source):
                    self.assertIn(source, known, "期望的引用在知识库里不存在")

    def test_every_case_expects_something(self):
        from devprism.config import ROOT

        for case in evaluation.load_cases(ROOT / "eval" / "cases.json"):
            self.assertTrue(case.expect_sources or case.expect_tools,
                            f"{case.id} 没有任何期望值，等于白测")


if __name__ == "__main__":
    unittest.main()
