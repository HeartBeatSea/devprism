"""导出一份离线演示数据。

用法：
    python scripts/export_demo.py google/gson --days 180 --out demo/gson.json

只保留分析链路真正读到的字段——GitHub 的原始提交对象一次几百 KB，
整份塞进仓库既难看又没必要。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from devprism.cache import HttpCache  # noqa: E402
from devprism.config import load_settings  # noqa: E402
from devprism.fixture import FIXTURE_VERSION  # noqa: E402
from devprism.github import GitHubClient  # noqa: E402

MAX_DEMO_FILES = 6
MAX_FILE_BYTES = 40_000


def trim_tree(tree: list[dict]) -> list[dict]:
    return [
        {"path": item.get("path"), "type": item.get("type"), "size": item.get("size") or 0}
        for item in tree
        if item.get("path")
    ]


def trim_commit(commit: dict) -> dict:
    inner = commit.get("commit") or {}
    author = inner.get("author") or {}
    return {
        "sha": (commit.get("sha") or "")[:12],
        "author": {"login": (commit.get("author") or {}).get("login")},
        "commit": {
            "message": inner.get("message") or "",
            "author": {"date": author.get("date"), "name": author.get("name")},
        },
    }


def trim_pull(pull: dict) -> dict:
    return {
        "number": pull.get("number"),
        "state": pull.get("state"),
        "created_at": pull.get("created_at"),
        "merged_at": pull.get("merged_at"),
        "comments": pull.get("comments"),
        "review_count": pull.get("review_count"),
        "user": {"login": (pull.get("user") or {}).get("login")},
    }


def pick_files(client: GitHubClient, owner: str, name: str, tree: list[dict]) -> dict[str, str]:
    """挑几份文件让「点开节点读源码」这条链路离线也能演示。"""
    blobs = {
        item["path"]: item.get("size") or 0
        for item in tree
        if item.get("type") == "blob" and item.get("path")
    }
    wanted = ["README.md", "pom.xml", "build.gradle", "CONTRIBUTING.md"]
    wanted += [
        path for path, size in sorted(blobs.items(), key=lambda kv: kv[1])
        if path.endswith(".java") and "/src/main/" in path and size < MAX_FILE_BYTES
    ][:MAX_DEMO_FILES]

    files: dict[str, str] = {}
    for path in wanted:
        if len(files) >= MAX_DEMO_FILES or path in files:
            break
        text = client.file_text(owner, name, path)
        if text:
            files[path] = text[:MAX_FILE_BYTES]
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="导出离线演示数据")
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--out", default="demo/gson.json")
    args = parser.parse_args()

    owner, _, name = args.repo.partition("/")
    if not owner or not name:
        print("仓库格式应为 owner/name", file=sys.stderr)
        return 1

    settings = load_settings()
    cache = HttpCache(settings.cache_path)
    client = GitHubClient(settings.github_token, cache, settings.timeout, proxy=settings.proxy)
    try:
        repo = client.repo(owner, name)
        branch = repo.get("default_branch") or "HEAD"
        tree = client.tree(owner, name, branch)

        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        commits = client.commits(owner, name, since_iso, max_pages=6)
        pulls = (
            client.pulls_via_graphql(owner, name, max_pages=3, since=since_iso)
            if settings.github_token
            else client.pulls(owner, name, max_pages=3, since=since_iso)
        )

        payload = {
            "version": FIXTURE_VERSION,
            "source": {
                "repo": f"{owner}/{name}",
                "days": args.days,
                "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "commit_count": len(commits),
            },
            "repo": {
                "full_name": repo.get("full_name") or f"{owner}/{name}",
                "description": repo.get("description"),
                "language": repo.get("language"),
                "stargazers_count": repo.get("stargazers_count"),
                "forks_count": repo.get("forks_count"),
                "open_issues_count": repo.get("open_issues_count"),
                "pushed_at": repo.get("pushed_at"),
                "default_branch": branch,
                "size": repo.get("size"),
            },
            "tree": trim_tree(tree),
            "tree_truncated": bool(getattr(client, "tree_truncated", False)),
            "commits": [trim_commit(item) for item in commits],
            "pulls": [trim_pull(item) for item in pulls],
            "files": pick_files(client, owner, name, tree),
        }
    finally:
        cache.close()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    size_kb = out.stat().st_size / 1024
    print(f"已导出 {out}")
    print(f"  提交 {len(payload['commits'])} 条，PR {len(payload['pulls'])} 个，"
          f"目录 {len(payload['tree'])} 项，文件 {len(payload['files'])} 份")
    print(f"  体积 {size_kb:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
