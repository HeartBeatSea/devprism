"""GitHub REST API 客户端。

设计要点：
  * 只用标准库（urllib），零依赖，方便在受限环境跑通；
  * 所有请求经 HttpCache，304 不消耗配额；
  * 只调用「列表型」接口，绝不按仓库对象逐个请求（避免 N+1 打爆 60 次/小时限额）。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .cache import HttpCache
from .net import build_opener, normalize as normalize_proxy
from .snapshot import FetchResult

API_ROOT = "https://api.github.com"
GRAPHQL_ROOT = "https://api.github.com/graphql"

_PULLS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $cursor,
                 orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        createdAt
        mergedAt
        state
        isDraft
        author { login }
        comments { totalCount }
        reviews { totalCount }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    """GitHub 调用失败。"""


class RateLimitError(GitHubError):
    """配额耗尽。这条提示是给用户看的，必须能指导他行动。"""

    def __init__(self, reset_at: int) -> None:
        self.reset_at = reset_at
        wait = max(0, reset_at - int(time.time()))
        minutes, seconds = divmod(wait, 60)
        super().__init__(
            f"GitHub API 配额已用尽，还需等待 {minutes} 分 {seconds} 秒。"
            "配置 GITHUB_TOKEN 可把 60 次/小时提升到 5000 次/小时。"
        )


class GitHubClient:
    def __init__(
        self,
        token: str | None,
        cache: HttpCache,
        timeout: float = 20.0,
        proxy: str | None = None,
    ) -> None:
        self.token = token
        self.cache = cache
        self.timeout = timeout
        self.proxy = normalize_proxy(proxy)
        self._opener = build_opener(proxy)
        self.requests_made = 0
        self.cache_hits = 0
        self.stale_hits = 0
        self.tree_truncated = False
        self.remaining: int | None = None
        self.truncated: set[str] = set()
        # 缓存键必须带上身份指纹：否则换 token 之后仍会读到未认证时期的旧响应，
        # 表现为「明明配了 token，配额还是 60」。
        scope = hashlib.sha256((token or "anonymous").encode("utf-8")).hexdigest()[:12]
        self._cache_scope = scope

    # ---------- 底层 ----------

    def _headers(self, etag: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": "DevPrism/0.1",
            "Accept": "application/vnd.github+json",
            # 必须显式要 gzip：PR 列表单页可达 1.5 MB，不压缩时经常被代理截断
            "Accept-Encoding": "gzip",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def get(
        self, path: str, params: dict[str, Any] | None = None, use_cache: bool = True
    ) -> Any:
        url = API_ROOT + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        cache_key = f"{self._cache_scope}:{url}"
        cached = self.cache.get(cache_key) if use_cache else None
        if cached is not None and cached[1] is None:
            # 无 ETag 的缓存只能靠 TTL 判定，这里直接复用
            self.cache_hits += 1
            return cached[0]

        # 网络失败时的兜底：即使超过 TTL 也要能拿到上次的数据，
        # 否则断网就没法演示、没法复现，缓存等于白建。
        fallback = cached
        if fallback is None and use_cache and hasattr(self.cache, "peek"):
            fallback = self.cache.peek(cache_key)

        etag = cached[1] if cached else None
        last_error: Exception | None = None

        for _ in range(3):
            request = urllib.request.Request(url, headers=self._headers(etag))
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    self._track_budget(response.headers)
                    payload = json.loads(body) if body else None
                    if use_cache:
                        self.cache.put(cache_key, payload, response.headers.get("ETag"))
                    self.requests_made += 1
                    return payload
            except http.client.IncompleteRead as exc:
                # 大响应在弱网/代理下会被截断，退避后重试
                last_error = exc
                if self._fallback_to_cache(fallback):
                    return fallback[0]  # type: ignore[index]
                time.sleep(1)
                continue
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and cached is not None:
                    self.cache.touch(cache_key)
                    self.cache_hits += 1
                    return cached[0]
                if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                    raise RateLimitError(int(exc.headers.get("X-RateLimit-Reset") or 0)) from exc
                if exc.code == 404:
                    raise GitHubError(f"找不到资源（404）：{path}") from exc
                raise GitHubError(f"GitHub API 返回 {exc.code}：{path}") from exc
            except urllib.error.URLError as exc:
                # 弱网或中间代理下 TLS 握手会偶发失败。
                # 有缓存就先用缓存，别因为一次握手失败让整次分析白跑。
                last_error = exc
                if self._fallback_to_cache(fallback):
                    return fallback[0]  # type: ignore[index]
                time.sleep(1)
                continue

        raise GitHubError(f"网络请求连续失败，已重试 3 次：{path}（{last_error}）")

    def _fallback_to_cache(self, cached: tuple[Any, Any] | None) -> bool:
        """网络失败时退回旧缓存。调用方负责返回 cached[0]。"""
        if cached is None:
            return False
        self.cache_hits += 1
        self.stale_hits += 1
        return True

    def _track_budget(self, headers: Any) -> None:
        value = headers.get("X-RateLimit-Remaining")
        if value is not None:
            try:
                self.remaining = int(value)
            except ValueError:
                pass

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_pages: int = 3,
        per_page: int = 100,
        boundary_since: str | None = None,
    ) -> FetchResult:
        """按页拉取并明确记录停止原因，不让指标层猜测数据是否完整。"""
        query = dict(params or {})
        query["per_page"] = per_page
        collected: list[dict[str, Any]] = []
        pages = 0
        stop_reason = "max_pages"
        complete = False
        for page in range(1, max_pages + 1):
            query["page"] = page
            batch = self.get(path, query)
            pages += 1
            if not batch:
                stop_reason = "exhausted"
                complete = True
                break
            crossed_boundary = False
            for item in batch:
                created = item.get("created_at") if boundary_since else None
                if boundary_since and created and created < boundary_since:
                    crossed_boundary = True
                    break
                collected.append(item)
            if crossed_boundary:
                stop_reason = "window_boundary"
                complete = True
                break
            if len(batch) < per_page:
                stop_reason = "exhausted"
                complete = True
                break
        if not complete:
            self.truncated.add(path)
        return FetchResult(collected, pages, complete, stop_reason)

    # ---------- GraphQL ----------

    def graphql(
        self, query: str, variables: dict[str, Any] | None = None, use_cache: bool = True
    ) -> dict[str, Any]:
        if not self.token:
            raise GitHubError("GraphQL 需要配置 GITHUB_TOKEN")

        # 与 REST 共用同一套缓存；POST 没有 ETag，只能靠 TTL 过期
        digest = hashlib.sha256(
            (query + json.dumps(variables or {}, sort_keys=True)).encode("utf-8")
        ).hexdigest()[:16]
        cache_key = f"{self._cache_scope}:gql:{digest}"

        cached = self.cache.get(cache_key) if use_cache else None
        if cached is not None:
            self.cache_hits += 1
            return cached[0]

        request = urllib.request.Request(
            GRAPHQL_ROOT,
            data=json.dumps({"query": query, "variables": variables or {}}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "DevPrism/0.1",
                "Accept-Encoding": "gzip",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                self.requests_made += 1
                payload = json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            raise GitHubError(f"GraphQL 返回 {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"GraphQL 网络失败：{exc.reason}") from exc

        if payload.get("errors"):
            raise GitHubError(f"GraphQL 查询出错：{payload['errors'][0].get('message')}")

        if use_cache:
            self.cache.put(cache_key, payload, None)
        return payload

    def pulls_via_graphql(
        self, owner: str, name: str, max_pages: int = 3, since: str | None = None
    ) -> FetchResult:
        """按创建时间倒序采集 PR，越过窗口边界后立即停止。"""
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        stop_reason = "max_pages"
        complete = False

        for _ in range(max_pages):
            payload = self.graphql(
                _PULLS_QUERY, {"owner": owner, "name": name, "cursor": cursor}
            )
            pages += 1
            repository = ((payload.get("data") or {}).get("repository")) or {}
            connection = repository.get("pullRequests") or {}
            nodes = connection.get("nodes") or []
            crossed_boundary = False

            for node in nodes:
                created = node.get("createdAt")
                if since and created and created < since:
                    crossed_boundary = True
                    break
                collected.append({
                    "number": node.get("number"),
                    "created_at": created,
                    "merged_at": node.get("mergedAt"),
                    "state": (node.get("state") or "").lower(),
                    "draft": node.get("isDraft"),
                    "author": node.get("author") or {},
                    "comments": (node.get("comments") or {}).get("totalCount", 0),
                    "review_count": (node.get("reviews") or {}).get("totalCount", 0),
                })

            info = connection.get("pageInfo") or {}
            if crossed_boundary:
                stop_reason = "window_boundary"
                complete = True
                break
            if not info.get("hasNextPage"):
                stop_reason = "exhausted"
                complete = True
                break
            cursor = info.get("endCursor")

        if not complete:
            self.truncated.add(f"/graphql/{owner}/{name}/pullRequests")
        return FetchResult(collected, pages, complete, stop_reason)

    # ---------- 领域接口 ----------

    def repo(self, owner: str, name: str) -> dict[str, Any]:
        return self.get(f"/repos/{owner}/{name}")

    def file_text(self, owner: str, name: str, path: str) -> str | None:
        """读取仓库中的单个文本文件；不存在或无权限时返回 None。"""
        try:
            payload = self.get(f"/repos/{owner}/{name}/contents/{path}")
        except GitHubError:
            return None
        if not isinstance(payload, dict) or not payload.get("content"):
            return None
        try:
            return base64.b64decode(payload["content"]).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None

    def tree(self, owner: str, name: str, ref: str = "HEAD") -> list[dict]:
        """一次请求拿到完整目录树。

        Java 项目的工程结构几乎都能从路径推出来（src/main/java、src/test/java、
        pom.xml、模块子目录），所以这一步性价比极高：1 次请求换全量结构信息。
        """
        payload = self.get(
            f"/repos/{owner}/{name}/git/trees/{ref}", {"recursive": 1}
        )
        if not isinstance(payload, dict):
            return []
        # 超大仓库的递归树会被 GitHub 截断，这时区域统计只是下限，必须如实标注
        self.tree_truncated = bool(payload.get("truncated"))
        return payload.get("tree") or []

    def commits(
        self,
        owner: str,
        name: str,
        since_iso: str,
        max_pages: int = 3,
        until_iso: str | None = None,
    ) -> FetchResult:
        return self.paginate(
            f"/repos/{owner}/{name}/commits",
            {"since": since_iso, "until": until_iso},
            max_pages=max_pages,
        )

    def pulls(
        self,
        owner: str,
        name: str,
        max_pages: int = 2,
        per_page: int = 50,
        since: str | None = None,
    ) -> FetchResult:
        # 按创建时间倒序；碰到窗口之前的 PR 即证明窗口采集完整。
        return self.paginate(
            f"/repos/{owner}/{name}/pulls",
            {"state": "all", "sort": "created", "direction": "desc"},
            max_pages=max_pages,
            per_page=per_page,
            boundary_since=since,
        )

    # 刻意不实现 /stats/contributors：该接口返回全量周粒度数据，实测单仓库可达
    # 2 MB 以上，既浪费配额又易被代理截断。贡献者分布直接用 commits 列表计算。

    def raw_text(
        self, owner: str, name: str, path: str, limit: int = 200_000, attempts: int = 2
    ) -> str | None:
        """从 raw.githubusercontent.com 直接取文件正文。

        contents API 和 raw 是**不同域名**，网络里经常一个通、一个不通；
        而且 raw 不消耗 API 配额，读取文件时用它做兜底很划算。
        """
        url = (
            "https://raw.githubusercontent.com/"
            f"{owner}/{name}/HEAD/{urllib.parse.quote(path)}"
        )
        last_error: Exception | None = None
        for _ in range(attempts):
            request = urllib.request.Request(
                url, headers={"User-Agent": "devprism/0.1"}
            )
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read(limit + 1)
                return body[:limit].decode("utf-8", "replace")
            except (urllib.error.URLError, http.client.IncompleteRead, OSError) as exc:
                last_error = exc
                time.sleep(0.8)
        if last_error:
            return None
        return None
