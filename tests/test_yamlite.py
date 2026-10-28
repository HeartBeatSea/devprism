"""极简 YAML 子集解析器测试。"""

from __future__ import annotations

import unittest

from devprism import yamlite


class TestScalars(unittest.TestCase):
    def test_types(self):
        data = yamlite.loads("a: 1\nb: 3.5\nc: true\nd: null\ne: 文本\n")
        self.assertEqual(data["a"], 1)
        self.assertAlmostEqual(data["b"], 3.5)
        self.assertIs(data["c"], True)
        self.assertIsNone(data["d"])
        self.assertEqual(data["e"], "文本")

    def test_quoted_strings(self):
        data = yamlite.loads('a: "he said \\"hi\\""\nb: \'x # not comment\'\n')
        self.assertEqual(data["a"], 'he said "hi"')
        self.assertEqual(data["b"], "x # not comment")


class TestStructure(unittest.TestCase):
    def test_nested_map(self):
        data = yamlite.loads("a:\n  b:\n    c: 1\n")
        self.assertEqual(data["a"]["b"]["c"], 1)

    def test_list_of_maps(self):
        text = "steps:\n  - id: one\n    type: tool\n  - id: two\n    type: llm\n"
        data = yamlite.loads(text)
        self.assertEqual([s["id"] for s in data["steps"]], ["one", "two"])
        self.assertEqual(data["steps"][1]["type"], "llm")

    def test_map_inside_list_item(self):
        text = "steps:\n  - id: a\n    args:\n      x: 1\n      y: 2\n"
        data = yamlite.loads(text)
        self.assertEqual(data["steps"][0]["args"], {"x": 1, "y": 2})

    def test_block_scalar_keeps_blank_lines(self):
        text = "p: |\n  line1\n\n  line3\nq: 1\n"
        data = yamlite.loads(text)
        self.assertEqual(data["p"], "line1\n\nline3")

    def test_comments_ignored(self):
        text = "# 顶部注释\na: 1  # 行尾注释\nb: 2\n"
        data = yamlite.loads(text)
        self.assertEqual(data, {"a": 1, "b": 2})

    def test_empty_document(self):
        self.assertEqual(yamlite.loads("   \n\n"), {})


class TestErrors(unittest.TestCase):
    def test_bad_indent_raises(self):
        with self.assertRaises(yamlite.YamlError):
            yamlite.loads("a: 1\n   b: 2\n")

    def test_non_key_line_raises(self):
        with self.assertRaises(yamlite.YamlError):
            yamlite.loads("a: 1\nthis is not a pair\n")


if __name__ == "__main__":
    unittest.main()
