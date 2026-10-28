"""工作流执行器：按 YAML 定义顺序执行节点，状态落 SQLite。

和 Agent 的分工：
  * Agent（`ask`）—— 模型决定下一步，适合开放问题；
  * 工作流（`run`）—— 人预先写死步骤，适合固定产出、需要审计的场景。

设计上的三个约束：
  1. **步骤按声明顺序执行**，只能引用前面步骤的输出，不搞隐式依赖推导；
  2. **每步的输入输出都落库**，所以中途失败可以续跑，人工确认可以暂停后再继续；
  3. **LLM 只是节点之一**，能用工具算出来的东西绝不交给模型。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import yamlite
from .llm import LLMClient
from .tools import ToolRegistry

VALID_TYPES = ("tool", "llm", "approval", "write_file")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


class WorkflowError(RuntimeError):
    pass


# ---------------------------------------------------------------- 定义

@dataclass
class WorkflowDefinition:
    name: str
    description: str
    steps: list[dict[str, Any]]

    def step_ids(self) -> list[str]:
        return [step["id"] for step in self.steps]


def load_workflow(path: Path) -> WorkflowDefinition:
    data = yamlite.load_file(path)
    if not isinstance(data, dict):
        raise WorkflowError("工作流文件顶层必须是映射")
    steps = data.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("工作流必须包含非空的 steps 列表")

    seen: set[str] = set()
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise WorkflowError(f"第 {index} 个步骤不是映射")
        step_id = step.get("id")
        if not step_id:
            raise WorkflowError(f"第 {index} 个步骤缺少 id")
        if step_id in seen:
            raise WorkflowError(f"步骤 id 重复：{step_id}")
        seen.add(step_id)

        kind = step.get("type")
        if kind not in VALID_TYPES:
            raise WorkflowError(f"步骤 {step_id} 的类型非法：{kind}（允许：{VALID_TYPES}）")
        if kind == "tool" and not step.get("tool"):
            raise WorkflowError(f"步骤 {step_id} 是 tool 节点但没写 tool 名")
        if kind == "llm" and not step.get("prompt"):
            raise WorkflowError(f"步骤 {step_id} 是 llm 节点但没写 prompt")
        if kind == "write_file" and not (step.get("path") and step.get("content") is not None):
            raise WorkflowError(f"步骤 {step_id} 是 write_file 节点但缺少 path 或 content")

    return WorkflowDefinition(
        name=str(data.get("name") or path.stem),
        description=str(data.get("description") or ""),
        steps=steps,
    )


# ---------------------------------------------------------------- 状态

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL,
    inputs     TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_steps (
    run_id     TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    status     TEXT NOT NULL,
    output     TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, step_id)
);
"""


class WorkflowStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        # 老库补列：CREATE TABLE IF NOT EXISTS 不会加新字段
        try:
            self._conn.execute("ALTER TABLE workflow_runs ADD COLUMN inputs TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def create_run(self, name: str, inputs: dict[str, Any] | None = None) -> str:
        run_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._conn.execute(
            "INSERT INTO workflow_runs (run_id, name, status, inputs, created_at, updated_at)"
            " VALUES (?, ?, 'running', ?, ?, ?)",
            (run_id, name, json.dumps(inputs or {}, ensure_ascii=False), now, now),
        )
        self._conn.commit()
        return run_id

    def get_inputs(self, run_id: str) -> dict[str, Any]:
        """续跑时必须能取回原始输入，否则模板会渲染成空字符串。"""
        row = self._conn.execute(
            "SELECT inputs FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row or not row[0]:
            return {}
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return {}

    def set_status(self, run_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status, time.time(), run_id),
        )
        self._conn.commit()

    def get_status(self, run_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row[0] if row else None

    def save_step(self, run_id: str, step_id: str, status: str, output: Any) -> None:
        payload = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        self._conn.execute(
            "INSERT OR REPLACE INTO workflow_steps (run_id, step_id, status, output, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, step_id, status, payload, time.time()),
        )
        self._conn.commit()

    def done_steps(self, run_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT step_id, output FROM workflow_steps WHERE run_id = ? AND status = 'done'",
            (run_id,),
        ).fetchall()
        return {step_id: output for step_id, output in rows}

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------- 变量

def _lookup(scope: dict[str, Any], path: str) -> Any:
    current: Any = scope
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return ""
    return current


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def render(template: str, scope: dict[str, Any]) -> str:
    return _PLACEHOLDER.sub(lambda m: _stringify(_lookup(scope, m.group(1))), template)


# ---------------------------------------------------------------- 执行

@dataclass
class RunResult:
    run_id: str
    status: str
    context: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    last_output: Any = None


class WorkflowRunner:
    def __init__(
        self,
        definition: WorkflowDefinition,
        registry: ToolRegistry,
        llm: LLMClient,
        store: WorkflowStore,
        inputs: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.definition = definition
        self.registry = registry
        self.llm = llm
        self.store = store
        self.inputs = inputs
        self.emit = on_event or (lambda event: None)

    def run(self, run_id: str | None = None, approve: bool = False) -> RunResult:
        if run_id is None:
            run_id = self.store.create_run(self.definition.name, self.inputs)
        elif self.store.get_status(run_id) is None:
            raise WorkflowError(f"找不到运行记录：{run_id}")
        else:
            # 续跑时命令行可能没带 --var，从库里把原始输入捞回来
            if not self.inputs:
                self.inputs = self.store.get_inputs(run_id)
            self.emit({"type": "resume", "run_id": run_id})

        done = self.store.done_steps(run_id)
        scope: dict[str, Any] = {"input": dict(self.inputs), "steps": {}}
        for step_id, payload in done.items():
            try:
                scope["steps"][step_id] = {"output": json.loads(payload)}
            except (json.JSONDecodeError, TypeError):
                scope["steps"][step_id] = {"output": payload}

        last_output: Any = None

        for step in self.definition.steps:
            step_id = step["id"]
            if step_id in done:
                last_output = scope["steps"][step_id]["output"]
                continue

            kind = step["type"]

            if kind == "approval":
                if not approve:
                    self.store.set_status(run_id, "paused")
                    self.emit({
                        "type": "paused",
                        "run_id": run_id,
                        "step": step_id,
                        "message": step.get("message", ""),
                    })
                    return RunResult(run_id, "paused", scope, last_output=last_output)
                approve = False  # 一次只放行一个人工确认节点
                self.store.save_step(run_id, step_id, "done", {"approved": True})
                scope["steps"][step_id] = {"output": {"approved": True}}
                continue

            self.emit({"type": "step_start", "step": step_id, "kind": kind})
            try:
                output = self._execute(step, scope)
            except Exception as exc:  # 工作流层面的失败要落库，便于续跑
                self.store.save_step(run_id, step_id, "failed", str(exc))
                self.store.set_status(run_id, "failed")
                self.emit({"type": "step_failed", "step": step_id, "error": str(exc)})
                return RunResult(run_id, "failed", scope, error=str(exc))

            self.store.save_step(run_id, step_id, "done", output)
            scope["steps"][step_id] = {"output": output}
            last_output = output
            self.emit({"type": "step_done", "step": step_id, "output": output})

        self.store.set_status(run_id, "done")
        self.emit({"type": "done", "run_id": run_id})
        return RunResult(run_id, "done", scope, last_output=last_output)

    def _execute(self, step: dict[str, Any], scope: dict[str, Any]) -> Any:
        kind = step["type"]

        if kind == "tool":
            args = {
                key: (render(value, scope) if isinstance(value, str) else value)
                for key, value in (step.get("args") or {}).items()
            }
            observation, is_error = self.registry.call(
                step["tool"], json.dumps(args, ensure_ascii=False)
            )
            payload = json.loads(observation)
            if is_error:
                raise WorkflowError(payload.get("error") or "工具执行失败")
            return payload

        if kind == "llm":
            prompt = render(step["prompt"], scope)
            response = self.llm.chat(
                [{"role": "user", "content": prompt}], temperature=0
            )
            message = (response.get("choices") or [{}])[0].get("message") or {}
            return (message.get("content") or "").strip()

        if kind == "write_file":
            path = Path(render(step["path"], scope))
            content = render(step["content"], scope)
            path.write_text(content, encoding="utf-8")
            return {"path": str(path), "bytes": len(content.encode("utf-8"))}

        raise WorkflowError(f"未知节点类型：{kind}")
