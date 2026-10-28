"""指标引擎单元测试。

重点守护两类问题：
  1. 接口缺字段被误当成 0（曾导致「100% PR 未经评审」的假结论）；
  2. 日期/枚举映射错误（曾把星期索引当成星期名输出）。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from devprism import metrics

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def commit(message: str, when: datetime, login: str = "alice", name: str = "Alice") -> dict:
    return {
        "author": {"login": login},
        "commit": {
            "author": {"name": name, "date": when.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "message": message,
        },
    }


def pull(created: datetime, merged: datetime | None, **extra) -> dict:
    return {
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merged_at": merged.strftime("%Y-%m-%dT%H:%M:%SZ") if merged else None,
        "state": "closed" if merged else "open",
        "draft": False,
        **extra,
    }


class TestPullStats(unittest.TestCase):
    def test_missing_review_fields_are_unavailable_not_zero(self):
        """数据源不含 review_count 时，必须判定为「不可用」。"""
        since = NOW - timedelta(days=30)
        pulls = [pull(since + timedelta(days=1), since + timedelta(days=2)) for _ in range(5)]

        stats = metrics.pull_stats(pulls, since, NOW)

        self.assertFalse(stats["review_data_available"])
        self.assertIsNone(stats["unreviewed_merge_ratio"])
        self.assertIsNone(stats["avg_review_count"])

    def test_review_fields_present_are_computed(self):
        since = NOW - timedelta(days=30)
        pulls = [
            pull(since + timedelta(days=1), since + timedelta(days=2),
                 comments=0, review_count=0),
            pull(since + timedelta(days=1), since + timedelta(days=2),
                 comments=2, review_count=3),
        ]

        stats = metrics.pull_stats(pulls, since, NOW)

        self.assertTrue(stats["review_data_available"])
        self.assertEqual(stats["unreviewed_merge_ratio"], 0.5)
        self.assertEqual(stats["avg_review_count"], 1.5)

    def test_cycle_time_median(self):
        since = NOW - timedelta(days=30)
        pulls = [
            pull(since, since + timedelta(hours=10)),
            pull(since, since + timedelta(hours=20)),
            pull(since, since + timedelta(hours=60)),
        ]

        stats = metrics.pull_stats(pulls, since, NOW)

        self.assertEqual(stats["merged"], 3)
        self.assertEqual(stats["median_cycle_hours"], 20.0)

    def test_pulls_outside_window_are_excluded(self):
        since = NOW - timedelta(days=10)
        pulls = [pull(NOW - timedelta(days=40), NOW - timedelta(days=39))]

        stats = metrics.pull_stats(pulls, since, NOW)

        self.assertEqual(stats["created"], 0)


class TestCommitMetrics(unittest.TestCase):
    def test_busiest_weekday_is_name_not_index(self):
        """回归测试：曾把 weekday 索引（0=周一）当名字输出。"""
        commits = [
            commit("feat: a", datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)),  # 周三
            commit("feat: b", datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc)),
            commit("feat: c", datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)),  # 周一
        ]

        activity = metrics.commit_activity(commits, window_days=30)

        self.assertEqual(activity["busiest_weekday"], "周三")

    def test_conventional_commit_ratio(self):
        base = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        commits = [
            commit("feat(cli): add flag", base),
            commit("fix: crash on empty", base),
            commit("updated readme", base),
            commit("Merge branch 'main' into dev", base),
        ]

        stats = metrics.convention_stats(commits)

        # 合并提交不计入分母，因此是 2/3 而不是 2/4
        self.assertEqual(stats["considered"], 3)
        self.assertEqual(stats["compliant"], 2)
        self.assertAlmostEqual(stats["ratio"], 0.667, places=3)

    def test_revert_detection(self):
        base = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        commits = [
            commit('Revert "feat: add flag"', base),
            commit("fix: something\n\nThis reverts commit abc1234def.", base),
            commit("feat: normal", base),
        ]

        stats = metrics.revert_stats(commits)

        self.assertEqual(stats["count"], 2)

    def test_night_ratio(self):
        base = datetime(2026, 10, 20, 23, 30, tzinfo=timezone.utc)
        commits = [
            commit("feat: late", base),
            commit("feat: day", base.replace(hour=14)),
        ]

        activity = metrics.commit_activity(commits, window_days=7)

        self.assertEqual(activity["night_ratio"], 0.5)


class TestBusFactor(unittest.TestCase):
    def test_single_author(self):
        commits = [commit("feat: x", datetime(2026, 10, 1, tzinfo=timezone.utc), "alice")] * 5

        result = metrics.bus_factor(commits)

        self.assertEqual(result["bus_factor"], 1)
        self.assertEqual(result["top_author"], "alice")
        self.assertEqual(result["top_author_share"], 1.0)

    def test_distributed(self):
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        commits = (
            [commit("feat: x", base, "alice")] * 3
            + [commit("feat: y", base, "bob")] * 3
            + [commit("feat: z", base, "carol")] * 2
        )

        result = metrics.bus_factor(commits)

        # alice(3) + bob(3) = 6/8 = 75% >= 50%，故巴士因子为 2
        self.assertEqual(result["bus_factor"], 2)
        self.assertEqual(result["author_count"], 3)

    def test_falls_back_to_commit_name_when_login_missing(self):
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        entry = commit("feat: x", base, "ghost", "Ghost Writer")
        entry["author"] = None

        result = metrics.bus_factor([entry])

        self.assertEqual(result["top_author"], "Ghost Writer")


class TestRisks(unittest.TestCase):
    def _summary(self, **pull_overrides) -> dict:
        since = NOW - timedelta(days=30)
        pulls = [pull(since, since + timedelta(hours=5)) for _ in range(6)]
        base = {
            "commits": {"total": 20, "night_ratio": 0.0, "weekly_average": 3.0},
            "pulls": metrics.pull_stats(pulls, since, NOW),
            "bus_factor": {"bus_factor": 3, "author_count": 3, "top_author_share": 0.4,
                           "top_author": "alice"},
            "convention": {"considered": 20, "ratio": 0.9},
            "reverts": {"ratio": 0.0},
        }
        base["pulls"].update(pull_overrides)
        return base

    def test_no_false_review_risk_when_data_missing(self):
        """评审数据缺失时绝不能报「多数 PR 未经评审」这条高风险。"""
        summary = self._summary()
        self.assertFalse(summary["pulls"]["review_data_available"])

        titles = [item["title"] for item in metrics.evaluate_risks(summary)]

        self.assertNotIn("多数 PR 未经评审即合并", titles)

    def test_review_risk_when_data_available(self):
        summary = self._summary(unreviewed_merge_ratio=0.9, review_data_available=True)

        titles = [item["title"] for item in metrics.evaluate_risks(summary)]

        self.assertIn("多数 PR 未经评审即合并", titles)


if __name__ == "__main__":
    unittest.main()
