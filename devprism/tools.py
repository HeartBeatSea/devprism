"""工具层：把指标能力包装成模型可调用、可校验的工具。

三个设计要点：

1. **工具只声明 schema，不碰 prompt。** 模型靠 description 决定调哪个，
   人靠 schema 做参数校验，两边职责分开。
2. **同一会话内复用数据。** 模型可能连环调用四五个工具，底层数据只拉一次，
   否则点两下就把配额打光。
3. **错误结构化返回。** 工具失败时返回 `{"error": ...}` 而不是抛异常打断循环，
   让模型有机会自己纠正（换参数、换工具）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import metrics
from .config import ROOT
from .github import GitHubClient
from .knowledge import KnowledgeBase

_WINDOW_NOTE = "分析窗口由会话决定，工具本身不接受时间参数。"


def _window_start(days: int) -> datetime:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ToolContext:
    """一次分析会话的共享上下文，内部缓存原始数据。"""

    client: GitHubClient
    window_days: int = 180
    max_pages: int = 6
    knowledge_root: Path | None = None
    knowledge_seed: KnowledgeBase | None = None
    embedder: Any = None

    _repo: dict[str, Any] = field(default_factory=dict)
    _commits: dict[str, list] = field(default_factory=dict)
    _pulls: dict[str, list] = field(default_factory=dict)
    _index: dict[str, Any] = field(default_factory=dict)
    _knowledge: KnowledgeBase | None = field(default=None, repr=False)

    @property
    def knowledge(self) -> KnowledgeBase:
        if self._knowledge is None:
            if self.knowledge_seed is not None:
                self._knowledge = self.knowledge_seed
            else:
                self._knowledge = KnowledgeBase(
                    self.knowledge_root or (ROOT / "knowledge"), embedder=self.embedder
                )
            if self.embedder is not None and self._knowledge.embedder is None:
                self._knowledge.attach_embedder(self.embedder)
        return self._knowledge

    def repo_meta(self, owner: str, name: str) -> dict[str, Any]:
        key = f"{owner}/{name}"
        if key not in self._repo:
            self._repo[key] = self.client.repo(owner, name)
        return self._repo[key]

    def commits(self, owner: str, name: str) -> list[dict]:
        key = f"{owner}/{name}"
        if key not in self._commits:
            since = _iso(_window_start(self.window_days))
            self._commits[key] = self.client.commits(
                owner, name, since, max_pages=self.max_pages
            )
        return self._commits[key]

    def commits_in_window(self, owner: str, name: str) -> list[dict]:
        """按作者时间筛过窗口的提交。

        工具和报告必须用同一份样本，否则同一个仓库会给出两个数字
        （实测：工具报 600 条，报告报 299 条）。
        """
        since = _window_start(self.window_days)
        result: list[dict] = []
        for commit in self.commits(owner, name):
            stamp = ((commit.get("commit") or {}).get("author") or {}).get("date")
            moment = metrics.parse_time(stamp)
            if moment and moment >= since:
                result.append(commit)
        return result

    def repo_index(self, owner: str, name: str):
        """仓库知识索引：目录树 + 关键文件，按需构建一次。"""
        key = f"{owner}/{name}"
        if key not in self._index:
            from .repo_index import build_index

            meta = self.repo_meta(owner, name)
            self._index[key] = build_index(
                self.client, owner, name, meta.get("default_branch")
            )
        return self._index[key]

    def pulls(self, owner: str, name: str) -> list[dict]:
        key = f"{owner}/{name}"
        if key not in self._pulls:
            since = _iso(_window_start(self.window_days))
            if self.client.token:
                self._pulls[key] = self.client.pulls_via_graphql(
                    owner, name, max_pages=self.max_pages, since=since
                )
            else:
                self._pulls[key] = self.client.pulls(
                    owner, name, max_pages=max(1, self.max_pages - 1)
                )
        return self._pulls[key]


# ---------------------------------------------------------------- 工具实现

def repo_overview(ctx: ToolContext, owner: str, name: str) -> dict[str, Any]:
    meta = ctx.repo_meta(owner, name)
    return {
        "full_name": meta.get("full_name"),
        "description": meta.get("description"),
        "language": meta.get("language"),
        "stars": meta.get("stargazers_count"),
        "forks": meta.get("forks_count"),
        "open_issues": meta.get("open_issues_count"),
        "created_at": meta.get("created_at"),
        "pushed_at": meta.get("pushed_at"),
    }


def commit_activity(ctx: ToolContext, owner: str, name: str) -> dict[str, Any]:
    commits = ctx.commits(owner, name)
    # 传入 since，才能识别「翻页截断」并按作者时间筛窗口
    data = metrics.commit_activity(commits, ctx.window_days, _window_start(ctx.window_days))
    data["window_days"] = ctx.window_days
    return data


def pull_health(ctx: ToolContext, owner: str, name: str) -> dict[str, Any]:
    pulls = ctx.pulls(owner, name)
    since = _window_start(ctx.window_days)
    return metrics.pull_stats(pulls, since)


def contributor_structure(ctx: ToolContext, owner: str, name: str) -> dict[str, Any]:
    return metrics.bus_factor(ctx.commits_in_window(owner, name))


def commit_convention(ctx: ToolContext, owner: str, name: str) -> dict[str, Any]:
    commits = ctx.commits_in_window(owner, name)
    return {
        "convention": metrics.convention_stats(commits),
        "reverts": metrics.revert_stats(commits),
    }


def search_knowledge(ctx: ToolContext, query: str, top_k: int = 3) -> dict[str, Any]:
    base = ctx.knowledge
    hits = base.search(query, top_k)
    return {
        "query": query,
        "corpus": base.stats(),
        "results": hits,
        "note": "引用时请使用 source 字段，例如 [来源: git-convention.md#格式]" if hits
                else "知识库中没有匹配内容，不要编造规范条文。",
    }


def repo_structure(ctx: ToolContext, owner: str, name: str) -> dict[str, Any]:
    """Java 工程结构：构建工具、模块划分、测试比例、依赖规模。"""
    index = ctx.repo_index(owner, name)
    return {
        "java": index.structure.to_dict(),
        "indexed_files": index.stats()["paths"],
    }


def search_repo(
    ctx: ToolContext, owner: str, name: str, query: str, top_k: int = 3
) -> dict[str, Any]:
    """在仓库自己的文档（README / 构建脚本 / CI 配置）里检索。"""
    index = ctx.repo_index(owner, name)
    base = index.knowledge
    hits = base.search(query, top_k) if base else []
    return {
        "query": query,
        "corpus": base.stats() if base else {"documents": 0, "chunks": 0},
        "results": hits,
        "note": "这是该仓库自身的文档，不是团队规范；引用时用 source 字段标注路径。"
                if hits else "仓库文档里没有匹配内容，不要编造。",
    }


# ---------------------------------------------------------------- 注册表

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict[str, Any]]


_REPO_PARAMS = {
    "type": "object",
    "properties": {
        "owner": {"type": "string", "description": "仓库所属用户或组织，例如 pallets"},
        "name": {"type": "string", "description": "仓库名，例如 click"},
    },
    "required": ["owner", "name"],
    "additionalProperties": False,
}


_KNOWLEDGE_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索关键词，例如「提交规范 类型」"},
        "top_k": {"type": "integer", "description": "返回条数，默认 3"},
    },
    "required": ["query"],
    "additionalProperties": False,
}


_REPO_SEARCH_PARAMS = {
    "type": "object",
    "properties": {
        "owner": {"type": "string", "description": "仓库所属用户或组织"},
        "name": {"type": "string", "description": "仓库名"},
        "query": {"type": "string", "description": "检索关键词，例如「构建 依赖 版本」"},
        "top_k": {"type": "integer", "description": "返回条数，默认 3"},
    },
    "required": ["owner", "name", "query"],
    "additionalProperties": False,
}


class ToolRegistry:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._tools: dict[str, Tool] = {}
        self.calls: list[dict[str, Any]] = []

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def call(self, name: str, raw_arguments: str) -> tuple[str, bool]:
        """返回 (给模型看的观察结果, 是否出错)。出错不抛异常，让模型自己纠。"""
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"不存在的工具：{name}"}, ensure_ascii=False), True

        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"参数不是合法 JSON：{exc}"}, ensure_ascii=False), True

        missing = [k for k in tool.parameters.get("required", []) if k not in arguments]
        if missing:
            return json.dumps(
                {"error": f"缺少必填参数：{', '.join(missing)}"}, ensure_ascii=False
            ), True

        try:
            result = tool.handler(self.ctx, **arguments)
            self.calls.append({"tool": name, "arguments": arguments, "ok": True})
            return json.dumps(result, ensure_ascii=False, default=str), False
        except Exception as exc:  # 网络、数据格式等，都交给模型判断
            self.calls.append({"tool": name, "arguments": arguments, "ok": False,
                               "error": str(exc)})
            return json.dumps({"error": str(exc)}, ensure_ascii=False), True


def build_registry(ctx: ToolContext) -> ToolRegistry:
    registry = ToolRegistry(ctx)
    registry.register(Tool(
        name="repo_overview",
        description="获取仓库基本信息：语言、星标数、未关闭 issue 数、最近推送时间。"
                    "适合作为分析的起点，先确认仓库规模。",
        parameters=_REPO_PARAMS,
        handler=repo_overview,
    ))
    registry.register(Tool(
        name="commit_activity",
        description="提交节奏：总提交数、周均提交、星期分布、24 小时分布、夜间提交占比。"
                    f"回答「团队节奏快不快、有没有加班迹象」这类问题。{_WINDOW_NOTE}",
        parameters=_REPO_PARAMS,
        handler=commit_activity,
    ))
    registry.register(Tool(
        name="pull_health",
        description="PR 协作健康度：新建/合并/未合并数量、合并周期中位数与 P90、"
                    "平均评审记录数、无评审即合并的比例。"
                    "注意：若数据源不含评审记录，相关字段返回 null，此时不要下结论。",
        parameters=_REPO_PARAMS,
        handler=pull_health,
    ))
    registry.register(Tool(
        name="contributor_structure",
        description="贡献者结构：活跃人数、提交量最高者占比、巴士因子（需要几个人才能覆盖一半提交）。"
                    "巴士因子为 1 表示存在关键人依赖风险。",
        parameters=_REPO_PARAMS,
        handler=contributor_structure,
    ))
    registry.register(Tool(
        name="commit_convention",
        description="提交规范执行情况：符合 Conventional Commits 的比例、违规样本，以及 Revert 占比。",
        parameters=_REPO_PARAMS,
        handler=commit_convention,
    ))
    registry.register(Tool(
        name="search_knowledge",
        description="检索团队规范文档（提交规范、评审清单、分支策略），返回带 source 标注的原文片段。"
                    "凡是给出「应该怎么做」类建议之前，都应先查这里，并引用 source。",
        parameters=_KNOWLEDGE_PARAMS,
        handler=search_knowledge,
    ))
    registry.register(Tool(
        name="repo_structure",
        description="Java 工程结构：构建工具（Maven/Gradle）、模块划分、"
                    "主代码与测试代码的类文件数量、测试比例、依赖规模、Java 版本。"
                    "分析 Java 仓库时应优先调用它，判断工程是否健康。",
        parameters=_REPO_PARAMS,
        handler=repo_structure,
    ))
    registry.register(Tool(
        name="search_repo",
        description="在**该仓库自己的**文档里检索：README、pom.xml / build.gradle、"
                    "CI 配置、docs 目录。回答「这个项目用什么构建工具」「依赖怎么管的」"
                    "这类问题应查这里，而不是查团队规范。",
        parameters=_REPO_SEARCH_PARAMS,
        handler=search_repo,
    ))
    return registry
