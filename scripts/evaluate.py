"""跑评测。

    python scripts/evaluate.py --retrieval          # 离线，不花钱，几毫秒
    python scripts/evaluate.py --agent --demo       # 真跑 Agent，消耗 token

--agent 默认用离线快照，不访问 GitHub；加 --real 才去请求真实仓库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，打不出 ✓ 这类字符；先切成 UTF-8 再输出
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from devprism import evaluation  # noqa: E402
from devprism.config import load_settings  # noqa: E402
from devprism.knowledge import KnowledgeBase  # noqa: E402

CASES_PATH = ROOT / "eval" / "cases.json"


def run_retrieval(args) -> int:
    cases = evaluation.load_cases(CASES_PATH)
    kb = KnowledgeBase(ROOT / "knowledge")
    result = evaluation.evaluate_retrieval(kb, cases, k=args.k)

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.json}")
    print(evaluation.format_retrieval(result))
    print(f"\n语料：{kb.stats()}")
    if args.min_recall is not None and result["recall"] < args.min_recall:
        print(f"\n召回率 {result['recall']:.1%} 低于阈值 {args.min_recall:.0%}", file=sys.stderr)
        return 1
    return 0


def run_agent(args) -> int:
    from devprism import agent
    from devprism.cache import HttpCache
    from devprism.cli import _make_client
    from devprism.llm import LLMClient
    from devprism.tools import ToolContext, build_registry

    settings = load_settings()
    if not settings.has_llm:
        print("工具评测需要 LLM_API_KEY，先配置 .env", file=sys.stderr)
        return 2

    demo = not args.real
    client, cache = _make_client(settings, demo)
    cases = evaluation.load_cases(CASES_PATH)
    llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                    proxy=settings.proxy)

    def runner(question: str) -> list[str]:
        ctx = ToolContext(client=client, window_days=180)
        registry = build_registry(ctx)
        prompt = (
            f"目标仓库：google/gson\n分析窗口：最近 180 天\n"
            f"规范来源：本团队内置规范\n\n{question}"
        )
        try:
            agent.run(prompt, registry, llm, max_steps=6, user_input=question)
        except Exception as exc:  # noqa: BLE001 单条失败不该中断整轮
            print(f"  ! {question} 运行失败：{exc}", file=sys.stderr)
        return [call["tool"] for call in registry.calls]

    try:
        result = evaluation.evaluate_tools(runner, cases, limit=args.limit)
    finally:
        if cache is not None:
            cache.close()

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.json}")
    print(evaluation.format_tools(result))
    print(f"\n数据源：{'离线快照' if demo else '真实 GitHub'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="跑评测")
    parser.add_argument("--retrieval", action="store_true", help="检索评测（离线，不花钱）")
    parser.add_argument("--agent", action="store_true", help="工具选择评测（消耗 token）")
    parser.add_argument("--k", type=int, default=3, help="检索取前 k 条，默认 3")
    parser.add_argument("--limit", type=int, help="只跑前 N 条用例（省 token）")
    parser.add_argument("--real", action="store_true", help="工具评测用真实 GitHub 数据")
    parser.add_argument("--json", help="把结果写成 JSON")
    parser.add_argument("--min-recall", type=float,
                        help="召回率低于该值时返回非零退出码，CI 用，例如 0.85")
    args = parser.parse_args()

    if not args.retrieval and not args.agent:
        parser.print_help()
        return 1

    if args.retrieval:
        status = run_retrieval(args)
        if status:
            return status
    if args.agent:
        return run_agent(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
