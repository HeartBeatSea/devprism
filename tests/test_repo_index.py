"""仓库结构分析测试：全部基于目录树，不触网。"""

from __future__ import annotations

import unittest

from devprism.repo_index import (
    JavaStructure,
    analyze_tree,
    build_index,
    classify_area,
    java_risk_items,
    pick_key_files,
    summarize_areas,
)

POM = """<project>
  <properties>
    <maven.compiler.source>8</maven.compiler.source>
  </properties>
  <modules>
    <module>core</module>
    <module>web</module>
  </modules>
  <dependencies>
    <dependency><artifactId>lombok</artifactId></dependency>
    <dependency><artifactId>guava</artifactId></dependency>
  </dependencies>
</project>
"""


def java_tree(main: int, test: int, modules: int = 0) -> list[dict]:
    tree = [{"path": "pom.xml", "type": "blob", "size": 1200}]
    for i in range(main):
        tree.append({"path": f"src/main/java/com/x/C{i}.java", "type": "blob", "size": 100})
    for i in range(test):
        tree.append({"path": f"src/test/java/com/x/C{i}Test.java", "type": "blob", "size": 100})
    for m in range(modules):
        tree.append({"path": f"mod{m}/pom.xml", "type": "blob", "size": 800})
        tree.append({"path": f"mod{m}/src/main/java/com/x/M{m}.java", "type": "blob", "size": 60})
    return tree


class TestAnalyzeTree(unittest.TestCase):
    def test_counts_main_and_test_classes(self):
        structure = analyze_tree(java_tree(main=10, test=4), {"pom.xml": POM})

        self.assertTrue(structure.is_java)
        self.assertEqual(structure.main_java, 10)
        self.assertEqual(structure.test_java, 4)
        self.assertEqual(structure.test_ratio, 0.4)

    def test_detects_maven_and_modules(self):
        structure = analyze_tree(java_tree(main=3, test=1, modules=2), {"pom.xml": POM})

        self.assertEqual(structure.build_tool, "maven")
        self.assertEqual(sorted(structure.modules), ["core", "web"])

    def test_parses_java_version_and_dependencies(self):
        structure = analyze_tree(java_tree(main=1, test=0), {"pom.xml": POM})

        self.assertEqual(structure.java_version, "8")
        self.assertEqual(structure.dependencies, 2)
        self.assertTrue(structure.lombok_used)

    def test_detects_gradle(self):
        tree = [
            {"path": "build.gradle", "type": "blob", "size": 500},
            {"path": "src/main/java/a/A.java", "type": "blob", "size": 90},
        ]
        gradle = "sourceCompatibility = JavaVersion.VERSION_17\nimplementation('x:y:1.0')\n"

        structure = analyze_tree(tree, {"build.gradle": gradle})

        self.assertEqual(structure.build_tool, "gradle")
        self.assertEqual(structure.java_version, "17")
        self.assertEqual(structure.dependencies, 1)

    def test_non_java_repo(self):
        tree = [{"path": "README.md", "type": "blob", "size": 100}]
        structure = analyze_tree(tree)

        self.assertFalse(structure.is_java)
        self.assertIsNone(structure.build_tool)

    def test_test_ratio_none_without_main(self):
        tree = [
            {"path": "pom.xml", "type": "blob", "size": 100},
            {"path": "src/test/java/T.java", "type": "blob", "size": 10},
        ]
        self.assertIsNone(analyze_tree(tree).test_ratio)

    def test_counts_ci_and_wrapper(self):
        tree = [
            {"path": ".github/workflows/build.yml", "type": "blob", "size": 50},
            {"path": "mvnw", "type": "blob", "size": 50},
            {"path": "src/main/java/a/A.java", "type": "blob", "size": 50},
        ]
        structure = analyze_tree(tree)

        self.assertEqual(structure.ci_files, 1)
        self.assertTrue(structure.has_wrapper)


class TestPickKeyFiles(unittest.TestCase):
    def test_priority_files_come_first(self):
        tree = [
            {"path": "README.md", "type": "blob", "size": 500},
            {"path": "pom.xml", "type": "blob", "size": 400},
            {"path": "docs/guide.md", "type": "blob", "size": 300},
        ]
        picked = pick_key_files(tree)

        self.assertEqual(picked[:2], ["README.md", "pom.xml"])

    def test_skips_oversized_files(self):
        tree = [{"path": "README.md", "type": "blob", "size": 10_000_000}]
        self.assertEqual(pick_key_files(tree), [])

    def test_respects_limit(self):
        tree = [{"path": f"doc{i}.md", "type": "blob", "size": 100} for i in range(30)]
        self.assertLessEqual(len(pick_key_files(tree, limit=5)), 5)


class TestJavaRisks(unittest.TestCase):
    def test_no_tests_is_high_risk(self):
        items = java_risk_items(JavaStructure(main_java=50, test_java=0, build_tool="maven"))
        self.assertTrue(any(i["title"] == "有主代码但没有测试目录" for i in items))
        self.assertTrue(any(i["level"] == "高" for i in items))

    def test_low_test_ratio_is_medium(self):
        items = java_risk_items(JavaStructure(main_java=100, test_java=5, build_tool="maven"))
        self.assertTrue(any("测试文件占比" in i["title"] for i in items))

    def test_old_java_version_flagged(self):
        items = java_risk_items(JavaStructure(main_java=5, test_java=1,
                                              build_tool="maven", java_version="1.8"))
        self.assertTrue(any("Java 版本偏旧" in i["title"] for i in items))

    def test_healthy_repo_has_no_risks(self):
        items = java_risk_items(JavaStructure(main_java=20, test_java=18,
                                              build_tool="maven", java_version="17",
                                              dependencies=10))
        self.assertEqual(items, [])

    def test_non_java_repo_returns_empty(self):
        self.assertEqual(java_risk_items(JavaStructure()), [])


class FakeClient:
    def __init__(self, tree, files):
        self._tree = tree
        self._files = files
        self.tree_calls = 0
        self.file_calls = 0

    def repo(self, owner, name):
        return {"default_branch": "main", "full_name": f"{owner}/{name}"}

    def tree(self, owner, name, ref="HEAD"):
        self.tree_calls += 1
        return self._tree

    def file_text(self, owner, name, path):
        self.file_calls += 1
        return self._files.get(path)


class TestRepoTools(unittest.TestCase):
    def test_repo_structure_tool_returns_java_stats(self):
        from devprism.tools import repo_structure
        from devprism.tools import ToolContext

        ctx = ToolContext(client=FakeClient(java_tree(6, 4), {"pom.xml": POM}))
        result = repo_structure(ctx, "o", "r")

        self.assertTrue(result["java"]["is_java"])
        self.assertEqual(result["java"]["main_java"], 6)
        self.assertIn("pom.xml", result["indexed_files"])

    def test_search_repo_finds_project_docs(self):
        from devprism.tools import search_repo, ToolContext

        tree = java_tree(2, 1) + [{"path": "README.md", "type": "blob", "size": 200}]
        docs = {"README.md": "# 项目\n\n## 构建\n\n运行 mvn package 打包。"}
        ctx = ToolContext(client=FakeClient(tree, docs))
        result = search_repo(ctx, "o", "r", "构建 打包")

        self.assertTrue(result["results"])
        self.assertIn("README", result["results"][0]["source"])

    def test_search_repo_miss_is_explicit(self):
        from devprism.tools import search_repo, ToolContext

        ctx = ToolContext(client=FakeClient(java_tree(2, 1), {"README.md": "# x"}))
        result = search_repo(ctx, "o", "r", "量子计算")

        self.assertEqual(result["results"], [])
        self.assertIn("不要编造", result["note"])


class TestBuildIndex(unittest.TestCase):
    def test_indexes_documents_and_builds_knowledge(self):
        client = FakeClient(
            java_tree(main=5, test=3),
            {"pom.xml": POM, "README.md": "# 示例项目\n\n## 构建\n\n使用 Maven。"},
        )

        index = build_index(client, "o", "r")

        self.assertEqual(client.tree_calls, 1)
        self.assertIn("pom.xml", index.documents)
        self.assertEqual(index.structure.build_tool, "maven")
        hits = index.knowledge.search("构建 Maven")
        self.assertTrue(hits)

    def test_empty_tree_is_safe(self):
        index = build_index(FakeClient([], {}), "o", "r")
        self.assertFalse(index.structure.is_java)
        self.assertEqual(index.documents, {})

    def test_unreadable_files_are_recorded(self):
        """取不到关键文件要记账，否则会被误读成「项目没有依赖」。"""
        class BrokenClient(FakeClient):
            def file_text(self, owner, name, path):
                return None

        index = build_index(BrokenClient(java_tree(main=2, test=1), {}), "o", "r")

        self.assertIn("pom.xml", index.failed)
        self.assertIsNone(index.structure.dependencies)


class TestAreaClassification(unittest.TestCase):
    def test_known_roles(self):
        cases = {
            "src/main/java/a/B.java": "主代码",
            "src/test/java/a/BTest.java": "测试",
            "pom.xml": "构建配置",
            "mvnw": "构建配置",
            ".github/workflows/ci.yml": "CI",
            "Jenkinsfile": "CI",
            "README.md": "文档",
            "docs/guide.md": "文档",
            "scripts/build.sh": "脚本",
            "web/index.html": "前端",
            "site/logo.png": "二进制",
            "src/main/resources/app.properties": "资源",
            "data/whatever.dat": "其他",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classify_area(path), expected)

    def test_test_dir_wins_over_extension(self):
        # src/test 下的 java 必须先判成测试，不能算进主代码
        self.assertEqual(classify_area("module/src/test/java/X.java"), "测试")

    def test_non_java_languages_are_code(self):
        """回归：只认 Java 会让 Python / 前端项目在核心视图里直接空掉。"""
        cases = {
            "devprism/agent.py": "主代码",
            "scripts/check_secrets.py": "主代码",
            "src/app.ts": "主代码",
            "cmd/main.go": "主代码",
            "web/index.html": "前端",
            "assets/style.css": "前端",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classify_area(path), expected)

    def test_python_tests_are_detected(self):
        self.assertEqual(classify_area("tests/test_agent.py"), "测试")
        self.assertEqual(classify_area("pkg/agent_test.py"), "测试")
        self.assertEqual(classify_area("web/app.test.ts"), "测试")


class TestSummarizeAreas(unittest.TestCase):
    def _tree(self, items):
        return [{"path": p, "type": "blob", "size": s} for p, s in items]

    def test_counts_and_bytes(self):
        tree = self._tree([
            ("src/main/java/A.java", 100),
            ("src/main/java/B.java", 50),
            ("README.md", 30),
        ])

        areas, _ = summarize_areas(tree)

        self.assertEqual(areas["主代码"]["files"], 2)
        self.assertEqual(areas["主代码"]["bytes"], 150)
        self.assertEqual(areas["主代码"]["java_files"], 2)
        self.assertEqual(areas["文档"]["files"], 1)

    def test_directories_are_ignored(self):
        tree = [{"path": "src", "type": "tree", "size": 0}]

        areas, modules = summarize_areas(tree)

        self.assertEqual(areas, {})
        self.assertEqual(modules, [])

    def test_module_attribution(self):
        tree = self._tree([
            ("core/src/main/java/A.java", 10),
            ("web/src/main/java/B.java", 20),
            ("src/main/java/C.java", 5),
        ])

        _, modules = summarize_areas(tree, ["core", "web"])

        by_name = {m["name"]: m for m in modules}
        self.assertEqual(by_name["core"]["files"], 1)
        self.assertEqual(by_name["web"]["files"], 1)
        self.assertEqual(by_name["（根目录）"]["files"], 1, "src 是布局目录，不该算成模块")

    def test_sorted_by_bytes(self):
        tree = self._tree([("a/x.txt", 10), ("b/y.txt", 900)])

        _, modules = summarize_areas(tree)

        self.assertEqual(modules[0]["name"], "b")

    def test_areas_follow_declared_order(self):
        tree = self._tree([
            ("README.md", 10),          # 文档
            ("src/main/java/A.java", 5),  # 主代码
        ])

        areas, _ = summarize_areas(tree)

        self.assertEqual(list(areas)[0], "主代码", "主代码应排在文档前面")


class TestAreaInStructure(unittest.TestCase):
    def test_analyze_tree_fills_areas(self):
        tree = [
            {"path": "pom.xml", "type": "blob", "size": 100},
            {"path": "src/main/java/A.java", "type": "blob", "size": 200},
            {"path": "README.md", "type": "blob", "size": 50},
        ]

        structure = analyze_tree(tree, {"pom.xml": POM})
        as_dict = structure.to_dict()

        self.assertIn("主代码", as_dict["areas"])
        self.assertTrue(as_dict["modules_detail"])
        self.assertFalse(as_dict["tree_truncated"])

    def test_truncated_tree_is_disclosed(self):
        class TruncatedClient(FakeClient):
            tree_truncated = True

        index = build_index(TruncatedClient(java_tree(2, 1), {"pom.xml": POM}), "o", "r")

        self.assertTrue(index.structure.to_dict()["tree_truncated"])


if __name__ == "__main__":
    unittest.main()
