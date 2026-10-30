"""命令行入口：`python -m devprism analyze <owner>/<repo>`。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, agent, metrics, report
from .cache import HttpCache
from .config import ROOT, load_settings
from .embeddings import make_embedder
from .github import GitHubClient, GitHubError, RateLimitError
from .knowledge import load_conventions_from_repo
from .llm import LLMClient, LLMError, mask_secret
from .tools import ToolContext, build_registry
from .web import serve as serve_web
from .workflow import WorkflowError, WorkflowRunner, WorkflowStore, load_workflow


def parse_repo(value: str) -> tuple[str, str]:
    cleaned = value.strip().rstrip("/")
    if cleaned.startswith("https://github.com/"):
        cleaned = cleaned[len("https://github.com/"):]
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    parts = cleaned.split("/")
    if len(parts) != 2 or not all(parts):
        raise argparse.ArgumentTypeError("仓库格式应为 owner/name，例如 pallets/click")
    return parts[0], parts[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devprism",
        description="效能棱镜 —— 研发效能分析 Agent（指标引擎，无大模型也可运行）",
    )
    parser.add_argument("--version", action="version", version=f"效能棱镜 {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="分析一个 GitHub 仓库并生成报告")
    analyze.add_argument("repo", type=parse_repo, help="owner/name 或仓库 URL")
    analyze.add_argument("--days", type=int, default=180, help="分析窗口天数，默认 180")
    analyze.add_argument("--out", type=Path, help="报告输出路径，默认 report-<repo>.md")
    analyze.add_argument("--json", type=Path, help="同时导出原始指标 JSON")
    analyze.add_argument("--max-pages", type=int, default=6, help="单接口最大翻页数（配额闸门）")

    sub.add_parser("doctor", help="检查 GitHub 与模型配置是否可用")

    ask = sub.add_parser("ask", help="用自然语言提问，由 Agent 自己决定查什么")
    ask.add_argument("question", help="例如：这个仓库的协作健康度怎么样")
    ask.add_argument("repo", type=parse_repo, help="owner/name 或仓库 URL")
    ask.add_argument("--days", type=int, default=180, help="分析窗口天数")
    ask.add_argument("--max-steps", type=int, default=8, help="最大推理步数")
    ask.add_argument("--max-pages", type=int, default=6, help="单接口最大翻页数，需与 analyze 一致")
    ask.add_argument(
        "--conventions",
        choices=["team", "repo"],
        default="team",
        help="team=用本项目内置的团队规范；repo=抓取目标仓库自己的 CONTRIBUTING 文档",
    )

    run = sub.add_parser("run", help="执行 YAML 定义的工作流")
    run.add_argument("workflow", type=Path, help="工作流文件，例如 workflows/weekly.yaml")
    run.add_argument("--var", action="append", default=[], metavar="K=V",
                     help="输入变量，可重复，例如 --var owner=pallets")
    run.add_argument("--days", type=int, default=180, help="分析窗口天数")
    run.add_argument("--max-pages", type=int, default=6, help="单接口最大翻页数")
    run.add_argument("--resume", metavar="RUN_ID", help="从暂停的运行处继续")
    run.add_argument("--approve", action="store_true", help="继续时放行人工确认节点")

    serve = sub.add_parser("serve", help="启动 Web 控制台，浏览器里看推理轨迹")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    serve.add_argument("--port", type=int, default=8800, help="端口，默认 8800")
    return parser


def run_analysis(owner: str, name: str, days: int, max_pages: int) -> dict:
    settings = load_settings()
    cache = HttpCache(settings.cache_path)
    client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)

    try:
        now = datetime.now(timezone.utc)
        # 对齐到 UTC 零点：否则 since 带秒级时间戳，每次运行 URL 都不同，缓存永远不命中
        since = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        print(f"→ 拉取 {owner}/{name} 最近 {days} 天的数据…", file=sys.stderr)
        repo_meta = client.repo(owner, name)
        commits = client.commits(owner, name, since_iso, max_pages=max_pages)
        # 有 Token 时走 GraphQL：响应体小，且自带评审次数，不必逐 PR 再请求
        if settings.github_token:
            pulls = client.pulls_via_graphql(
                owner, name, max_pages=max_pages, since=since_iso
            )
        else:
            pulls = client.pulls(owner, name, max_pages=max(1, max_pages - 1))

        summary = metrics.summarize(repo_meta, commits, pulls, days, now)

        # Java 工程结构：1 次 trees 请求 + 少量关键文件。
        # 拿不到也不能拖垮整份报告，所以单独兜底。
        try:
            from .repo_index import build_index, java_risk_items

            index = build_index(client, owner, name, repo_meta.get("default_branch"))
            summary["java"] = index.structure.to_dict()
            summary["java"]["failed_files"] = index.failed
            summary["risks"].extend(java_risk_items(index.structure))
        except Exception as exc:  # noqa: BLE001 - 结构分析属于增强项
            summary["java"] = {"is_java": False, "error": str(exc)}

        print(
            f"  提交 {summary['commits']['total']} 条（拉取 {len(commits)}），"
            f"PR {summary['pulls']['created']} 个（拉取 {len(pulls)}）"
            f"｜请求 {client.requests_made} 次，缓存命中 {client.cache_hits} 次",
            file=sys.stderr,
        )
        summary["_source"] = {
            "requests": client.requests_made,
            "cache_hits": client.cache_hits,
            "stale": client.stale_hits,
            "remaining": client.remaining,
            "truncated": sorted(client.truncated),
        }
        return summary
    finally:
        cache.close()


def cmd_analyze(args: argparse.Namespace) -> int:
    owner, name = args.repo
    try:
        summary = run_analysis(owner, name, args.days, args.max_pages)
    except RateLimitError as exc:
        print(f"配额不足：{exc}", file=sys.stderr)
        return 2
    except GitHubError as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        return 1

    source = summary.pop("_source", {})
    markdown = report.render(summary, source)

    out_path = args.out or Path(f"report-{owner}-{name}.md")
    out_path.write_text(markdown, encoding="utf-8")
    print(f"✓ 报告已生成：{out_path.resolve()}", file=sys.stderr)

    if args.json:
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✓ 指标 JSON 已导出：{args.json.resolve()}", file=sys.stderr)

    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.has_llm:
        print("未配置 LLM_API_KEY，Agent 无法运行。先执行 `python -m devprism doctor` 检查。",
              file=sys.stderr)
        return 2

    owner, name = args.repo
    cache = HttpCache(settings.cache_path)
    client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)

    knowledge_seed = None
    if args.conventions == "repo":
        knowledge_seed, loaded_path = load_conventions_from_repo(client, owner, name)
        if knowledge_seed is None:
            print("⚠ 未找到该仓库的 CONTRIBUTING 文档，回退到团队规范。", file=sys.stderr)
        else:
            print(f"→ 已加载该仓库自身规范：{loaded_path}"
                  f"（{knowledge_seed.stats()['chunks']} 个片段）", file=sys.stderr)

    embedder = make_embedder(settings, settings.cache_path.parent / "vectors.sqlite3")
    ctx = ToolContext(
        client,
        window_days=args.days,
        max_pages=args.max_pages,
        knowledge_seed=knowledge_seed,
        embedder=embedder,
    )
    registry = build_registry(ctx)
    llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model, proxy=settings.proxy)

    def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind == "thought":
            print(f"\n[思考 {event['index']}] {event['content']}", file=sys.stderr)
        elif kind == "tool_call":
            print(f"  → {event['tool']}({event.get('arguments')})", file=sys.stderr)
        elif kind == "observation":
            print(f"  ← {'ok' if event['ok'] else '失败'}：{event['content'][:300]}",
                  file=sys.stderr)
        elif kind == "citation_warning":
            print(f"  ⚠ 以下引用未在知识库中找到：{', '.join(event['invalid'])}",
                  file=sys.stderr)

    # 必须显式告诉模型分析哪个仓库：工具参数里要填 owner/name，
    # 不写清楚模型就会自己编一个（实测编成了 pallets/click）。
    prompt = (
        f"目标仓库：{owner}/{name}\n"
        f"分析窗口：最近 {args.days} 天\n"
        f"规范来源：{'该仓库自身的 CONTRIBUTING 文档' if knowledge_seed else '本团队内置规范'}\n\n"
        f"{args.question}"
    )

    print(f"→ 分析 {owner}/{name}，窗口 {args.days} 天", file=sys.stderr)
    try:
        result = agent.run(
            prompt, registry, llm, max_steps=args.max_steps, on_event=on_event
        )
    except LLMError as exc:
        print(f"模型调用失败：{exc}", file=sys.stderr)
        return 1
    finally:
        cache.close()

    print("\n" + "─" * 60)
    print(result.answer)
    print("─" * 60)
    print(f"工具调用 {result.tool_calls} 次，结束原因：{result.stopped_reason}",
          file=sys.stderr)
    return 0 if result.stopped_reason == "answered" else 1


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()

    try:
        definition = load_workflow(args.workflow)
    except (WorkflowError, ValueError, OSError) as exc:
        print(f"工作流定义有误：{exc}", file=sys.stderr)
        return 1

    inputs: dict[str, str] = {}
    for item in args.var:
        if "=" not in item:
            print(f"--var 格式应为 key=value，收到：{item}", file=sys.stderr)
            return 1
        key, _, value = item.partition("=")
        inputs[key.strip()] = value.strip()

    cache = HttpCache(settings.cache_path)
    client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)
    ctx = ToolContext(
        client,
        window_days=args.days,
        max_pages=args.max_pages,
        embedder=make_embedder(settings, settings.cache_path.parent / "vectors.sqlite3"),
    )
    registry = build_registry(ctx)
    llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model, proxy=settings.proxy)
    store = WorkflowStore(settings.cache_path.parent / "workflows.sqlite3")

    def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind == "step_start":
            print(f"→ [{event['step']}] {event['kind']}", file=sys.stderr)
        elif kind == "step_done":
            output = event.get("output")
            preview = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            print(f"  ✓ {preview[:160]}", file=sys.stderr)
        elif kind == "step_failed":
            print(f"  ✗ {event['error']}", file=sys.stderr)
        elif kind == "paused":
            print(f"  ⏸ 停在人工确认节点 {event['step']}：{event['message']}",
                  file=sys.stderr)
        elif kind == "resume":
            print(f"→ 继续运行 {event['run_id']}", file=sys.stderr)

    print(f"→ 工作流：{definition.name}（{len(definition.steps)} 步）", file=sys.stderr)
    try:
        result = WorkflowRunner(
            definition, registry, llm, store, inputs, on_event=on_event
        ).run(run_id=args.resume, approve=args.approve)
    finally:
        cache.close()
        store.close()

    if result.status == "paused":
        if isinstance(result.last_output, str):
            print("\n" + "─" * 60)
            print(result.last_output)
            print("─" * 60)
        print(f"\n运行已暂停，run_id = {result.run_id}", file=sys.stderr)
        print(f"确认无误后执行：python -m devprism run {args.workflow} "
              f"--resume {result.run_id} --approve", file=sys.stderr)
        return 2

    if result.status == "failed":
        print(f"工作流失败：{result.error}", file=sys.stderr)
        print(f"修复后可续跑：--resume {result.run_id}", file=sys.stderr)
        return 1

    if isinstance(result.last_output, dict) and result.last_output.get("path"):
        print(f"✓ 已写入 {result.last_output['path']}", file=sys.stderr)
    print(f"✓ 工作流完成，run_id = {result.run_id}", file=sys.stderr)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings()
    env_file = ROOT / ".env"

    print("效能棱镜 环境自检")
    print(f"  Python       : {sys.version.split()[0]}")
    print(f"  配置文件     : {'已加载 .env' if env_file.is_file() else '未找到 .env（可用 .env.example 复制）'}")
    print(f"  GitHub Token : {mask_secret(settings.github_token)}")
    print(f"  网络代理     : {settings.proxy or '未配置（直连）'}")
    if settings.proxy:
        from .net import probe

        reachable, detail = probe(settings.proxy)
        print(f"  代理连通性   : {'可用' if reachable else '不可用'} —— {detail}")

    cache = HttpCache(settings.cache_path)
    client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)
    try:
        # 配额查询必须绕过缓存，否则换了 token 也会读到旧身份的结果
        data = client.get("/rate_limit", use_cache=False)
        core = data["resources"]["core"]
        print(f"  GitHub API   : 可用，剩余 {core['remaining']}/{core['limit']} 次/小时")
    except (GitHubError, KeyError, TypeError) as exc:
        print(f"  GitHub API   : 不可用 —— {exc}")
    finally:
        cache.close()

    print(f"  LLM Base URL : {settings.llm_base_url}")
    print(f"  LLM Model    : {settings.llm_model}")
    print(f"  LLM API Key  : {mask_secret(settings.llm_api_key)}")
    if settings.embed_enabled:
        print(f"  向量检索     : 已启用 · {settings.embed_model}")
        try:
            probe_vec = make_embedder(settings).embed(["连通性测试"])[0]  # type: ignore[union-attr]
            print(f"  向量连通性   : 正常（{len(probe_vec)} 维）")
        except Exception as exc:  # noqa: BLE001
            print(f"  向量连通性   : 失败 —— {exc}（会自动退回 BM25）")
    else:
        print("  向量检索     : 未启用（直接走 BM25；配 EMBED_MODEL 可开启混合检索）")

    if not settings.has_llm:
        print("\n⚠ LLM_API_KEY 未配置：Agent 循环暂不可用（指标引擎不受影响，仍可生成报告）。")
        print("  在 .env 里填入 LLM_API_KEY 后重新运行本命令即可验证。")
        return 0

    try:
        reply = LLMClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            proxy=settings.proxy,
        ).ping()
        print(f"  LLM 连通性   : 正常（模型回复：{reply!r}）")
    except LLMError as exc:
        print(f"  LLM 连通性   : 失败 —— {exc}")
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "analyze":
        return cmd_analyze(args)
    if args.command == "ask":
        return cmd_ask(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "serve":
        return serve_web(args.host, args.port)
    if args.command == "doctor":
        return cmd_doctor(args)
    parser.print_help()
    return 1
