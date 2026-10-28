"""把 GitHub 仓库当成知识库：目录树分析 + 关键文件索引。

**为什么不是"把仓库代码喂给模型"**
一个大仓几十万行，context 装不下；而且逐文件拉取会把 API 配额打光。
这里只做两件事：
  1. 用一次 Trees API 拿到完整目录树，从**路径**就能算出大量工程结构信息；
  2. 精选少量关键文件（构建脚本、README、CI 配置）拉正文，切分后进知识库。

**为什么专攻 Java**
Java 项目的工程结构高度规整：`src/main/java`、`src/test/java`、`pom.xml`、
`build.gradle`。路径本身就携带语义，不用读代码就能判断构建工具、模块划分、
测试覆盖的大致比例。通用仓库分析工具不会做这些。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .knowledge import KnowledgeBase

# 进入知识库的关键文件，按优先级排列
PRIORITY_FILES = (
    "README.md",
    "README.rst",
    "README",
    "CONTRIBUTING.md",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    ".editorconfig",
    "checkstyle.xml",
    "docs/README.md",
)

MAX_FILE_BYTES = 200_000     # 超过这个体积多半是二进制或锁文件，直接跳过
MAX_KB_CHARS = 24_000        # 进知识库时截断，避免单篇文档撑爆检索单元
MAX_INDEXED_FILES = 10

_JAVA_VERSION = re.compile(r"<(?:maven\.compiler\.source|java\.version|release)>\s*([\d.]+)")
_DEPENDENCY = re.compile(r"<dependency>")
_MODULE = re.compile(r"<module>\s*([^<]+?)\s*</module>")
_GRADLE_DEP = re.compile(r"^\s*(?:implementation|api|compileOnly|runtimeOnly)\s*\(", re.M)
_GRADLE_JAVA = re.compile(r"(?:sourceCompatibility|targetCompatibility|JavaVersion\.VERSION_)([\d_]+)")


@dataclass
class JavaStructure:
    """从目录树推断出的 Java 工程结构。"""

    total_entries: int = 0
    java_files: int = 0
    main_java: int = 0
    test_java: int = 0
    kotlin_files: int = 0
    build_tool: str | None = None
    modules: list[str] = field(default_factory=list)
    java_version: str | None = None
    dependencies: int | None = None
    ci_files: int = 0
    has_wrapper: bool = False
    lombok_used: bool = False

    @property
    def is_java(self) -> bool:
        return self.java_files > 0 or self.build_tool is not None

    @property
    def test_ratio(self) -> float | None:
        if self.main_java == 0:
            return None
        return round(self.test_java / self.main_java, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_java": self.is_java,
            "java_files": self.java_files,
            "main_java": self.main_java,
            "test_java": self.test_java,
            "test_ratio": self.test_ratio,
            "kotlin_files": self.kotlin_files,
            "build_tool": self.build_tool,
            "modules": self.modules[:20],
            "module_count": len(self.modules),
            "java_version": self.java_version,
            "dependencies": self.dependencies,
            "ci_files": self.ci_files,
            "has_wrapper": self.has_wrapper,
        }


def analyze_tree(tree: list[dict], file_texts: dict[str, str] | None = None) -> JavaStructure:
    """只用目录树 + 少量文件正文推断 Java 工程结构。纯函数，可离线测。"""
    texts = file_texts or {}
    structure = JavaStructure(total_entries=len(tree))

    paths = [entry.get("path", "") for entry in tree if entry.get("type") == "blob"]

    for path in paths:
        lowered = path.lower()
        if lowered.endswith(".java"):
            structure.java_files += 1
            if "/src/test/java/" in f"/{path}":
                structure.test_java += 1
            elif "/src/main/java/" in f"/{path}":
                structure.main_java += 1
        elif lowered.endswith(".kt"):
            structure.kotlin_files += 1

        if path.startswith(".github/workflows/") or lowered.endswith((".gitlab-ci.yml", "jenkinsfile")):
            structure.ci_files += 1
        if path in ("mvnw", "gradlew") or path.endswith(("mvnw.cmd", "gradlew.bat")):
            structure.has_wrapper = True

    if "pom.xml" in paths:
        structure.build_tool = "maven"
        structure.modules = sorted({
            str(Path(path).parent) for path in paths
            if path.endswith("pom.xml") and path != "pom.xml"
        })
    elif any(p in paths for p in ("build.gradle", "build.gradle.kts")):
        structure.build_tool = "gradle"
        structure.modules = sorted({
            str(Path(path).parent) for path in paths
            if path.endswith(("build.gradle", "build.gradle.kts")) and "/" in path
        })

    pom = texts.get("pom.xml")
    if pom:
        version = _JAVA_VERSION.search(pom)
        if version:
            structure.java_version = version.group(1)
        structure.dependencies = len(_DEPENDENCY.findall(pom))
        declared = _MODULE.findall(pom)
        if declared:
            structure.modules = sorted(set(declared))
        structure.lombok_used = "lombok" in pom.lower()

    gradle = texts.get("build.gradle") or texts.get("build.gradle.kts")
    if gradle:
        structure.dependencies = len(_GRADLE_DEP.findall(gradle))
        version = _GRADLE_JAVA.search(gradle)
        if version:
            structure.java_version = version.group(1).replace("_", ".")
        structure.lombok_used = "lombok" in gradle.lower()

    return structure


def pick_key_files(tree: list[dict], limit: int = MAX_INDEXED_FILES) -> list[str]:
    """挑出值得进知识库的文件：优先级列表打头，再补充 size 适中的文档。"""
    blobs = {
        entry.get("path", ""): entry.get("size", 0)
        for entry in tree
        if entry.get("type") == "blob"
    }

    picked: list[str] = []
    for name in PRIORITY_FILES:
        if name in blobs and blobs[name] <= MAX_FILE_BYTES:
            picked.append(name)

    for path, size in sorted(blobs.items()):
        if len(picked) >= limit:
            break
        if path in picked or size > MAX_FILE_BYTES:
            continue
        lowered = path.lower()
        if lowered.endswith(".md") and lowered.count("/") <= 2:
            picked.append(path)

    return picked[:limit]


@dataclass
class RepoIndex:
    owner: str
    name: str
    structure: JavaStructure
    documents: dict[str, str] = field(default_factory=dict)
    knowledge: KnowledgeBase | None = None
    failed: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        return {
            "documents": len(self.documents),
            "paths": sorted(self.documents)[:12],
        }


def build_index(
    client,
    owner: str,
    name: str,
    branch: str | None = None,
) -> RepoIndex:
    """拉目录树 + 关键文件，产出结构化分析与可检索的知识库。

    请求数：1 次 tree + 最多 MAX_INDEXED_FILES 次 contents。
    """
    ref = branch or "HEAD"
    tree = client.tree(owner, name, ref)
    if not tree:
        return RepoIndex(owner, name, JavaStructure())

    documents: dict[str, str] = {}
    failed: list[str] = []
    for path in pick_key_files(tree):
        text = client.file_text(owner, name, path)
        if text:
            documents[path] = text[:MAX_FILE_BYTES]
        else:
            # 取不到要记账：否则「没解析出依赖」会被误读成「这个项目没有依赖」
            failed.append(path)

    structure = analyze_tree(tree, documents)

    knowledge = KnowledgeBase(None)
    for path, text in documents.items():
        knowledge.add_document(path.rsplit("/", 1)[-1], text[:MAX_KB_CHARS])

    return RepoIndex(owner, name, structure, documents, knowledge, failed)


def java_risk_items(structure: JavaStructure) -> list[dict[str, str]]:
    """把 Java 结构翻译成风险条目。阈值集中在这里，改口径只改一处。"""
    if not structure.is_java:
        return []

    items: list[dict[str, str]] = []

    if structure.main_java >= 20 and structure.test_java == 0:
        items.append({
            "level": "高",
            "title": "有主代码但没有测试目录",
            "detail": f"src/main/java 下 {structure.main_java} 个类文件，"
                      "但未发现 src/test/java。任何改动都缺少回归保护。",
        })
    elif structure.test_ratio is not None and structure.test_ratio < 0.15:
        items.append({
            "level": "中",
            "title": "测试文件占比偏低",
            "detail": f"测试类与主代码类之比为 {structure.test_ratio}，"
                      "低于 0.15 的常见参考线。",
        })

    if structure.java_version and _is_old_java(structure.java_version):
        items.append({
            "level": "中",
            "title": "Java 版本偏旧",
            "detail": f"构建脚本声明 Java {structure.java_version}，"
                      "已不适合作为新项目的目标版本。",
        })

    if structure.dependencies is not None and structure.dependencies > 80:
        items.append({
            "level": "中",
            "title": "依赖数量偏多",
            "detail": f"构建脚本中声明了约 {structure.dependencies} 个依赖，"
                      "升级与漏洞治理成本会明显上升。",
        })

    if structure.build_tool is None:
        items.append({
            "level": "提示",
            "title": "未识别到构建工具",
            "detail": "根目录没有 pom.xml 或 build.gradle，可能不是标准 Java 工程。",
        })

    return items


def _is_old_java(version: str) -> bool:
    match = re.match(r"(\d+)", version)
    if not match:
        return False
    major = int(match.group(1))
    if major == 1:  # 1.8 这种老写法
        parts = version.split(".")
        major = int(parts[1]) if len(parts) > 1 else 8
    return major < 11
