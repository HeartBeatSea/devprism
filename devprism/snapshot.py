"""分析窗口、采集结果与不可变分析快照的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime

    @classmethod
    def ending_at(cls, until: datetime, days: int) -> "Window":
        return cls(since=until - timedelta(days=days), until=until)

    @property
    def days(self) -> int:
        return int((self.until - self.since).total_seconds() // 86400)

    @property
    def since_iso(self) -> str:
        return self.since.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def until_iso(self) -> str:
        return self.until.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class FetchResult:
    items: list[dict[str, Any]]
    pages: int
    window_complete: bool
    stop_reason: str

    @property
    def fetched(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.items[index]


@dataclass
class AnalysisSnapshot:
    snapshot_id: str
    owner: str
    name: str
    window: Window
    repo: dict[str, Any]
    commits: list[dict[str, Any]]
    pulls: list[dict[str, Any]]
    summary: dict[str, Any]
    source: dict[str, Any]
    coverage: dict[str, dict[str, Any]]
    markdown: str | None = None
    narratives: dict[str, str] = field(default_factory=dict)
