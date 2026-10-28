"""Agent 循环测试：用假模型驱动，不消耗真实 API。"""

from __future__ import annotations

import json
import unittest

from devprism import agent
from devprism.tools import Tool, ToolContext, ToolRegistry


class StubLLM:
    """按脚本依次返回预设响应。"""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools=None, temperature=0.0, max_tokens=None):
        self.calls += 1
        if not self.responses:
            return {"choices": [{"message": {"content": "备用答案"}}]}
        return self.responses.pop(0)


def tool_call_response(name: str, arguments: str, call_id: str = "call_1") -> dict:
    return {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            }
        }]
    }


def answer_response(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


def make_registry() -> ToolRegistry:
    registry = ToolRegistry(ToolContext(client=None))  # 测试不触网

    def echo(ctx, text: str) -> dict:
        return {"echo": text}

    registry.register(Tool(
        name="echo",
        description="回显输入",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=echo,
    ))
    return registry


class TestCitationValidation(unittest.TestCase):
    def test_sources_extracted_from_observation(self):
        observation = json.dumps({
            "results": [
                {"source": "git.md#格式", "text": "..."},
                {"source": "review.md#时机", "text": "..."},
            ]
        })

        self.assertEqual(
            agent.extract_sources(observation), {"git.md#格式", "review.md#时机"}
        )

    def test_malformed_observation_is_safe(self):
        self.assertEqual(agent.extract_sources("不是 JSON"), set())

    def test_valid_citation_kept(self):
        answer = "必须带类型前缀 [来源: git.md#格式]。"
        cleaned, invalid = agent.validate_citations(answer, {"git.md#格式"})

        self.assertEqual(cleaned, answer)
        self.assertEqual(invalid, [])

    def test_invented_citation_is_flagged(self):
        answer = "必须带类型前缀 [来源: git.md#根本不存在的章节]。"
        cleaned, invalid = agent.validate_citations(answer, {"git.md#格式"})

        self.assertIn("⚠未验证", cleaned)
        self.assertEqual(invalid, ["git.md#根本不存在的章节"])

    def test_citation_without_any_retrieval_is_flagged(self):
        cleaned, invalid = agent.validate_citations("[来源: 编的.md#某处]", set())

        self.assertIn("⚠未验证", cleaned)
        self.assertEqual(len(invalid), 1)


class TestRepoDocCitations(unittest.TestCase):
    def test_repo_doc_citations_are_trusted(self):
        """回归：引用仓库自身文档（search_repo）不该被判为未验证。"""
        registry = make_registry()

        def fake_search_repo(ctx, owner, name, query, top_k=3):
            return {"results": [{"source": "README.md#构建", "text": "使用 Maven"}]}

        registry.register(Tool(
            name="search_repo",
            description="检索仓库文档",
            parameters={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "name": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["owner", "name", "query"],
            },
            handler=fake_search_repo,
        ))

        llm = StubLLM([
            tool_call_response("search_repo", '{"owner":"o","name":"r","query":"构建"}'),
            answer_response("该项目用 Maven [来源: README.md#构建]。"),
        ])
        events: list[dict] = []

        result = agent.run("测试", registry, llm, on_event=events.append)

        self.assertIn("README.md#构建", result.answer)
        self.assertNotIn("⚠未验证", result.answer)
        self.assertFalse(any(e["type"] == "citation_warning" for e in events))


class TestAgentLoop(unittest.TestCase):
    def test_single_tool_then_answer(self):
        llm = StubLLM([
            tool_call_response("echo", '{"text": "hi"}'),
            answer_response("答案是 hi"),
        ])
        events: list[dict] = []

        result = agent.run("测试问题", make_registry(), llm, on_event=events.append)

        self.assertEqual(result.answer, "答案是 hi")
        self.assertEqual(result.stopped_reason, "answered")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.steps[0].tool, "echo")
        self.assertEqual(result.steps[0].arguments, {"text": "hi"})
        self.assertTrue(result.steps[0].ok)

        kinds = [e["type"] for e in events]
        self.assertIn("tool_call", kinds)
        self.assertIn("observation", kinds)
        self.assertEqual(kinds[-1], "answer")

    def test_tool_error_is_visible_to_model(self):
        llm = StubLLM([
            tool_call_response("echo", '{"wrong": "field"}'),
            answer_response("参数错了，我换个说法"),
        ])

        result = agent.run("测试", make_registry(), llm)

        self.assertFalse(result.steps[0].ok)
        self.assertIn("缺少必填参数", result.steps[0].observation)

    def test_unknown_tool_does_not_crash(self):
        llm = StubLLM([
            tool_call_response("nonexistent", "{}"),
            answer_response("没有这个工具"),
        ])

        result = agent.run("测试", make_registry(), llm)

        self.assertFalse(result.steps[0].ok)
        self.assertIn("不存在的工具", result.steps[0].observation)

    def test_max_steps_stops_loop(self):
        loop = [tool_call_response("echo", '{"text": "x"}') for _ in range(3)]
        llm = StubLLM(loop)

        result = agent.run("测试", make_registry(), llm, max_steps=3)

        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(llm.calls, 3)

    def test_repeat_guard_blocks_identical_calls(self):
        # 连续 4 次相同调用：前 2 次真实执行，之后被护栏拦下
        llm = StubLLM([
            tool_call_response("echo", '{"text": "same"}') for _ in range(4)
        ])

        result = agent.run("测试", make_registry(), llm, max_steps=4)

        executed = [s for s in result.steps if s.ok]
        blocked = [s for s in result.steps if s.tool == "echo" and not s.ok]

        self.assertEqual(len(executed), agent.MAX_REPEAT)
        self.assertEqual(len(blocked), 4 - agent.MAX_REPEAT)
        self.assertIn("重复调用", blocked[0].observation)


if __name__ == "__main__":
    unittest.main()
