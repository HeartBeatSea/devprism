"""把目录树压成一张可绘制的结构图。

**为什么不读源码**

一个中等 Java 仓库就有几百个文件，逐个读取会瞬间打爆 API 配额。
但目录树一次请求就拿到了全部路径和文件大小——层级、体量、角色都能算出来。
所以这里只做「结构」，语义留给按需的 `symbols` + LLM 解释。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .repo_index import classify_area

MAX_NODES = 320          # 图太大前端画不动，也看不清
MAX_DIR_CHILDREN = 5     # 每个节点最多画几个子目录
MAX_FILE_CHILDREN = 7    # 再留几个名额给文件，否则核心里全是目录、看不到代码
MAX_DEPTH = 3            # 根 → 模块 → 子目录 → 文件

# 「核心代码」= 真正承载功能的源码。文档、配置、CI、图片、测试都不画进星系——
# 测试类通常比主代码还多，混在一起会把功能单元淹掉。
CODE_AREAS = ("主代码",)
FOCUS_VALUES = ("code", "all")


@dataclass
class GraphNode:
    id: str
    label: str
    kind: str              # root | dir | file | rest
    depth: int
    parent: str | None
    bytes: int = 0
    files: int = 0
    area: str = "其他"
    children: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "depth": self.depth,
            "parent": self.parent,
            "bytes": self.bytes,
            "files": self.files,
            "area": self.area,
        }


def _aggregate(tree: list[dict], focus: str = "code") -> tuple[dict[str, dict[str, int]], int]:
    """统计每个目录的体量与文件数（含全部子孙）。

    返回 (统计, 被跳过的文件数)。focus=code 时只统计代码文件，
    目录的体量也随之只反映代码，避免「一个巨大的 docs 目录」把星系撑歪。
    """
    stats: dict[str, dict[str, int]] = {}
    skipped = 0
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path") or ""
        if not path:
            continue
        if focus == "code" and classify_area(path) not in CODE_AREAS:
            skipped += 1
            continue
        size = int(entry.get("size") or 0)
        parts = path.split("/")
        stats.setdefault("", {"bytes": 0, "files": 0})
        stats[""]["bytes"] += size
        stats[""]["files"] += 1
        for i in range(1, len(parts)):
            directory = "/".join(parts[:i])
            slot = stats.setdefault(directory, {"bytes": 0, "files": 0})
            slot["bytes"] += size
            slot["files"] += 1
    return stats, skipped


def build_graph(
    repo: str,
    tree: list[dict],
    focus: str = "code",
    max_nodes: int = MAX_NODES,
) -> dict[str, Any]:
    """返回 {nodes: [...], truncated: bool, total_files: int, hidden_files: int}。"""
    focus = focus if focus in FOCUS_VALUES else "code"
    stats, skipped = _aggregate(tree, focus)
    if not stats or not stats.get(""):
        return {"nodes": [], "truncated": False, "total_files": 0, "hidden_files": skipped, "focus": focus}

    root_label = repo.split("/")[-1] or repo
    nodes: dict[str, GraphNode] = {
        "": GraphNode("", root_label, "root", 0, None,
                      stats[""]["bytes"], stats[""]["files"], "其他")
    }

    # 直接子节点：本层目录 + 本层文件
    children_of: dict[str, list[tuple[str, int, int, str]]] = {}
    included_files: list[tuple[str, int]] = []
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path") or ""
        if not path:
            continue
        if focus == "code" and classify_area(path) not in CODE_AREAS:
            continue
        size = int(entry.get("size") or 0)
        included_files.append((path, size))
        parts = path.split("/")
        parent = "/".join(parts[:-1])
        if len(parts) - 1 > MAX_DEPTH:
            continue
        children_of.setdefault(parent, []).append((path, size, 1, "file"))
    for directory, slot in stats.items():
        if not directory:
            continue
        parent = "/".join(directory.split("/")[:-1])
        children_of.setdefault(parent, []).append(
            (directory, slot["bytes"], slot["files"], "dir")
        )

    truncated = False
    queue = [""]
    while queue:
        parent_id = queue.pop(0)
        parent = nodes.get(parent_id)
        if parent is None:
            continue
        candidates = children_of.get(parent_id, [])
        if not candidates:
            continue

        dirs = sorted([c for c in candidates if c[3] == "dir"], key=lambda c: c[1], reverse=True)
        files = sorted([c for c in candidates if c[3] == "file"], key=lambda c: c[1], reverse=True)

        # 到了深度上限就不再往下展开，否则深目录会把星系拉成一条细线。
        # 但 Java 的类基本都埋在 src/main/java/... 第五层以下，直接丢掉就只剩空壳目录，
        # 所以这里把该目录下最大的几个代码文件挂上来，让星系里有真正的代码单位。
        if parent.depth >= MAX_DEPTH:
            prefix = f"{parent_id}/" if parent_id else ""
            deep = [
                (path, size) for path, size in included_files
                if path != parent_id and path.startswith(prefix)
            ]
            deep.sort(key=lambda item: item[1], reverse=True)
            for path, size in deep[:MAX_FILE_CHILDREN]:
                if len(nodes) >= max_nodes:
                    truncated = True
                    break
                if path in nodes:
                    continue
                nodes[path] = GraphNode(
                    id=path,
                    label=path.rsplit("/", 1)[-1],
                    kind="file",
                    depth=parent.depth + 1,
                    parent=parent_id,
                    bytes=size,
                    files=1,
                    area=classify_area(path),
                )
                parent.children.append(path)
            if len(deep) > MAX_FILE_CHILDREN or parent.children:
                truncated = True
            continue

        keep: list[tuple[str, int, int, str]] = []
        keep.extend(dirs[:MAX_DIR_CHILDREN])
        keep.extend(files[:MAX_FILE_CHILDREN])

        dropped = len(candidates) - len(keep)
        if dropped > 0:
            truncated = True

        for path, size, count, kind in keep:
            if len(nodes) >= max_nodes:
                truncated = True
                break
            label = path.rsplit("/", 1)[-1]
            node_id = path
            nodes[node_id] = GraphNode(
                id=node_id,
                label=label,
                kind=kind,
                depth=parent.depth + 1,
                parent=parent_id,
                bytes=size,
                files=count,
                area=classify_area(path) if kind == "file" else "其他",
            )
            parent.children.append(node_id)
            # 深度上限处的目录也要入队：弹出时才能发现「下面还有内容没展开」并标记截断
            if kind == "dir" and parent.depth + 1 <= MAX_DEPTH:
                queue.append(node_id)

        if dropped > 0 and parent.children:
            rest_id = f"{parent_id}#rest"
            if len(nodes) < max_nodes:
                nodes[rest_id] = GraphNode(
                    rest_id, f"其余 {dropped} 项", "rest",
                    parent.depth + 1, parent_id,
                    sum(c[1] for c in candidates if c[0] not in parent.children),
                    sum(c[2] for c in candidates if c[0] not in parent.children),
                    "其他",
                )
                parent.children.append(rest_id)

    return {
        "nodes": [node.to_dict() for node in nodes.values()],
        "truncated": truncated,
        "total_files": stats[""]["files"],
        "hidden_files": skipped,
        "focus": focus,
    }
