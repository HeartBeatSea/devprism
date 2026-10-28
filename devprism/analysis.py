"""统一采集与分析服务：一次运行只固定一个时间窗口和一份原始快照。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from . import metrics
from .github import GitHubClient
from .snapshot import AnalysisSnapshot, Window


class AnalysisService:
    def __init__(
        self,
        client: GitHubClient,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.emit = on_event or (lambda event: None)
        self.snapshots: dict[str, AnalysisSnapshot] = {}

    def _coverage(self, result: Any) -> dict[str, Any]:
        return {
            "complete": result.window_complete,
            "pages": result.pages,
            "fetched": len(result.items),
            "stop_reason": result.stop_reason,
        }

    def analyze(
        self,
        owner: str,
        name: str,
        days: int = 180,
        max_pages: int = 6,
        now: datetime | None = None,
    ) -> AnalysisSnapshot:
        until = now or datetime.now(timezone.utc)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        window = Window.ending_at(until, days)
        full_name = f"{owner}/{name}"
        self.emit({"type": "analysis_start", "repo": full_name,
                   "since": window.since.isoformat(), "until": window.until.isoformat()})
        before_requests = self.client.requests_made
        before_hits = self.client.cache_hits

        self.emit({"type": "fetch_start", "resource": "repo", "repo": full_name})
        repo = self.client.repo(owner, name)
        self.emit({"type": "fetch_done", "resource": "repo", "repo": full_name})

        self.emit({"type": "fetch_start", "resource": "commits", "repo": full_name})
        commit_result = self.client.commits(
            owner, name, window.since_iso, max_pages=max_pages, until_iso=window.until_iso
        )
        self.emit({"type": "fetch_done", "resource": "commits", "repo": full_name,
                   "fetched": len(commit_result.items), "pages": commit_result.pages,
                   "complete": commit_result.window_complete,
                   "stop_reason": commit_result.stop_reason})

        self.emit({"type": "fetch_start", "resource": "pulls", "repo": full_name})
        if self.client.token:
            pull_result = self.client.pulls_via_graphql(
                owner, name, max_pages=max_pages, since=window.since_iso
            )
            pull_path = f"/graphql/{owner}/{name}/pullRequests"
        else:
            pull_result = self.client.pulls(
                owner, name, max_pages=max_pages, since=window.since_iso
            )
            pull_path = f"/repos/{owner}/{name}/pulls"
        self.emit({"type": "fetch_done", "resource": "pulls", "repo": full_name,
                   "fetched": len(pull_result.items), "pages": pull_result.pages,
                   "complete": pull_result.window_complete,
                   "stop_reason": pull_result.stop_reason})

        self.emit({"type": "summarize_start", "repo": full_name})
        summary = metrics.summarize(
            repo,
            commit_result.items,
            pull_result.items,
            days,
            until,
            since=window.since,
            commits_complete=commit_result.window_complete,
            pulls_complete=pull_result.window_complete,
        )
        coverage = {
            "commits": self._coverage(commit_result),
            "pulls": self._coverage(pull_result),
        }
        truncated: list[str] = []
        if not commit_result.window_complete:
            truncated.append(f"/repos/{owner}/{name}/commits")
        if not pull_result.window_complete:
            truncated.append(pull_path)
        source = {
            "requests": self.client.requests_made - before_requests,
            "cache_hits": self.client.cache_hits - before_hits,
            "remaining": self.client.remaining,
            "truncated": truncated,
        }
        identity = {
            "repo": full_name,
            "since": window.since.isoformat(),
            "until": window.until.isoformat(),
            "max_pages": max_pages,
            "repo_data": repo,
            "commits": commit_result.items,
            "pulls": pull_result.items,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20]
        summary.update({
            "snapshot_id": snapshot_id,
            "source": source,
            "coverage": coverage,
            "since": window.since.isoformat(),
            "until": window.until.isoformat(),
        })
        snapshot = AnalysisSnapshot(
            snapshot_id=snapshot_id,
            owner=owner,
            name=name,
            window=window,
            repo=repo,
            commits=commit_result.items,
            pulls=pull_result.items,
            summary=summary,
            source=source,
            coverage=coverage,
        )
        self.snapshots[snapshot_id] = snapshot
        self.emit({"type": "snapshot_done", "repo": full_name, "snapshot_id": snapshot_id,
                   "coverage": coverage})
        return snapshot

    def get(self, snapshot_id: str) -> AnalysisSnapshot | None:
        return self.snapshots.get(snapshot_id)
