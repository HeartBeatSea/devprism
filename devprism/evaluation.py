"""评测：把「效果还行」变成两个能写进简历的数字。

两件事分开评，因为成本不一样：

* **检索评测**——纯离线，不调模型，几毫秒跑完，可以进 CI。
  指标是 recall@k 和 MRR：答案所在片段有没有进前 k 条、排第几。
* **工具评测**——要真跑 Agent，消耗 token。指标是工具选择准确率：
  该调的工具都调到了没有。

用例放在 `eval/cases.json`，一条用例可以只参与其中一种评测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Case:
    id: str
    question: str
    expect_sources: list[str] = field(default_factory=list)
    expect_tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases: list[Case] = []
    for item in payload.get("cases", []):
        question = (item.get("question") or "").strip()
        if not question:
            continue
        cases.append(Case(
            id=item.get("id") or question[:12],
            question=question,
            expect_sources=list(item.get("expect_sources") or []),
            expect_tools=list(item.get("expect_tools") or []),
            tags=list(item.get("tags") or []),
        ))
    return cases


# ---------------------------------------------------------------- 检索

def evaluate_retrieval(kb: Any, cases: list[Case], k: int = 3) -> dict[str, Any]:
    """recall@k：期望的引用有没有出现在前 k 条里；MRR：排第几。"""
    usable = [case for case in cases if case.expect_sources]
    rows: list[dict[str, Any]] = []
    hits = 0
    reciprocal = 0.0

    for case in usable:
        results = kb.search(case.question, top_k=k)
        returned = [item["source"] for item in results]
        rank = next(
            (index + 1 for index, source in enumerate(returned) if source in case.expect_sources),
            None,
        )
        hit = rank is not None
        hits += 1 if hit else 0
        reciprocal += (1.0 / rank) if rank else 0.0
        rows.append({
            "id": case.id,
            "question": case.question,
            "hit": hit,
            "rank": rank,
            "returned": returned,
            "expected": case.expect_sources,
        })

    total = len(usable)
    return {
        "kind": "retrieval",
        "k": k,
        "total": total,
        "hits": hits,
        "recall": round(hits / total, 3) if total else 0.0,
        "mrr": round(reciprocal / total, 3) if total else 0.0,
        "misses": [row for row in rows if not row["hit"]],
        "cases": rows,
    }


# ---------------------------------------------------------------- 工具选择

# 这三类是「先看一眼」的定向工具，不是答案的一部分：
# list_skills / load_skill 是提示词要求开场必查的，repo_overview 用于确认仓库规模。
# 把它们算作多余调用，会把精确率算低，也不符合设计意图。
HELPER_TOOLS = {"list_skills", "load_skill", "repo_overview"}


def evaluate_tools(
    runner: Callable[[str], list[str]],
    cases: list[Case],
    limit: int | None = None,
) -> dict[str, Any]:
    """runner(question) -> 本轮实际调用过的工具名列表。"""
    usable = [case for case in cases if case.expect_tools]
    if limit:
        usable = usable[:limit]

    rows: list[dict[str, Any]] = []
    correct = 0
    tool_tp = tool_fp = tool_fn = 0

    for case in usable:
        called = set(runner(case.question) or [])
        expected = set(case.expect_tools)

        missing = sorted(expected - called)
        extra = sorted(called - expected)
        ok = not missing

        correct += 1 if ok else 0
        tool_tp += len(expected & called)
        tool_fn += len(missing)
        tool_fp += len(set(extra) - HELPER_TOOLS)

        rows.append({
            "id": case.id,
            "question": case.question,
            "ok": ok,
            "called": sorted(called),
            "expected": sorted(expected),
            "missing": missing,
            "extra": extra,
        })

    total = len(usable)
    precision = tool_tp / (tool_tp + tool_fp) if (tool_tp + tool_fp) else 0.0
    recall = tool_tp / (tool_tp + tool_fn) if (tool_tp + tool_fn) else 0.0
    return {
        "kind": "tools",
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "tool_precision": round(precision, 3),
        "tool_recall": round(recall, 3),
        "cases": rows,
    }


# ---------------------------------------------------------------- 输出

def format_retrieval(result: dict[str, Any]) -> str:
    lines = [
        f"检索评测 · recall@{result['k']} = {result['recall']:.1%}"
        f"（{result['hits']}/{result['total']}）· MRR = {result['mrr']:.3f}",
        "",
    ]
    for row in result["cases"]:
        mark = "✓" if row["hit"] else "✗"
        rank = f"第{row['rank']}位" if row["rank"] else "未命中"
        lines.append(f"  {mark} {row['id']:<12} {rank:<6} {row['question']}")
        if not row["hit"]:
            lines.append(f"      期望：{row['expected'][0]}")
            lines.append(f"      实得：{'、'.join(row['returned']) or '（空）'}")
    return "\n".join(lines)


def format_tools(result: dict[str, Any]) -> str:
    lines = [
        f"工具选择评测 · 准确率 = {result['accuracy']:.1%}"
        f"（{result['correct']}/{result['total']}）"
        f" · 精确率 {result['tool_precision']:.1%} · 召回 {result['tool_recall']:.1%}",
        "",
    ]
    for row in result["cases"]:
        mark = "✓" if row["ok"] else "✗"
        lines.append(f"  {mark} {row['id']:<10} {row['question']}")
        if not row["ok"]:
            lines.append(f"      期望：{'、'.join(row['expected'])}")
            lines.append(f"      实调：{'、'.join(row['called']) or '（无）'}")
    return "\n".join(lines)
