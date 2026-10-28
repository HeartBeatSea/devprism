"""报告渲染的边界测试。

真实仓库并不总是"有数据"：归档仓库、空仓库、只有 issue 没有 PR 的仓库，
都会让某些指标变成 None。渲染层必须扛得住，不能崩。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from devprism import metrics, report

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


class TestReportRender(unittest.TestCase):
    def test_zero_merged_prs_does_not_crash(self):
        """回归：octocat/Hello-World 实测崩溃——0 个已合并 PR 时 avg_review_count 为 None。"""
        pulls = [{
            "created_at": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "merged_at": None,
            "state": "open",
            "draft": False,
            "review_count": 0,
            "comments": 0,
        }]

        summary = metrics.summarize({"full_name": "o/r"}, [], pulls, 180, NOW)
        text = report.render(summary, {})

        self.assertIsNone(summary["pulls"]["avg_review_count"])
        self.assertIn("没有已合并的 PR", text)

    def test_completely_empty_repository(self):
        summary = metrics.summarize({"full_name": "o/empty"}, [], [], 180, NOW)
        text = report.render(summary, {})

        self.assertIn("研发效能报告", text)
        self.assertIn("不应用于个人绩效考核", text)

    def test_truncated_warning_appears(self):
        summary = metrics.summarize({"full_name": "o/r"}, [], [], 180, NOW)
        text = report.render(summary, {"truncated": ["/repos/o/r/commits"]})

        self.assertIn("数据被截断", text)

    def test_no_truncation_warning_when_clean(self):
        summary = metrics.summarize({"full_name": "o/r"}, [], [], 180, NOW)
        text = report.render(summary, {"truncated": []})

        self.assertNotIn("数据被截断", text)

    def test_java_section_rendered(self):
        summary = metrics.summarize({"full_name": "o/r"}, [], [], 180, NOW)
        summary["java"] = {
            "is_java": True, "build_tool": "maven", "java_version": "17",
            "main_java": 42, "test_java": 18, "test_ratio": 0.43,
            "module_count": 2, "modules": ["core", "web"],
            "dependencies": 24, "ci_files": 3, "has_wrapper": True,
            "kotlin_files": 0,
        }

        text = report.render(summary, {})

        self.assertIn("Java 工程结构", text)
        self.assertIn("maven", text)
        self.assertIn("主代码 **42**", text)
        self.assertIn("Java 版本 **17**", text)

    def test_non_java_repo_has_no_java_section(self):
        summary = metrics.summarize({"full_name": "o/r"}, [], [], 180, NOW)
        summary["java"] = {"is_java": False}

        self.assertNotIn("Java 工程结构", report.render(summary, {}))

    def test_java_error_is_disclosed(self):
        summary = metrics.summarize({"full_name": "o/r"}, [], [], 180, NOW)
        summary["java"] = {"is_java": False, "error": "网络超时"}

        self.assertIn("结构分析未完成", report.render(summary, {}))

    def test_unreadable_java_files_are_disclosed(self):
        summary = metrics.summarize({"full_name": "o/r"}, [], [], 180, NOW)
        summary["java"] = {
            "is_java": True, "build_tool": "maven", "main_java": 3, "test_java": 2,
            "ci_files": 0, "has_wrapper": False, "failed_files": ["pom.xml"],
        }

        text = report.render(summary, {})

        self.assertIn("未能读取", text)
        self.assertIn("pom.xml", text)


    def test_stale_cache_is_disclosed(self):
        summary = metrics.summarize({"full_name": "o/r"}, [], [], 180, NOW)
        text = report.render(summary, {"requests": 0, "cache_hits": 3, "stale": 2})

        self.assertIn("使用了旧缓存", text)
        self.assertIn("2 次", text)

    def test_no_stale_warning_when_clean(self):
        summary = metrics.summarize({"full_name": "o/r"}, [], [], 180, NOW)
        text = report.render(summary, {"requests": 1, "cache_hits": 0, "stale": 0})

        self.assertNotIn("使用了旧缓存", text)


if __name__ == "__main__":
    unittest.main()
