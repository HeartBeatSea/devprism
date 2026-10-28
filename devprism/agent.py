"""ReAct 循环：模型自己决定查什么，直到有足够证据回答。

不依赖 LangChain / LangGraph —— 核心就是一个 while 循环加消息拼接，
这样每个环节都能讲清楚，也方便后面接 SSE 把轨迹推给前端。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import LLMClient, LLMError
from .tools import ToolRegistry

SYSTEM_PROMPT = """你是一个研发效能分析助手。你可以调用工具获取仓库的真实数据，然后回答用户的问题。

铁律：
1. 回答中的每一个数字都必须来自工具返回结果，不许估算、不许编造。
2. 工具返回的字段如果是 null 或标注为不可用，就直接说"该数据不可用"，禁止当成 0。
3. 数据不足以支撑结论时，明确说"根据现有数据无法判断"，不要强行下结论。
4. 不对个人做绩效评价或排名，只对团队/流程做诊断。
5. 先取数据再回答，不要凭常识猜。通常需要 2 到 4 次工具调用。
6. 给出规范类建议前，先调用 search_knowledge 查团队规范，并用
   [来源: 文件名#章节] 标注出处；查不到就明说没有依据，不要自己编规范。
7. 被分析的仓库通常**不是**本团队的项目。引用规范时只能说「按本团队规范对照」，
   绝不可直接断言该仓库「不合规」——它不是这份规范的缔约方。
8. 工具调用必须使用用户消息中给出的 owner 和 name，严禁猜测或替换成别的仓库。
9. 这是一个**面向 Java 仓库**的效能分析工具。分析 Java 项目时，先调 repo_structure
   看构建工具、模块划分和测试比例；要了解项目自身约定（怎么构建、依赖怎么管），
   用 search_repo 查它自己的文档，不要拿团队规范当项目的技术事实。

输出要求：
- 用中文回答，先给结论，再给支撑数据。
- 提到数字时注明来源工具，例如「（来源：pull_health）」。
- 最后给 1 到 3 条可执行的改进建议，不要空话。
"""

MAX_REPEAT = 2

_CITATION = re.compile(r"\[来源[:：]\s*([^\]]+?)\s*\]")


def extract_sources(observation: str) -> set[str]:
    """从 search_knowledge 的返回里取出真实存在的 source，作为引用白名单。"""
    try:
        payload = json.loads(observation)
    except (json.JSONDecodeError, TypeError):
        return set()
    return {
        item["source"]
        for item in (payload.get("results") or [])
        if isinstance(item, dict) and item.get("source")
    }


def validate_citations(answer: str, known: set[str]) -> tuple[str, list[str]]:
    """校验答案里的 [来源: ...]。

    模型会编造出不存在的章节。这里拿工具真实返回的 source 做白名单，
    对不上的就地标注，宁可让用户看到「未验证」，也不让假引用蒙混过去。
    """
    invalid: list[str] = []

    def replace(match: re.Match[str]) -> str:
        cited = match.group(1).strip()
        if cited in known or any(source.startswith(cited) for source in known):
            return match.group(0)
        invalid.append(cited)
        return f"[来源: {cited} ⚠未验证]"

    return _CITATION.sub(replace, answer), invalid


@dataclass
class Step:
    index: int
    thought: str = ""
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    observation: str | None = None
    ok: bool = True


@dataclass
class AgentResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str = "answered"

    @property
    def tool_calls(self) -> int:
        return sum(1 for step in self.steps if step.tool)


def run(
    question: str,
    registry: ToolRegistry,
    llm: LLMClient,
    max_steps: int = 8,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    emit = on_event or (lambda event: None)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    steps: list[Step] = []
    seen: dict[tuple[str, str], int] = {}
    known_sources: set[str] = set()
    specs = registry.specs()

    for index in range(1, max_steps + 1):
        response = llm.chat(messages, tools=specs, temperature=0)
        message = (response.get("choices") or [{}])[0].get("message") or {}
        content = (message.get("content") or "").strip()
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            answer, invalid = validate_citations(content, known_sources)
            steps.append(Step(index=index, thought=answer))
            emit({"type": "answer", "content": answer})
            if invalid:
                emit({"type": "citation_warning", "invalid": invalid})
            return AgentResult(answer, steps, messages, "answered")

        if content:
            emit({"type": "thought", "index": index, "content": content})

        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })

        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            raw_arguments = function.get("arguments") or "{}"

            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}

            # 一次工具调用对应一条轨迹，否则并行调用会互相覆盖
            step = Step(index=index, tool=name, arguments=arguments)
            steps.append(step)

            # 死循环护栏：同参数重复调用不消耗真实请求，直接提醒模型
            key = (name, raw_arguments)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > MAX_REPEAT:
                observation = json.dumps(
                    {"error": "相同参数已重复调用，请基于已有结果作答或换一个工具"},
                    ensure_ascii=False,
                )
                step.observation = observation
                step.ok = False
                emit({"type": "tool_call", "tool": name, "arguments": arguments,
                      "repeat": True})
                messages.append({
                    "role": "tool", "tool_call_id": call.get("id"),
                    "content": observation,
                })
                continue

            emit({"type": "tool_call", "tool": name, "arguments": arguments})

            observation, is_error = registry.call(name, raw_arguments)
            step.observation = observation
            step.ok = not is_error
            emit({"type": "observation", "tool": name, "ok": not is_error,
                  "content": observation[:1000]})

            if name in ("search_knowledge", "search_repo") and not is_error:
                known_sources.update(extract_sources(observation))

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": observation,
            })

    emit({"type": "stop", "reason": "max_steps"})
    return AgentResult(
        "已达到最大推理步数，未能形成完整结论。以下是已收集到的信息，"
        "建议缩小问题范围后重试。",
        steps,
        messages,
        "max_steps",
    )
