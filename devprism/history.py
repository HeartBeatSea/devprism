"""分析历史：按仓库分组，仓库下按会话（问题）保存。

设计取舍：
  * 用 SQLite 而不是一堆文件——要按仓库分组、按时间排序、按 id 取单条，
    这本质是查询问题，文件系统做起来反而更绕；
  * 每次分析完成自动落库，用户不需要点保存；
  * 会话内容（结论 Markdown + 轨迹/指标）整条存下来，回看时不重新请求网络。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    question    TEXT,
    days        INTEGER NOT NULL,
    conventions TEXT,
    created_at  REAL NOT NULL,
    markdown    TEXT,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_repo ON history(owner, name, created_at DESC);
"""

MAX_RECORDS = 300       # 全库上限
MAX_PER_REPO = 8        # 单个仓库最多留几条会话，避免一个仓库刷满侧栏


@dataclass
class HistoryRecord:
    id: str
    owner: str
    name: str
    kind: str
    title: str
    question: str | None
    days: int
    conventions: str | None
    created_at: float
    markdown: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def repo(self) -> str:
        return f"{self.owner}/{self.name}"

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repo": self.repo,
            "kind": self.kind,
            "title": self.title,
            "days": self.days,
            "conventions": self.conventions,
            "created_at": self.created_at,
        }


class HistoryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(
        self,
        owner: str,
        name: str,
        kind: str,
        title: str,
        days: int,
        markdown: str,
        payload: dict[str, Any] | None = None,
        question: str | None = None,
        conventions: str | None = None,
    ) -> str:
        """写入一条记录。同一个仓库、同一类内容只保留一条，重复的只更新时间。

        否则同一个问题问三次就会在侧栏堆三条，历史很快变成垃圾场。
        """
        existing = self._conn.execute(
            "SELECT id FROM history WHERE owner = ? AND name = ? AND kind = ?"
            " AND title = ? AND IFNULL(question, '') = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (owner, name, kind, title, question or ""),
        ).fetchone()

        payload_text = json.dumps(payload or {}, ensure_ascii=False)
        now = time.time()

        if existing:
            record_id = existing[0]
            self._conn.execute(
                "UPDATE history SET created_at = ?, days = ?, conventions = ?,"
                " markdown = ?, payload = ? WHERE id = ?",
                (now, days, conventions, markdown, payload_text, record_id),
            )
            self._conn.commit()
            self._trim(owner, name)
            return record_id

        record_id = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO history (id, owner, name, kind, title, question, days,"
            " conventions, created_at, markdown, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, owner, name, kind, title, question, days,
                conventions, now, markdown, payload_text,
            ),
        )
        self._conn.commit()
        self._trim(owner, name)
        return record_id

    def _trim(self, owner: str | None = None, name: str | None = None) -> None:
        """全库只保留最近 N 条；单个仓库再单独收紧。"""
        self._conn.execute(
            "DELETE FROM history WHERE id NOT IN"
            " (SELECT id FROM history ORDER BY created_at DESC LIMIT ?)",
            (MAX_RECORDS,),
        )
        if owner and name:
            self._conn.execute(
                "DELETE FROM history WHERE owner = ? AND name = ? AND id NOT IN"
                " (SELECT id FROM history WHERE owner = ? AND name = ?"
                "  ORDER BY created_at DESC LIMIT ?)",
                (owner, name, owner, name, MAX_PER_REPO),
            )
        self._conn.commit()

    def groups(self) -> list[dict[str, Any]]:
        """按仓库分组，组内按时间倒序。"""
        rows = self._conn.execute(
            "SELECT id, owner, name, kind, title, question, days, conventions, created_at"
            " FROM history ORDER BY created_at DESC"
        ).fetchall()

        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = HistoryRecord(
                id=row[0], owner=row[1], name=row[2], kind=row[3], title=row[4],
                question=row[5], days=row[6], conventions=row[7], created_at=row[8],
            )
            repo = record.repo
            bucket = grouped.setdefault(
                repo,
                {"repo": repo, "owner": record.owner, "name": record.name,
                 "count": 0, "last_at": record.created_at, "sessions": []},
            )
            bucket["count"] += 1
            bucket["sessions"].append(record.brief())

        return list(grouped.values())

    def get(self, record_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, owner, name, kind, title, question, days, conventions,"
            " created_at, markdown, payload FROM history WHERE id = ?",
            (record_id,),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[10] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return {
            "id": row[0], "repo": f"{row[1]}/{row[2]}", "owner": row[1], "name": row[2],
            "kind": row[3], "title": row[4], "question": row[5], "days": row[6],
            "conventions": row[7], "created_at": row[8], "markdown": row[9],
            "payload": payload,
        }

    def delete(self, record_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM history WHERE id = ?", (record_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_repo(self, owner: str, name: str) -> int:
        cursor = self._conn.execute(
            "DELETE FROM history WHERE owner = ? AND name = ?", (owner, name)
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()
