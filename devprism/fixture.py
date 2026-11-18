"""离线快照客户端：用一份本地 JSON 顶替 GitHub。

用途有两个：
  1. 演示和录屏不依赖网络。这台机器访问 github.com 时通时断，现场演示翻车一次就够呛。
  2. 别人 clone 下来不用配 token 就能看到完整效果，降低上手门槛。

只实现分析链路真正用到的那几个方法，其余一律不做——保持它是"替身"而不是第二套实现。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .snapshot import FetchResult

FIXTURE_VERSION = 1


def _author_date(commit: dict[str, Any]) -> str:
    return (
        (commit.get("commit") or {}).get("author", {}).get("date")
        or (commit.get("commit") or {}).get("committer", {}).get("date")
        or ""
    )


def _created_at(pull: dict[str, Any]) -> str:
    return pull.get("created_at") or ""


class FixtureClient:
    """读本地快照，接口和 GitHubClient 保持一致。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"找不到演示数据：{self.path}")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("version") != FIXTURE_VERSION:
            raise ValueError(f"演示数据版本不匹配：{data.get('version')}")

        self._repo: dict[str, Any] = data.get("repo") or {}
        self._tree: list[dict[str, Any]] = data.get("tree") or []
        self._commits: list[dict[str, Any]] = data.get("commits") or []
        self._pulls: list[dict[str, Any]] = data.get("pulls") or []
        self._files: dict[str, str] = data.get("files") or {}
        self.source = data.get("source", {})

        # 这些字段调用方会读，给成"离线无网络"的值
        self.requests_made = 0
        self.cache_hits = 0
        self.stale_hits = 0
        self.remaining: int | None = None
        self.truncated: set[str] = set()
        self.tree_truncated = bool(data.get("tree_truncated"))

    # ---------- 与 GitHubClient 对齐 ----------

    def now(self) -> datetime:
        """快照的时间基准＝导出时刻。

        分析窗口是按「现在」往前推 N 天算的。如果演示也用真实时间，
        快照放几天之后窗口就滑走了，提交数会越来越少，最后变成 0。
        把时间钉在导出那一刻，演示的数字才是稳定、可复现的。
        """
        exported = (self.source or {}).get("exported_at")
        if exported:
            try:
                return datetime.strptime(exported, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def repo(self, owner: str, name: str) -> dict[str, Any]:
        return self._repo

    def tree(self, owner: str, name: str, ref: str = "HEAD") -> list[dict[str, Any]]:
        return self._tree

    def file_text(self, owner: str, name: str, path: str) -> str | None:
        return self._files.get(path)

    def raw_text(self, owner: str, name: str, path: str, limit: int = 200_000) -> str | None:
        text = self._files.get(path)
        return text[:limit] if text else None

    def commits(
        self,
        owner: str,
        name: str,
        since_iso: str,
        max_pages: int = 3,
        until_iso: str | None = None,
    ) -> FetchResult:
        items = [item for item in self._commits if _author_date(item) >= since_iso]
        if until_iso:
            items = [item for item in items if _author_date(item) <= until_iso]
        return FetchResult(items=items, pages=1, window_complete=True, stop_reason="fixture")

    def pulls(
        self,
        owner: str,
        name: str,
        max_pages: int = 2,
        per_page: int = 50,
        since: str | None = None,
    ) -> FetchResult:
        items = list(self._pulls)
        if since:
            items = [item for item in items if _created_at(item) >= since]
        return FetchResult(items=items, pages=1, window_complete=True, stop_reason="fixture")

    def pulls_via_graphql(
        self, owner: str, name: str, max_pages: int = 3, since: str | None = None
    ) -> FetchResult:
        return self.pulls(owner, name, since=since)

    def get(self, path: str, params: dict[str, Any] | None = None, use_cache: bool = True) -> Any:
        raise RuntimeError("离线演示模式不支持任意接口调用，只支持快照里的数据")
