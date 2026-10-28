"""HTTP 响应缓存。

GitHub 未认证调用只有 60 次/小时，因此省配额是硬需求。三条手段：
  1. 本地 SQLite 缓存，TTL 内直接返回，不发请求；
  2. 带 ETag 的条件请求，命中 304 时**不消耗配额**；
  3. 缓存按 URL 建索引，参数不同的请求互不覆盖。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    url        TEXT PRIMARY KEY,
    etag       TEXT,
    payload    TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""


class HttpCache:
    def __init__(self, path: Path, ttl: int = 3600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        self.ttl = ttl

    def get(self, url: str) -> tuple[Any, str | None] | None:
        row = self._conn.execute(
            "SELECT payload, etag, fetched_at FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None
        payload, etag, fetched_at = row
        if time.time() - fetched_at > self.ttl:
            return None
        return json.loads(payload), etag

    def put(self, url: str, payload: Any, etag: str | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO http_cache (url, etag, payload, fetched_at)"
            " VALUES (?, ?, ?, ?)",
            (url, etag, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self._conn.commit()

    def touch(self, url: str) -> None:
        """304 命中后刷新时间戳，避免每次都走条件请求。"""
        self._conn.execute(
            "UPDATE http_cache SET fetched_at = ? WHERE url = ?", (time.time(), url)
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
