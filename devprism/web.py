"""Web 控制台：FastAPI + SSE，把 Agent 的推理过程实时推到浏览器。

选 FastAPI 的理由：
  * 路由和参数校验交给框架，少写一堆样板；
  * `StreamingResponse` 天然适合 SSE；
  * 这是真实项目里最常见的组合，作品里出现它比手搓 `http.server` 更有说服力。

依赖是**可选**的（`pip install -e ".[web]"`）。没装时其余命令照常可用，
`devprism serve` 会给出明确的安装提示而不是抛一堆 traceback。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from typing import Any, Callable

from . import agent
from .cache import HttpCache
from .config import ROOT, load_settings
from .github import GitHubClient, GitHubError
from .history import HistoryStore
from .knowledge import (
    delete_custom,
    list_custom,
    load_conventions_from_repo,
    load_custom,
    save_custom,
)
from .llm import LLMClient
from .markdown_lite import render as render_markdown
from .tools import ToolContext, build_registry

try:  # 可选依赖
    import uvicorn
    from fastapi import Body, FastAPI, Query
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    WEB_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于环境
    WEB_AVAILABLE = False
    uvicorn = None  # type: ignore[assignment]
    FastAPI = Query = None  # type: ignore[assignment]
    HTMLResponse = JSONResponse = StreamingResponse = None  # type: ignore[assignment]


def split_repo(raw: str) -> tuple[str, str]:
    cleaned = raw.strip().rstrip("/")
    if cleaned.startswith("https://github.com/"):
        cleaned = cleaned[len("https://github.com/"):]
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    parts = cleaned.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("仓库格式应为 owner/name，例如 pallets/click")
    return parts[0], parts[1]


def resolve_knowledge(
    client: GitHubClient, owner: str, name: str, conventions: str
) -> tuple[Any, str]:
    """把 conventions 参数解析成知识库。

    返回 (KnowledgeBase | None, 人类可读的来源说明)。
    None 表示用项目内置的团队规范（由 ToolContext 自己加载）。
    """
    if conventions.startswith("custom:"):
        slug = conventions.split(":", 1)[1]
        base = load_custom(slug)
        return (base, f"自定义规范「{slug}」") if base else (None, "自定义规范不存在，回退团队规范")

    if conventions == "repo":
        base, path = load_conventions_from_repo(client, owner, name)
        if base is None:
            return None, "未找到该仓库的 CONTRIBUTING，回退团队规范"
        return base, f"该仓库自身规范：{path}"

    return None, "本团队内置规范"


def run_analysis(
    repo: str,
    question: str,
    days: int,
    conventions: str,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """同步执行一次分析，事件通过 emit 抛出去。运行在独立线程里。"""
    settings = load_settings()
    if not settings.has_llm:
        emit({"type": "error", "message": "未配置 LLM_API_KEY，Agent 无法运行"})
        return

    owner, name = split_repo(repo)
    cache = HttpCache(settings.cache_path)
    trace: list[dict[str, Any]] = []
    try:
        client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)

        seed, label = resolve_knowledge(client, owner, name, conventions)
        if conventions != "team":
            emit({"type": "note", "message": f"规范来源：{label}"})

        ctx = ToolContext(client, window_days=days, knowledge_seed=seed)
        registry = build_registry(ctx)
        llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model, proxy=settings.proxy)

        prompt = (
            f"目标仓库：{owner}/{name}\n"
            f"分析窗口：最近 {days} 天\n"
            f"规范来源：{label}\n\n"
            f"{question}"
        )

        def agent_event(event: dict[str, Any]) -> None:
            # Agent 内部也会发一个只有纯文本的 answer；丢掉它，
            # 统一由本函数末尾发送带渲染 HTML 的那一条，避免前端收到两次。
            if event.get("type") == "answer":
                return
            trace.append(event)
            emit(event)

        result = agent.run(prompt, registry, llm, on_event=agent_event)
        emit({
            "type": "answer",
            "markdown": result.answer,
            "html": render_markdown(result.answer),
            "tool_calls": result.tool_calls,
        })

        # 合并进问答流：把结构化指标一并推给前端，用于渲染指标卡与分布图。
        # 这里失败不能影响已经给出的文字结论。
        metrics_payload: dict[str, Any] | None = None
        try:
            metrics_payload = build_report_payload(f"{owner}/{name}", days)
            emit({"type": "metrics", **metrics_payload})
        except Exception as exc:  # noqa: BLE001
            emit({"type": "note", "message": f"结构化指标未生成：{exc}"})

        # 自动落库。指标快照要一起存，否则回看历史时右侧面板是空的。
        record: dict[str, Any] = {
            "trace": trace[-80:],
            "tool_calls": result.tool_calls,
        }
        if metrics_payload:
            record["summary"] = metrics_payload["summary"]
            record["source"] = metrics_payload["source"]

        store = HistoryStore(settings.cache_path.parent / "history.sqlite3")
        try:
            store.add(
                owner=owner, name=name, kind="agent", title=question.strip()[:80] or "问答",
                days=days, conventions=conventions, question=question,
                markdown=result.answer, payload=record,
            )
        finally:
            store.close()
    finally:
        cache.close()


def build_report_payload(repo: str, days: int) -> dict[str, Any]:
    """跑一遍确定性指标并渲染成报告，问答流和 /api/report 共用。"""
    from . import report as report_module
    from .cli import run_analysis as analyze

    owner, name = split_repo(repo)
    summary = analyze(owner, name, days, 6)
    source = summary.pop("_source", {})
    markdown = report_module.render(summary, source)
    return {
        "markdown": markdown,
        "html": render_markdown(markdown),
        "commits": summary["commits"]["total"],
        "merged": summary["pulls"]["merged"],
        "summary": summary,
        "source": source,
    }


def build_app() -> Any:
    if not WEB_AVAILABLE:
        raise RuntimeError("Web 控制台需要 FastAPI 与 uvicorn")

    app = FastAPI(title="效能棱镜", description="研发效能分析 Agent", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        path = ROOT / "web" / "index.html"
        if not path.is_file():
            return HTMLResponse("<h1>web/index.html 不存在</h1>", status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/meta")
    def meta() -> Any:
        settings = load_settings()
        return JSONResponse({
            "github": bool(settings.github_token),
            "llm": settings.has_llm,
            "model": settings.llm_model,
        })

    @app.get("/api/report")
    def quick_report(
        repo: str = Query(..., description="owner/name"),
        days: int = Query(180, ge=7, le=3650),
    ) -> Any:
        """只要结构化指标、不要大模型时用这个接口。

        界面上已经并入问答流，这里保留是为了脚本化调用和排错。
        """
        try:
            payload = build_report_payload(repo, days)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except GitHubError as exc:
            return JSONResponse({"detail": f"GitHub 请求失败：{exc}"}, status_code=502)
        except Exception as exc:  # 兜底：别让前端只看到 500
            return JSONResponse(
                {"detail": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

        owner, name = split_repo(repo)
        settings = load_settings()
        store = HistoryStore(settings.cache_path.parent / "history.sqlite3")
        try:
            store.add(
                owner=owner, name=name, kind="report", title="指标报告",
                days=days, conventions=None, markdown=payload["markdown"],
                payload={"summary": payload["summary"], "source": payload["source"]},
            )
        finally:
            store.close()

        return JSONResponse(payload)

    # ---------- 历史 ----------

    def _store() -> Any:
        settings = load_settings()
        return HistoryStore(settings.cache_path.parent / "history.sqlite3")

    @app.get("/api/history")
    def history_list() -> Any:
        store = _store()
        try:
            return JSONResponse({"groups": store.groups()})
        finally:
            store.close()

    @app.get("/api/history/{record_id}")
    def history_detail(record_id: str) -> Any:
        store = _store()
        try:
            record = store.get(record_id)
        finally:
            store.close()
        if record is None:
            return JSONResponse({"detail": "记录不存在"}, status_code=404)
        if record.get("markdown"):
            record["html"] = render_markdown(record["markdown"])
        return JSONResponse(record)

    @app.delete("/api/history/{record_id}")
    def history_delete(record_id: str) -> Any:
        store = _store()
        try:
            ok = store.delete(record_id)
        finally:
            store.close()
        return JSONResponse({"deleted": ok}, status_code=200 if ok else 404)

    @app.delete("/api/history")
    def history_clear(owner: str = Query(...), name: str = Query(...)) -> Any:
        store = _store()
        try:
            removed = store.delete_repo(owner, name)
        finally:
            store.close()
        return JSONResponse({"removed": removed})

    # ---------- 自定义规范 ----------

    @app.get("/api/conventions")
    def conventions_list() -> Any:
        return JSONResponse({"items": list_custom()})

    @app.post("/api/conventions")
    def conventions_create(payload: dict = Body(...)) -> Any:
        name = str(payload.get("name") or "").strip()
        content = str(payload.get("content") or "")
        if not name or not content.strip():
            return JSONResponse({"detail": "名称和内容都不能为空"}, status_code=400)
        try:
            slug = save_custom(name, content)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"slug": slug, "items": list_custom()})

    @app.delete("/api/conventions/{slug}")
    def conventions_delete(slug: str) -> Any:
        return JSONResponse({"deleted": delete_custom(slug)})

    @app.get("/api/ask")
    async def ask(
        repo: str = Query(..., description="owner/name"),
        question: str = Query("这个仓库的协作健康度怎么样？"),
        days: int = Query(180, ge=7, le=3650),
        conventions: str = Query("team", description="team | repo | custom:<slug>"),
    ) -> Any:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        closed = threading.Event()

        def emit(event: dict[str, Any]) -> None:
            if closed.is_set():
                return
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def worker() -> None:
            emit({"type": "start", "repo": repo, "days": days})
            try:
                run_analysis(repo, question, days, conventions, emit)
            except Exception as exc:  # 任何异常都要变成事件，否则前端只看到连接断开
                emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                emit({"type": "end"})

        threading.Thread(target=worker, daemon=True).start()

        async def events() -> Any:
            try:
                while True:
                    event = await queue.get()
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("type") == "end":
                        break
            finally:
                # 客户端断开时停止接收事件，后台线程自然跑完就退出
                closed.set()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def serve(host: str = "127.0.0.1", port: int = 8800) -> int:
    if not WEB_AVAILABLE:
        print("Web 控制台需要 FastAPI 与 uvicorn，先安装：", file=sys.stderr)
        print('  pip install -e ".[web]"', file=sys.stderr)
        return 2

    settings = load_settings()
    print(f"效能棱镜 Web 控制台：http://{host}:{port}")
    print(f"  GitHub Token：{'已配置' if settings.github_token else '未配置（60 次/小时）'}")
    print(f"  模型：{settings.llm_model if settings.has_llm else '未配置'}")
    print("按 Ctrl+C 停止\n")

    uvicorn.run(build_app(), host=host, port=port, log_level="warning")
    return 0
