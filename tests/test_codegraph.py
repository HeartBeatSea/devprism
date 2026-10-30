"""结构图测试：只吃目录树，不触网。"""

from __future__ import annotations

import unittest

from devprism.codegraph import build_graph


def tree_of(items):
    return [{"path": path, "type": "blob", "size": size} for path, size in items]


class TestBuildGraph(unittest.TestCase):
    def test_root_and_children(self):
        graph = build_graph("o/demo", tree_of([
            ("src/main/java/A.java", 400),
            ("src/test/java/ATest.java", 100),
            ("README.md", 50),
        ]))

        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertIn("", nodes, "应该有根节点")
        self.assertEqual(nodes[""]["label"], "demo")
        self.assertEqual(nodes[""]["files"], 1, "默认只看功能代码，测试和 README 都不算")
        self.assertEqual(graph["total_files"], 1)
        self.assertEqual(graph["hidden_files"], 2)
        self.assertEqual(graph["focus"], "code")

    def test_tests_are_excluded_from_core(self):
        graph = build_graph("o/demo", tree_of([
            ("src/main/java/A.java", 400),
            ("src/test/java/ATest.java", 900),
        ]))

        self.assertEqual(graph["total_files"], 1, "测试类比主代码还大也不该进核心视图")

    def test_all_focus_includes_docs(self):
        graph = build_graph("o/demo", tree_of([
            ("src/main/java/A.java", 400),
            ("README.md", 50),
        ]), focus="all")

        self.assertEqual(graph["total_files"], 2)
        self.assertEqual(graph["hidden_files"], 0)
        labels = {node["label"] for node in graph["nodes"]}
        self.assertIn("README.md", labels)

    def test_docs_only_directory_is_dropped(self):
        graph = build_graph("o/demo", tree_of([
            ("src/main/java/A.java", 400),
            ("docs/guide.md", 900),
        ]))

        ids = {node["id"] for node in graph["nodes"]}
        self.assertIn("src", ids)
        self.assertNotIn("docs", ids, "整目录都是文档时不该出现在星系里")

    def test_invalid_focus_falls_back_to_code(self):
        graph = build_graph("o/demo", tree_of([("README.md", 10)]), focus="wat")

        self.assertEqual(graph["focus"], "code")
        self.assertEqual(graph["nodes"], [])

    def test_directory_aggregates_descendants(self):
        graph = build_graph("o/demo", tree_of([
            ("src/main/java/A.java", 400),
            ("src/main/java/B.java", 600),
        ]))

        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["src"]["files"], 2)
        self.assertEqual(nodes["src"]["bytes"], 1000)

    def test_depth_is_capped(self):
        deep = "/".join(["a", "b", "c", "d", "e", "f", "F.java"])
        graph = build_graph("o/demo", tree_of([(deep, 100)]))

        dirs = [n["depth"] for n in graph["nodes"] if n["kind"] == "dir"]
        self.assertLessEqual(max(dirs or [0]), 3, "目录层数要卡住，否则星系被拉成一条线")
        self.assertLessEqual(max(n["depth"] for n in graph["nodes"]), 4,
                             "深度上限处的目录可以挂文件，但不能再往下")
        self.assertTrue(graph["truncated"], "被截断时必须标注")

    def test_deep_code_files_are_surfaced(self):
        """Java 的类埋在 src/main/java/... 第五层以下，不能因为超深度就整片消失。"""
        graph = build_graph("o/demo", tree_of([
            ("gson/src/main/java/com/demo/Gson.java", 900),
            ("gson/src/main/java/com/demo/Json.java", 500),
        ]))

        files = [n for n in graph["nodes"] if n["kind"] == "file"]
        self.assertTrue(files, "深度上限的目录应把重要的代码文件挂上来")
        self.assertIn("Gson.java", {n["label"] for n in files})

    def test_children_are_limited(self):
        items = [(f"mod{i}/X.java", 100 + i) for i in range(20)]
        graph = build_graph("o/demo", tree_of(items))

        level1 = [n for n in graph["nodes"] if n["depth"] == 1]
        self.assertLessEqual(len(level1), 10, "每层子节点要限量，否则画不下")

    def test_empty_tree(self):
        graph = build_graph("o/demo", [])

        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["total_files"], 0)

    def test_labels_are_single_segment(self):
        graph = build_graph("o/demo", tree_of([("src/main/java/A.java", 10)]))

        for node in graph["nodes"]:
            self.assertNotIn("/", node["label"] if node["kind"] != "root" else "")


if __name__ == "__main__":
    unittest.main()
