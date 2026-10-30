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
import base64
import hashlib
import json
import sys
import threading
from typing import Any, Callable

from . import agent
from .cache import HttpCache
from .codegraph import build_graph
from .config import ROOT, load_settings
from .embeddings import make_embedder
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
from .symbols import extract_symbols, summarize_symbols
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
    embedder: Any = None
    try:
        client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)

        seed, label = resolve_knowledge(client, owner, name, conventions)
        if conventions != "team":
            emit({"type": "note", "message": f"规范来源：{label}"})

        embedder = make_embedder(settings, settings.cache_path.parent / "vectors.sqlite3")
        ctx = ToolContext(client, window_days=days, knowledge_seed=seed, embedder=embedder)
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
        if embedder is not None and hasattr(embedder, "close"):
            embedder.close()
        cache.close()


MAX_SOURCE_BYTES = 40_000
MAX_SOURCE_FILES = 3

_EXPLAIN_PROMPT = """你在解释一个代码仓库里的一部分。只根据下面给出的源码回答，
不要推测源码里没有出现的功能；信息不足就直接说「源码里看不出来」。

仓库：{repo}
路径：{path}
类型：{kind}
文件：{files}
识别到的符号：{symbols}

源码（每个文件最多 {limit} 字符）：
{source}

请用 Markdown 输出，控制在 200 字以内，结构固定为：
**职责**：一句话说清它负责什么
**关键结构**：列出主要的类/函数及各自作用
**注意**：明显的约束、风险或看不懂的地方（没有就写「无」）
"""


def _read_text(
    client: GitHubClient, owner: str, name: str, path: str
) -> tuple[str | None, str | None]:
    """读一个文件的正文，失败时把原因带回来。

    之前直接用 client.file_text，它把网络错误吞成 None，前端只能显示
    「没有可读源码」——用户根本不知道是断网、是 404、还是文件太大。
    """
    try:
        payload = client.get(f"/repos/{owner}/{name}/contents/{path}")
    except GitHubError as exc:
        # contents API 不通时换 raw 域名再试一次：两者常常一个被拦一个能通
        fallback = client.raw_text(owner, name, path, limit=MAX_SOURCE_BYTES)
        if fallback:
            return fallback, None
        return None, f"读取失败：{exc}"
    if not isinstance(payload, dict):
        return None, "接口返回格式异常"
    if payload.get("encoding") == "none" or not payload.get("content"):
        fallback = client.raw_text(owner, name, path, limit=MAX_SOURCE_BYTES)
        if fallback:
            return fallback, None
        size = payload.get("size") or 0
        return None, f"GitHub 不返回正文（{size / 1024:.0f} KB，可能超过单文件上限）"
    try:
        return base64.b64decode(payload["content"]).decode("utf-8", "replace"), None
    except (ValueError, TypeError) as exc:
        return None, f"内容解码失败：{exc}"


def _collect_sources(
    client: GitHubClient, owner: str, name: str, blobs: dict[str, int], path: str
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """挑出要读的文件：文件节点就是它自己，目录节点取体积最大的几个源码文件。"""
    if path in blobs:
        candidates = [path]
    else:
        prefix = f"{path}/" if path else ""
        candidates = [
            item for item in blobs
            if item.startswith(prefix) and item != path
        ]
        # 优先读源码文件，再读其它；同类里挑小的，省 token
        candidates.sort(key=lambda item: (
            0 if item.lower().endswith((".java", ".kt", ".py", ".js", ".ts", ".tsx", ".go", ".rs", ".rb")) else 1,
            blobs.get(item, 0),
        ))
        candidates = candidates[:MAX_SOURCE_FILES]

    files: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    problems: list[str] = []
    for candidate in candidates:
        size = blobs.get(candidate, 0)
        files.append({"path": candidate, "bytes": size})
        if size > MAX_SOURCE_BYTES:
            problems.append(f"`{candidate}` 超过 {MAX_SOURCE_BYTES // 1000} KB，已跳过")
            continue
        text, error = _read_text(client, owner, name, candidate)
        if text:
            texts[candidate] = text[:MAX_SOURCE_BYTES]
        elif error:
            problems.append(f"`{candidate}` {error}")
    return files, texts, problems


def _explain_with_llm(
    settings: Any,
    cache: HttpCache,
    repo: str,
    path: str,
    blobs: dict[str, int],
    files: list[dict[str, Any]],
    texts: dict[str, str],
    symbols: list[dict[str, Any]],
    problems: list[str] | None = None,
) -> tuple[str, str]:
    """返回 (markdown, 说明)。没配模型或调用失败时，退回纯结构描述。"""
    problems = problems or []
    kind = "文件" if path in blobs else ("目录" if path else "仓库根目录")
    file_list = "、".join(item["path"].rsplit("/", 1)[-1] for item in files) or "无"

    if not texts:
        structure = "\n".join(
            f"- `{item['path']}`（{item['bytes'] / 1024:.1f} KB）" for item in files
        ) or "- 该目录下没有可直接读取的源码文件"
        reasons = "\n".join(f"- {item}" for item in problems) or "- 未获取到文件正文"
        markdown = (
            "**职责**：读取源码失败，这里只能给出结构信息。\n\n"
            f"**读取失败的原因**：\n{reasons}\n\n"
            f"**包含的文件**：\n{structure}"
        )
        return markdown, problems[0] if problems else "没有可读源码"

    if not settings.has_llm:
        names = "、".join(f"`{item['path']}`" for item in texts)
        markdown = (
            f"**职责**：未配置大模型，这里只列出结构与符号。\n\n"
            f"**读取的文件**：{names}\n\n**识别到的符号**：{summarize_symbols(symbols)}"
        )
        return markdown, "未配置 LLM_API_KEY，仅展示结构"

    source = "\n\n".join(
        f"### {file_path}\n```\n{text}\n```" for file_path, text in texts.items()
    )
    prompt = _EXPLAIN_PROMPT.format(
        repo=repo, path=path or "/", kind=kind, files=file_list,
        symbols=summarize_symbols(symbols), source=source[:24_000], limit=MAX_SOURCE_BYTES,
    )

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    key = f"explain:v1:{repo}:{path or '/'}:{digest}"
    long_cache = HttpCache(settings.cache_path, ttl=30 * 24 * 3600)
    try:
        cached = long_cache.get(key)
        if cached is not None:
            return cached[0], "来自缓存"

        llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                        proxy=settings.proxy)
        reply = llm.chat([
            {"role": "system", "content": "你是一个严谨的代码解读助手。"},
            {"role": "user", "content": prompt},
        ])
        content = (reply["choices"][0]["message"].get("content") or "").strip()
        if not content:
            raise RuntimeError("模型返回为空")
        long_cache.put(key, content, None)
        return content, "由模型生成"
    except Exception as exc:  # noqa: BLE001 解释失败不该让整页崩掉
        names = "、".join(f"`{item['path']}`" for item in texts)
        markdown = (
            f"**职责**：模型调用失败，先给出结构。\n\n"
            f"**读取的文件**：{names}\n\n**识别到的符号**：{summarize_symbols(symbols)}\n\n"
            f"> 失败原因：{exc}"
        )
        return markdown, f"模型调用失败：{exc}"
    finally:
        long_cache.close()


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

    # ---------- 代码分析模式 ----------

    @app.get("/api/graph")
    def graph(
        repo: str = Query(..., description="owner/name"),
        focus: str = Query("code", description="code=只看核心代码，all=全部文件"),
    ) -> Any:
        """返回仓库结构图。只读目录树，不读源码，所以不额外消耗配额。"""
        try:
            owner, name = split_repo(repo)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        settings = load_settings()
        cache = HttpCache(settings.cache_path)
        try:
            client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)
            meta_info = client.repo(owner, name)
            tree = client.tree(owner, name, meta_info.get("default_branch") or "HEAD")
            payload = build_graph(f"{owner}/{name}", tree, focus=focus)
            payload["repo"] = f"{owner}/{name}"
            payload["tree_truncated"] = bool(getattr(client, "tree_truncated", False))

            # 记住这次代码分析，用户下次不用重新输入仓库。同一仓库同一视角只留一条。
            store = HistoryStore(settings.cache_path.parent / "history.sqlite3")
            try:
                store.add(
                    owner=owner, name=name, kind="code",
                    title="核心代码" if focus == "code" else "全部文件",
                    days=0, markdown="",
                    payload={
                        "focus": focus,
                        "nodes": len(payload["nodes"]),
                        "total_files": payload["total_files"],
                    },
                )
            finally:
                store.close()

            return JSONResponse(payload)
        except GitHubError as exc:
            return JSONResponse({"detail": f"GitHub 请求失败：{exc}"}, status_code=502)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)
        finally:
            cache.close()

    @app.get("/api/explain")
    def explain(
        repo: str = Query(..., description="owner/name"),
        path: str = Query(..., description="仓库内的路径，空字符串表示根目录"),
    ) -> Any:
        """按需读取一小部分源码，交给模型概括结构与功能。

        只在用户点开某个节点时调用，避免为了画图去读完整个仓库。
        """
        try:
            owner, name = split_repo(repo)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        settings = load_settings()
        cache = HttpCache(settings.cache_path)
        try:
            client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)
            meta_info = client.repo(owner, name)
            tree = client.tree(owner, name, meta_info.get("default_branch") or "HEAD")
            blobs = {
                entry.get("path"): int(entry.get("size") or 0)
                for entry in tree
                if entry.get("type") == "blob"
            }

            files, texts, problems = _collect_sources(client, owner, name, blobs, path)
            symbols: list[dict[str, Any]] = []
            for file_path, text in texts.items():
                symbols.extend(
                    {**sym, "file": file_path}
                    for sym in extract_symbols(file_path, text)
                )
                if len(symbols) >= 60:
                    break

            summary, note = _explain_with_llm(
                settings, cache, f"{owner}/{name}", path, blobs, files, texts, symbols,
                problems,
            )
            return JSONResponse({
                "path": path or "/",
                "kind": "file" if path in blobs else ("dir" if path else "root"),
                "summary": summary,
                "html": render_markdown(summary),
                "symbols": symbols[:60],
                "files": files,
                "sources": [
                    {"path": file_path, "text": text[:12000], "bytes": len(text)}
                    for file_path, text in texts.items()
                ],
                "problems": problems,
                "note": note,
            })
        except GitHubError as exc:
            return JSONResponse({"detail": f"GitHub 请求失败：{exc}"}, status_code=502)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)
        finally:
            cache.close()

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
