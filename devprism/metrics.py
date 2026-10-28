"""指标计算 —— 纯函数，不依赖网络与大模型，可独立单测。

**指标口径（面试会被追问，必须写死）**

时间窗口：默认取最近 180 天。所有"近 N 天"一律以 UTC 计算。

* 周均提交       = 窗口内提交总数 ÷ (窗口天数 ÷ 7)
* 夜间提交占比   = 提交时间落在 22:00–05:59 的比例（含跨零点）
* 规范提交占比   = 符合 Conventional Commits 的提交 ÷ 总提交
                   （合并提交按约定不计入分母，见 is_merge_commit）
* Revert 占比    = 信息以 "Revert" 开头、或正文含 "This reverts commit" 的提交占比
* PR 合并周期    = merged_at − created_at，取中位数与 P90，仅统计窗口内已合并的 PR
* 无评审合并占比 = 已合并 PR 中 comments + reviews 均为 0 的比例
* 巴士因子       = 按提交量降序累加作者，累计占比首次 ≥ 50% 时的作者数
                   （= 1 意味着单点依赖）

风险等级阈值集中在 RISK_RULES，改动需同步更新本文档。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

CONVENTIONAL_RE = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([^)]+\))?!?:\s+\S",
    re.IGNORECASE,
)
REVERT_RE = re.compile(r"^(?:Revert\b)|This reverts commit [0-9a-f]{7,}", re.IGNORECASE | re.MULTILINE)
MERGE_RE = re.compile(r"^Merge (?:branch|pull request|remote-tracking)", re.IGNORECASE)

NIGHT_HOURS = frozenset(list(range(22, 24)) + list(range(0, 6)))


# ---------------------------------------------------------------- 工具

def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return float(ordered[index])


def _commit_message(commit: dict) -> str:
    return (commit.get("commit", {}).get("message") or "").strip()


def _commit_time(commit: dict) -> datetime | None:
    return parse_time(commit.get("commit", {}).get("author", {}).get("date"))


def is_merge_commit(message: str) -> bool:
    return bool(MERGE_RE.match(message))


# ---------------------------------------------------------------- 提交

def commit_activity(
    commits: Iterable[dict],
    window_days: int,
    since: datetime | None = None,
    window_complete: bool = True,
    until: datetime | None = None,
) -> dict[str, Any]:
    """提交节奏；完整性只能由采集层传入，不能从样本时间反推。"""
    commits = list(commits)

    if since is not None:
        commits = [
            c for c in commits
            if (t := _commit_time(c)) is not None and t >= since
            and (until is None or t <= until)
        ]

    by_weekday: Counter[int] = Counter()
    by_hour: Counter[int] = Counter()
    night = 0
    dated = 0
    timestamps: list[datetime] = []

    for commit in commits:
        moment = _commit_time(commit)
        if moment is None:
            continue
        dated += 1
        timestamps.append(moment)
        by_weekday[moment.weekday()] += 1
        by_hour[moment.hour] += 1
        if moment.hour in NIGHT_HOURS:
            night += 1

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    busiest = by_weekday.most_common(1)

    covered_days = 0.0
    if timestamps:
        timestamps.sort()
        covered_days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400.0

    return {
        "total": len(commits),
        "dated": dated,
        # 窗口没盖满时绝不外推「周均」：2.4 天的样本乘以 7 只会造出一个假数字。
        "weekly_average": round(len(commits) / (window_days / 7.0), 1)
        if window_complete else None,
        "daily_average": round(len(commits) / covered_days, 1)
        if timestamps and covered_days > 0 else None,
        "night_ratio": round(night / dated, 3) if dated else 0.0,
        "by_weekday": {weekday_names[i]: by_weekday.get(i, 0) for i in range(7)},
        "busiest_weekday": weekday_names[busiest[0][0]] if busiest else None,
        "by_hour": {str(h): by_hour.get(h, 0) for h in range(24)},
        "covered_days": round(covered_days, 1),
        "covers_window": window_complete,
    }


def convention_stats(commits: Iterable[dict], sample_size: int = 5) -> dict[str, Any]:
    considered = 0
    compliant = 0
    violations: list[str] = []

    for commit in commits:
        message = _commit_message(commit)
        if not message or is_merge_commit(message):
            continue
        considered += 1
        subject = message.splitlines()[0][:100]
        if CONVENTIONAL_RE.match(message):
            compliant += 1
        elif len(violations) < sample_size:
            violations.append(subject)

    return {
        "considered": considered,
        "compliant": compliant,
        "ratio": round(compliant / considered, 3) if considered else 0.0,
        "violations": violations,
    }


def revert_stats(commits: Iterable[dict]) -> dict[str, Any]:
    commits = list(commits)
    reverts = sum(1 for c in commits if REVERT_RE.search(_commit_message(c)))
    return {
        "count": reverts,
        "ratio": round(reverts / len(commits), 3) if commits else 0.0,
    }


# ---------------------------------------------------------------- PR

def pull_stats(
    pulls: Iterable[dict],
    since: datetime,
    now: datetime | None = None,
    window_complete: bool = True,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    in_window: list[dict] = []

    for pr in pulls:
        created = parse_time(pr.get("created_at"))
        if created and since <= created <= now:
            in_window.append(pr)

    merged = [pr for pr in in_window if pr.get("merged_at")]
    cycles: list[float] = []

    for pr in merged:
        created = parse_time(pr.get("created_at"))
        done = parse_time(pr.get("merged_at"))
        if created and done:
            cycles.append((done - created).total_seconds() / 3600.0)

    # ⚠️ 评审数据只有 GraphQL 才拿得到。REST 的 /pulls 列表接口不返回
    # comments / reviews 字段，逐 PR 查询详情又要消耗 N 次配额。
    # 缺字段时必须判定为「不可用」，绝不能当作 0 —— 否则会得出
    # 「100% 的 PR 未经评审」这种看起来确定、实则完全错误的结论。
    review_data_available = any("review_count" in pr for pr in in_window)

    avg_review_count: float | None = None
    unreviewed_ratio: float | None = None
    if review_data_available and merged:
        total_reviews = sum(int(pr.get("review_count") or 0) for pr in merged)
        avg_review_count = round(total_reviews / len(merged), 2)
        unreviewed = sum(
            1
            for pr in merged
            if int(pr.get("comments") or 0) == 0 and int(pr.get("review_count") or 0) == 0
        )
        unreviewed_ratio = round(unreviewed / len(merged), 3)

    span_days = max((now - since).days, 1)

    return {
        "created": len(in_window),
        "merged": len(merged),
        "open": sum(1 for pr in in_window if not pr.get("merged_at") and pr.get("state") == "open"),
        "draft": sum(1 for pr in in_window if pr.get("draft")),
        "median_cycle_hours": round(median(cycles), 1) if cycles else None,
        "p90_cycle_hours": round(_percentile(cycles, 0.9), 1) if cycles else None,
        "avg_review_count": avg_review_count,
        "unreviewed_merge_ratio": unreviewed_ratio,
        "review_data_available": review_data_available,
        "weekly_merge_rate": round(len(merged) / max(span_days / 7.0, 1.0), 1)
        if window_complete else None,
        "covers_window": window_complete,
    }


# ---------------------------------------------------------------- 结构风险

def bus_factor(commits: Iterable[dict]) -> dict[str, Any]:
    """按窗口内提交量计算巴士因子。

    刻意不用 /stats/contributors：它返回全量周粒度数据（实测 2 MB+），
    既费配额又易被截断；用已到手的提交列表算，窗口口径也更准确。
    """
    counts: Counter[str] = Counter()
    for commit in commits:
        login = (commit.get("author") or {}).get("login")
        name = ((commit.get("commit") or {}).get("author") or {}).get("name")
        counts[login or name or "unknown"] += 1

    totals = counts.most_common()
    grand_total = sum(counts.values())
    if grand_total == 0:
        return {"bus_factor": 0, "top_author_share": 0.0, "authors": [], "author_count": 0}

    cumulative = 0
    factor = 0
    for index, (_, count) in enumerate(totals, start=1):
        cumulative += count
        if cumulative / grand_total >= 0.5:
            factor = index
            break

    return {
        "bus_factor": factor or len(totals),
        "top_author_share": round(totals[0][1] / grand_total, 3),
        "top_author": totals[0][0],
        "author_count": len(totals),
        "authors": [{"login": login, "commits": count} for login, count in totals[:10]],
    }


# ---------------------------------------------------------------- 汇总与风险

def evaluate_risks(summary: dict[str, Any]) -> list[dict[str, str]]:
    """只根据观察样本给出有边界的风险提示，不把截断样本外推为全窗口结论。"""
    findings: list[dict[str, str]] = []
    commits = summary["commits"]
    pulls = summary["pulls"]
    bus = summary["bus_factor"]
    convention = summary["convention"]
    reverts = summary["reverts"]
    total = commits.get("total", 0)
    commits_complete = commits.get("covers_window", True) is not False
    pulls_complete = pulls.get("covers_window", True) is not False

    if total == 0:
        findings.append({
            "level": "提示",
            "title": "窗口内无提交数据",
            "detail": "无法评估交付节奏与人员结构；PR 协作指标仍按其实际样本评估。",
        })

    if not commits_complete:
        findings.append({
            "level": "提示",
            "title": "提交数据只覆盖了窗口的一部分",
            "detail": f"翻页上限使样本只覆盖约 {commits.get('covered_days')} 天；"
                      "周均提交不外推，以下提交结论仅适用于观察样本。",
        })
    if not pulls_complete:
        findings.append({
            "level": "提示",
            "title": "PR 数据只覆盖了窗口的一部分",
            "detail": "达到翻页上限且尚未越过窗口起点，周合并率不外推，"
                      "PR 结论仅适用于已获取的观察样本。",
        })

    if total >= 10 and bus.get("bus_factor") == 1 and bus.get("author_count", 0) > 1:
        findings.append({
            "level": "高" if commits_complete else "提示",
            "title": "关键人依赖（巴士因子 = 1）",
            "detail": f"观察样本中 {bus.get('top_author')} 贡献了 "
                      f"{bus.get('top_author_share', 0) * 100:.0f}% 的提交，需关注知识共享。",
        })
    elif total >= 10 and bus.get("bus_factor") == 1:
        findings.append({
            "level": "中" if commits_complete else "提示",
            "title": "单人仓库",
            "detail": "观察样本中仅一名贡献者；这不等同于团队存在人员风险。",
        })
    elif 0 < total < 10 and bus.get("bus_factor") == 1:
        findings.append({
            "level": "提示",
            "title": "贡献者样本较小",
            "detail": f"仅观察到 {total} 条提交，不能据此判定单人或关键人风险。",
        })

    if commits.get("dated", total) >= 10 and commits.get("night_ratio", 0) > 0.25:
        findings.append({
            "level": "中" if commits_complete else "提示",
            "title": "夜间（UTC）提交占比偏高",
            "detail": f"观察样本中 {commits['night_ratio'] * 100:.0f}% 的提交发生在 "
                      "UTC 22:00–05:59；时区和协作分布未知，不能据此推断加班。",
        })

    if convention.get("considered", 0) >= 10 and convention.get("ratio", 1) < 0.5:
        findings.append({
            "level": "中",
            "title": "提交规范执行率低",
            "detail": f"观察样本中仅 {convention['ratio'] * 100:.0f}% 的提交符合 "
                      "Conventional Commits，影响变更日志自动生成与追溯。",
        })

    median_cycle = pulls.get("median_cycle_hours")
    if median_cycle is not None and median_cycle > 72:
        findings.append({
            "level": "中",
            "title": "PR 合并周期偏长",
            "detail": f"观察样本的中位合并周期为 {median_cycle:.0f} 小时，"
                      "审查环节可能是瓶颈。",
        })

    unreviewed_ratio = pulls.get("unreviewed_merge_ratio")
    if unreviewed_ratio is not None and unreviewed_ratio > 0.5 and pulls.get("merged", 0) >= 5:
        findings.append({
            "level": "高" if pulls_complete else "提示",
            "title": "多数 PR 未经评审即合并" if pulls_complete else "观察样本中多数 PR 未经评审",
            "detail": f"已观察的合并 PR 中 {unreviewed_ratio * 100:.0f}% 没有评论或评审记录；"
                      + ("代码质量缺乏制衡。" if pulls_complete else "样本不完整，不外推到整个窗口。"),
        })

    if total >= 10 and reverts.get("ratio", 0) > 0.05:
        findings.append({
            "level": "中" if commits_complete else "提示",
            "title": "Revert 比例偏高",
            "detail": f"观察样本中回滚提交占 {reverts['ratio'] * 100:.1f}%，"
                      "是变更失败率的近似信号。",
        })

    if not findings:
        findings.append({
            "level": "低",
            "title": "未发现显著风险信号",
            "detail": "完整窗口内的已采集指标均处于常规区间。",
        })
    return findings


def summarize(
    repo_meta: dict[str, Any],
    commits: list[dict],
    pulls: list[dict],
    window_days: int,
    now: datetime | None = None,
    *,
    since: datetime | None = None,
    commits_complete: bool = True,
    pulls_complete: bool = True,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = since or (now - timedelta(days=window_days))

    in_window = [
        commit for commit in commits
        if (stamp := _commit_time(commit)) is not None and since <= stamp <= now
    ]
    summary: dict[str, Any] = {
        "repo": {
            "full_name": repo_meta.get("full_name"),
            "description": repo_meta.get("description"),
            "language": repo_meta.get("language"),
            "stars": repo_meta.get("stargazers_count"),
            "forks": repo_meta.get("forks_count"),
            "open_issues": repo_meta.get("open_issues_count"),
        },
        "window_days": window_days,
        "since": since.isoformat(),
        "until": now.isoformat(),
        "generated_at": now.isoformat(),
        "commits": commit_activity(
            in_window, window_days, since, window_complete=commits_complete, until=now
        ),
        "convention": convention_stats(in_window),
        "reverts": revert_stats(in_window),
        "pulls": pull_stats(pulls, since, now, window_complete=pulls_complete),
        "bus_factor": bus_factor(in_window),
    }
    summary["risks"] = evaluate_risks(summary)
    return summary
