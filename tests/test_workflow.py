"""工作流执行器测试：用假模型和假工具，不触网。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devprism import workflow
from devprism.tools import Tool, ToolContext, ToolRegistry


class StubLLM:
    def __init__(self, reply: str = "草稿内容") -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, temperature=0.0, max_tokens=None):
        self.calls.append(messages)
        return {"choices": [{"message": {"content": self.reply}}]}


def make_registry() -> ToolRegistry:
    registry = ToolRegistry(ToolContext(client=None))

    def echo(ctx, text: str) -> dict:
        return {"echo": text}

    registry.register(Tool(
        name="echo",
        description="回显",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=echo,
    ))
    return registry


class TestDefinition(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(text)
            path = Path(handle.name)
        return workflow.load_workflow(path)

    def test_valid(self):
        definition = self._load("name: t\nsteps:\n  - id: a\n    type: tool\n    tool: echo\n")
        self.assertEqual(definition.name, "t")
        self.assertEqual(definition.step_ids(), ["a"])

    def test_duplicate_id_rejected(self):
        with self.assertRaises(workflow.WorkflowError):
            self._load("steps:\n  - id: a\n    type: tool\n    tool: echo\n"
                       "  - id: a\n    type: tool\n    tool: echo\n")

    def test_unknown_type_rejected(self):
        with self.assertRaises(workflow.WorkflowError):
            self._load("steps:\n  - id: a\n    type: magic\n")

    def test_tool_node_needs_tool_name(self):
        with self.assertRaises(workflow.WorkflowError):
            self._load("steps:\n  - id: a\n    type: tool\n")


class TestRender(unittest.TestCase):
    def test_input_and_step_refs(self):
        scope = {"input": {"owner": "pallets"}, "steps": {"a": {"output": {"n": 3}}}}
        self.assertEqual(
            workflow.render("{{input.owner}}/{{ steps.a.output }}", scope),
            'pallets/{"n": 3}',
        )

    def test_missing_variable_renders_empty(self):
        self.assertEqual(workflow.render("[{{ input.nope }}]", {"input": {}}), "[]")


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = workflow.WorkflowStore(self.root / "wf.sqlite3")
        self.out = self.root / "out.md"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _definition(self):
        return workflow.WorkflowDefinition(
            name="测试流程",
            description="",
            steps=[
                {"id": "a", "type": "tool", "tool": "echo",
                 "args": {"text": "{{ input.who }}"}},
                {"id": "b", "type": "llm", "prompt": "数据：{{ steps.a.output }}"},
                {"id": "c", "type": "approval", "message": "确认？"},
                {"id": "d", "type": "write_file",
                 "path": str(self.out), "content": "{{ steps.b.output }}"},
            ],
        )

    def test_pause_then_resume_completes(self):
        llm = StubLLM("周报正文")
        runner = workflow.WorkflowRunner(
            self._definition(), make_registry(), llm, self.store, {"who": "alice"}
        )

        first = runner.run()
        self.assertEqual(first.status, "paused")
        # 暂停时把上一步（llm 草稿）的输出带出来，方便人审阅
        self.assertEqual(first.last_output, "周报正文")
        self.assertFalse(self.out.exists())

        second = workflow.WorkflowRunner(
            self._definition(), make_registry(), llm, self.store, {}
        ).run(run_id=first.run_id, approve=True)

        self.assertEqual(second.status, "done")
        self.assertTrue(self.out.exists())
        self.assertEqual(self.out.read_text(encoding="utf-8"), "周报正文")

    def test_inputs_survive_resume(self):
        """回归：续跑没带 --var 时，必须从库里恢复输入，否则模板渲染成空。"""
        llm = StubLLM("正文")
        first = workflow.WorkflowRunner(
            self._definition(), make_registry(), llm, self.store, {"who": "bob"}
        ).run()

        second = workflow.WorkflowRunner(
            self._definition(), make_registry(), llm, self.store, {}
        ).run(run_id=first.run_id, approve=True)

        self.assertEqual(second.context["input"]["who"], "bob")

    def test_failed_step_marks_run_failed(self):
        definition = workflow.WorkflowDefinition(
            name="坏流程", description="",
            steps=[{"id": "x", "type": "tool", "tool": "no_such_tool", "args": {}}],
        )
        result = workflow.WorkflowRunner(
            definition, make_registry(), StubLLM(), self.store, {}
        ).run()

        self.assertEqual(result.status, "failed")
        self.assertIn("不存在", result.error or "")

    def test_unknown_run_id_raises(self):
        with self.assertRaises(workflow.WorkflowError):
            workflow.WorkflowRunner(
                self._definition(), make_registry(), StubLLM(), self.store, {}
            ).run(run_id="deadbeef")


if __name__ == "__main__":
    unittest.main()
