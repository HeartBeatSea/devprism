"""把指标汇总渲染成 Markdown 报告。

报告强制包含「方法与口径」一节：指标定义不写清楚，结论就不可辩护。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _fmt_int(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def _parse_iso(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return "-"


def _grade(condition: bool | None, warn: str = "关注", ok: str = "良好") -> str:
    """condition 为 None 表示「没有数据」，此时绝不能给正面或负面评价。"""
    if condition is None:
        return "无数据"
    return warn if condition else ok


def render(
    summary: dict[str, Any],
    source: dict[str, Any] | None = None,
    narrative: str | None = None,
) -> str:
    if source is None:
        source = summary.get("source")
    repo = summary["repo"]
    truncated = list((source or {}).get("truncated") or [])
    coverage = summary.get("coverage") or {}
    commits = summary["commits"]
    pulls = summary["pulls"]
    bus = summary["bus_factor"]
    convention = summary["convention"]
    reverts = summary["reverts"]

    lines: list[str] = []
    add = lines.append

    add(f"# 研发效能报告 · {repo['full_name']}")
    add("")
    add(f"> 分析窗口：最近 **{summary['window_days']} 天**　|　"
        f"生成时间：{_parse_iso(summary['generated_at'])}")
    if repo.get("description"):
        add(f"> {repo['description']}")
    if summary.get("snapshot_id"):
        add(f"> 快照：`{summary['snapshot_id']}`")
    add("")
    if narrative:
        add("## 分析叙述")
        add("")
        add(narrative)
        add("")
    if coverage:
        add("## 数据覆盖")
        add("")
        for label, key in (("提交", "commits"), ("PR", "pulls")):
            item = coverage.get(key) or {}
            scope = "完整窗口" if item.get("complete", True) else "观察样本（窗口不完整）"
            add(f"- {label}：{scope}；获取 {item.get('fetched', 0)} 条，"
                f"{item.get('pages', 0)} 页，停止原因 `{item.get('stop_reason', 'unknown')}`。")
        add("")

    # ---------------- 概览
    has_commits = commits["total"] > 0
    has_merged = pulls["merged"] > 0

    cycle_hours = pulls["median_cycle_hours"] if has_merged else None
    bus_value = bus["bus_factor"] if has_commits else None
    conv_value = convention["ratio"] if convention["considered"] else None
    revert_value = reverts["ratio"] if has_commits else None

    add("## 一、概览")
    add("")
    add("| 指标 | 数值 | 评价 |")
    add("| --- | --- | --- |")
    if not has_commits:
        commit_note = "窗口内无提交"
    elif commits.get("weekly_average") is None:
        commit_note = f"仅覆盖 {commits['covered_days']} 天"
    else:
        commit_note = f"周均 {commits['weekly_average']}"
    add(f"| 提交总数 | {_fmt_int(commits['total'])} | {commit_note} |")
    merge_rate = pulls.get("weekly_merge_rate")
    if has_merged:
        merge_note = (f"周均 {merge_rate}" if merge_rate is not None else "样本截断，周合并不外推")
        merge_note += f"，未合并 {pulls['open']}"
    else:
        merge_note = f"未合并 {pulls['open']}，窗口内无合并"
    add(f"| 合并 PR | {_fmt_int(pulls['merged'])} | {merge_note} |")
    add(f"| PR 合并周期（中位） | "
        + (f"{cycle_hours:.1f} 小时" if cycle_hours is not None else "—")
        + f" | {_grade(None if cycle_hours is None else cycle_hours > 72)} |")
    add(f"| 巴士因子 | "
        + (f"{bus_value} 人" if bus_value is not None else "—")
        + f" | {_grade(None if bus_value is None else bus_value == 1, '高风险', '正常')} |")
    add(f"| 规范提交占比 | "
        + (f"{conv_value * 100:.0f}%" if conv_value is not None else "—")
        + f" | {_grade(None if conv_value is None else conv_value < 0.5)} |")
    add(f"| Revert 占比 | "
        + (f"{revert_value * 100:.1f}%" if revert_value is not None else "—")
        + f" | {_grade(None if revert_value is None else revert_value > 0.05)} |")
    add("")

    # ---------------- 交付节奏
    add("## 二、交付节奏")
    add("")
    if has_commits:
        if commits.get("weekly_average") is not None:
            add(f"- 周均提交 **{commits['weekly_average']}** 次；"
                f"最活跃的是 **{commits['busiest_weekday'] or '—'}**。")
        else:
            add(f"- 日均提交 **{commits['daily_average']}** 次"
                f"（仅覆盖 {commits['covered_days']} 天）；"
                f"最活跃的是 **{commits['busiest_weekday'] or '—'}**。")
            add("- ⚠ 数据覆盖不足：翻页上限导致只取到最近 "
                f"**{commits['covered_days']}** 天的提交（窗口 {summary['window_days']} 天未装满），"
                "因此不给周均，也不宜与其它仓库直接横比。")
        weekday_line = "　".join(f"{day} {count}" for day, count in commits["by_weekday"].items())
        add(f"- 工作日分布：{weekday_line}")
        add(f"- 夜间（UTC 22:00–05:59）提交占比 **{commits['night_ratio'] * 100:.0f}%**；"
            "该时点分布不能直接推断加班。")
    else:
        add("窗口内没有提交记录，本节无数据。")
    add("")

    # ---------------- 协作健康
    add("## 三、协作健康")
    add("")
    add(f"- 窗口内新建 PR **{pulls['created']}** 个，已合并 **{pulls['merged']}** 个，"
        f"当前未合并 **{pulls['open']}** 个。")
    if pulls.get("median_cycle_hours") is not None:
        add(f"- 合并周期：中位 **{pulls['median_cycle_hours']:.1f}** 小时，"
            f"P90 **{pulls['p90_cycle_hours']:.1f}** 小时。")
    else:
        add("- 合并周期：**无数据**。窗口内没有已合并的 PR，无法计算。")
    if pulls.get("avg_review_count") is not None:
        add(f"- 每个已合并 PR 平均收到 **{pulls['avg_review_count']:.1f}** 条评审记录。")
        add(f"- 无任何评审记录即合并的 PR 占 **{pulls['unreviewed_merge_ratio'] * 100:.0f}%**。")
    elif pulls.get("review_data_available"):
        add("- 窗口内没有已合并的 PR，评审强度无从计算。")
    else:
        add("- 评审强度：**不可用**。当前数据源未包含评审记录"
            "（REST 列表接口不返回该字段，配置 GITHUB_TOKEN 后改用 GraphQL 即可获得）。")
    add("")

    # ---------------- 人员结构
    add("## 四、人员结构")
    add("")
    if has_commits:
        add(f"- 窗口内活跃贡献者 **{bus['author_count']}** 人，"
            f"提交量最高者占 **{bus['top_author_share'] * 100:.0f}%**。")
        add(f"- 巴士因子 **{bus['bus_factor']}**："
            f"{'存在关键人依赖风险。' if bus['bus_factor'] == 1 else '人员分布可接受。'}")
    else:
        add("窗口内没有提交记录，无法计算贡献者结构。")
    if bus["authors"]:
        add("")
        add("| 贡献者 | 提交数 |")
        add("| --- | --- |")
        for author in bus["authors"][:5]:
            add(f"| {author['login']} | {_fmt_int(author['commits'])} |")
    add("")

    # ---------------- Java 工程结构
    java = summary.get("java") or {}
    if java.get("is_java"):
        add("## 五、Java 工程结构")
        add("")
        build = java.get("build_tool") or "未识别"
        add(f"- 构建工具：**{build}**"
            + (f"，Java 版本 **{java['java_version']}**" if java.get("java_version") else ""))
        add(f"- 类文件：主代码 **{java.get('main_java', 0)}** 个，"
            f"测试 **{java.get('test_java', 0)}** 个"
            + (f"（测试/主代码 = {java['test_ratio']}）" if java.get("test_ratio") is not None
               else "（无法计算比例：未识别 src/main/java 结构）"))
        if java.get("module_count"):
            modules = "、".join(java.get("modules", [])[:6])
            add(f"- 模块：**{java['module_count']}** 个子模块（{modules}）")
        if java.get("dependencies") is not None:
            if java.get("module_count"):
                add(f"- 根构建文件声明依赖：**{java['dependencies']}** 个"
                    "（多模块项目，子模块的依赖未计入）")
            else:
                add(f"- 声明依赖：**{java['dependencies']}** 个")
        if java.get("kotlin_files"):
            add(f"- 同时包含 Kotlin 文件 **{java['kotlin_files']}** 个")
        add(f"- CI 配置：**{java.get('ci_files', 0)}** 个文件；"
            f"构建包装器：{'有' if java.get('has_wrapper') else '无'}")
        areas = java.get("areas") or {}
        if areas:
            total_bytes = sum(a.get("bytes", 0) for a in areas.values()) or 1
            parts = [
                f"{name} {data.get('files', 0)} 个文件（{data.get('bytes', 0) / total_bytes:.0%}）"
                for name, data in list(areas.items())[:5]
            ]
            add(f"- 区域构成：{'、'.join(parts)}")
        if java.get("tree_truncated"):
            add("- ⚠ 目录树被 GitHub 截断，以上文件计数是下限而非精确值")
        if java.get("failed_files"):
            add(f"- ⚠ 以下关键文件未能读取，依赖与版本信息可能缺失："
                f"{'、'.join(java['failed_files'][:6])}")
        add("")
    elif java.get("error"):
        add("## 五、Java 工程结构")
        add("")
        add(f"结构分析未完成：{java['error']}")
        add("")

    # ---------------- 风险
    add("## 六、风险提示")
    add("")
    for item in summary["risks"]:
        add(f"**[{item['level']}] {item['title']}**")
        add("")
        add(f"{item['detail']}")
        add("")

    # ---------------- 方法与口径
    add("## 七、方法与口径")
    add("")
    add("| 指标 | 定义 |")
    add("| --- | --- |")
    add("| 周均提交 | 窗口内提交总数 ÷（窗口天数 ÷ 7） |")
    add("| PR 合并周期 | `merged_at − created_at`，仅统计窗口内已合并的 PR |")
    add("| 规范提交占比 | 符合 Conventional Commits 的提交 ÷ 总提交（合并提交不计入分母） |")
    add("| Revert 占比 | 以 `Revert` 开头或正文含 `This reverts commit` 的提交占比 |")
    add("| 巴士因子 | 按提交量降序累加作者，累计占比首次 ≥ 50% 时的作者数 |")
    if pulls.get("avg_review_count") is not None:
        add("| 无评审合并 | 已合并 PR 中 `comments + reviews` 均为 0 的比例 |")
    elif pulls.get("review_data_available"):
        add("| 评审强度 | 未计算（窗口内无已合并 PR） |")
    else:
        add("| 评审强度 | 不可用（当前数据源未包含评审记录） |")
    add("")
    add("**已知局限**：本报告基于 Git 提交与 PR 元数据，不含 CI 失败率、缺陷数据与代码复杂度，"
        "因此 Revert 占比只是「变更失败率」的近似替代。")
    if not pulls.get("review_data_available"):
        add("")
        add("**评审数据缺失**：当前数据源未包含 PR 评审记录（REST 列表接口不返回该字段），"
            "因此「评审强度」判为不可用，而非 0。")
    if truncated:
        add("")
        add(f"**数据被截断**：`{('`、`'.join(truncated))}` 达到翻页上限，"
            "相关计数是下限而非精确值。可调大 `--max-pages` 或缩小 `--days`。")
    stale = int((source or {}).get("stale") or 0)
    if stale:
        add("")
        add(f"**使用了旧缓存**：{stale} 次请求因网络失败退回本地缓存，"
            "对应数据可能不是最新的。")
    add("")
    add("所有结论仅用于团队改进，**不应用于个人绩效考核**。")
    add("")

    if source:
        add("---")
        add("")
        remaining = source.get("remaining")
        remaining_text = str(remaining) if remaining is not None else "未查询（本次全部命中缓存）"
        add(f"<sub>数据来源：GitHub API（REST + GraphQL）　|　实际请求 {source.get('requests', 0)} 次，"
            f"缓存命中 {source.get('cache_hits', 0)} 次"
            + (f"（其中 {source.get('stale')} 次为网络失败后回退）" if source.get("stale") else "")
            + f"，剩余配额 {remaining_text}　|　效能棱镜 v0.1</sub>")
        add("")

    return "\n".join(lines)
