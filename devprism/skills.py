"""技能（Skills）：把「怎么做一件事」写成模型可加载的流程文件。

和检索的区别：检索解决「模型不知道的事实」，技能解决「模型不知道该怎么做」。
团队规范是事实，走知识库；体检流程、检查清单、该调哪些工具，走技能包。

**渐进式披露**：模型先只看到目录（名字 + 什么时候用），判断相关了才加载正文。
这样即使技能很多，也不会一次性把上下文塞满。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKILL_FILE = "SKILL.md"
_FRONTMATTER = re.compile(r"^\s*---\s*\n(.*?)\n\s*---\s*\n?(.*)$", re.S)
_FIELD = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


@dataclass
class Skill:
    name: str
    title: str
    description: str
    when: str
    tools: list[str] = field(default_factory=list)
    body: str = ""
    path: Path | None = None

    def brief(self) -> dict[str, Any]:
        """只给目录信息，正文要 load 才给。"""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "when": self.when,
        }

    def full(self) -> dict[str, Any]:
        return {**self.brief(), "tools": self.tools, "steps": self.body}


def parse_skill(text: str, path: Path | None = None) -> Skill | None:
    """解析带 frontmatter 的技能文件。缺 name 的直接跳过，不抛异常。"""
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    header, body = match.group(1), match.group(2)

    fields: dict[str, str] = {}
    for line in header.splitlines():
        found = _FIELD.match(line.strip())
        if found:
            fields[found.group(1).lower()] = found.group(2).strip().strip('"').strip("'")

    name = fields.get("name")
    if not name:
        return None

    tools = [item.strip() for item in fields.get("tools", "").split(",") if item.strip()]
    return Skill(
        name=name,
        title=fields.get("title") or name,
        description=fields.get("description", ""),
        when=fields.get("when", ""),
        tools=tools,
        body=body.strip(),
        path=path,
    )


def load_skills(root: Path) -> list[Skill]:
    """从 <root>/<name>/SKILL.md 加载全部技能，按名字排序。坏文件跳过。"""
    if not root or not root.is_dir():
        return []

    skills: list[Skill] = []
    for path in sorted(root.glob(f"*/{SKILL_FILE}")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        skill = parse_skill(text, path)
        if skill:
            skills.append(skill)
    return sorted(skills, key=lambda item: item.name)


def find_skill(skills: list[Skill], name: str) -> Skill | None:
    wanted = (name or "").strip().lower()
    for skill in skills:
        if skill.name.lower() == wanted:
            return skill
    return None


def catalog(skills: list[Skill]) -> str:
    """给模型看的一行式目录。"""
    if not skills:
        return "（没有可用技能）"
    return "\n".join(
        f"- {item.name}（{item.title}）：{item.description or item.when}"
        for item in skills
    )
