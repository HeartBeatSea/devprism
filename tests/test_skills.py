"""技能加载测试：解析 frontmatter、目录加载、坏文件容错。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devprism.skills import catalog, find_skill, load_skills, parse_skill

GOOD = """---
name: demo
title: 演示技能
description: 演示用
when: 用户问演示
tools: repo_overview, pull_health
---

## 步骤

1. 先调 repo_overview
"""


class TestParseSkill(unittest.TestCase):
    def test_parses_frontmatter(self):
        skill = parse_skill(GOOD)

        self.assertIsNotNone(skill)
        self.assertEqual(skill.name, "demo")
        self.assertEqual(skill.title, "演示技能")
        self.assertEqual(skill.when, "用户问演示")
        self.assertEqual(skill.tools, ["repo_overview", "pull_health"])
        self.assertIn("先调 repo_overview", skill.body)

    def test_missing_frontmatter_is_rejected(self):
        self.assertIsNone(parse_skill("# 只有正文，没有头部"))

    def test_missing_name_is_rejected(self):
        self.assertIsNone(parse_skill("---\ntitle: x\n---\n正文"))

    def test_quotes_are_stripped(self):
        skill = parse_skill('---\nname: "q"\ntitle: \'引号\'\n---\n正文')
        self.assertEqual(skill.name, "q")
        self.assertEqual(skill.title, "引号")

    def test_tools_are_optional(self):
        skill = parse_skill("---\nname: x\ntitle: y\n---\n正文")
        self.assertEqual(skill.tools, [])


class TestLoadSkills(unittest.TestCase):
    def test_loads_directory_of_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("alpha", "beta"):
                folder = root / name
                folder.mkdir()
                (folder / "SKILL.md").write_text(
                    f"---\nname: {name}\ntitle: {name}\n---\n步骤", encoding="utf-8"
                )

            skills = load_skills(root)

            self.assertEqual([s.name for s in skills], ["alpha", "beta"])

    def test_broken_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad").mkdir()
            (root / "bad" / "SKILL.md").write_text("没有头部", encoding="utf-8")

            self.assertEqual(load_skills(root), [])

    def test_missing_directory_is_safe(self):
        self.assertEqual(load_skills(Path("不存在的目录")), [])


class TestLookup(unittest.TestCase):
    def setUp(self):
        self.skills = [parse_skill(GOOD)]

    def test_find_by_name(self):
        self.assertIsNotNone(find_skill(self.skills, "demo"))

    def test_find_is_case_insensitive(self):
        self.assertIsNotNone(find_skill(self.skills, "DEMO"))

    def test_find_missing(self):
        self.assertIsNone(find_skill(self.skills, "nope"))

    def test_catalog_lists_names(self):
        text = catalog(self.skills)
        self.assertIn("demo", text)
        self.assertIn("演示技能", text)

    def test_catalog_empty(self):
        self.assertIn("没有", catalog([]))


class TestProjectSkills(unittest.TestCase):
    """仓库自带的技能包必须能全部加载，且声明的工具确实存在。"""

    def test_repo_skills_are_valid(self):
        from devprism.config import ROOT
        from devprism.tools import build_registry, ToolContext

        skills = load_skills(ROOT / "skills")
        self.assertGreaterEqual(len(skills), 3, "至少要有几个内置技能")

        registry = build_registry(ToolContext(client=None))
        known = set(registry.names())
        for skill in skills:
            with self.subTest(skill=skill.name):
                self.assertTrue(skill.description, "description 不能为空")
                self.assertTrue(skill.when, "when 不能为空")
                self.assertTrue(skill.body, "步骤不能为空")
                missing = [t for t in skill.tools if t not in known]
                self.assertEqual(missing, [], f"声明了不存在的工具：{missing}")


if __name__ == "__main__":
    unittest.main()
