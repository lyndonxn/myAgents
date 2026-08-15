"""执行器（Executor）：按计划逐步骤调用工具，收集中间结果。

每个步骤的执行结果包含：动作、输入、输出、耗时、错误。
工具调用失败不会中断整个问答：该步骤标记 error，后续继续。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .planner import Plan
from .tools import Tool, ToolContext


@dataclass
class StepResult:
    step_id: int
    action: str
    input: dict = field(default_factory=dict)
    purpose: str = ""
    output: Any = None
    error: str = ""
    latency_s: float = 0.0
    ok: bool = True

    def display(self) -> str:
        head = ""
        if isinstance(self.output, dict) and "text" in self.output:
            head = str(self.output["text"])
        elif self.output is not None:
            head = str(self.output)
        return f"步骤{self.step_id} [{self.action}] ({self.latency_s:.1f}s):\n{head[:600]}"


class Executor:
    def __init__(self, config, tools: dict[str, Tool], ctx: ToolContext):
        self.config = config
        self.tools = tools
        self.ctx = ctx

    def execute(self, plan: Plan) -> list[StepResult]:
        results: list[StepResult] = []
        for step in plan.steps:
            result = self._run_step(step.step_id, step)
            results.append(result)
        return results

    def _run_step(self, step_id: int, step) -> StepResult:
        result = StepResult(step_id=step_id, action=step.action, input=step.input, purpose=step.purpose)
        tool = self.tools.get(step.action)
        if step.action == "none" or tool is None:
            result.output = {"text": "（无工具调用）"}
            return result
        t0 = time.monotonic()
        try:
            kwargs = dict(step.input or {})
            result.output = tool(self.ctx, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 工具错误不中断问答
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            result.output = {"text": f"工具执行失败：{result.error}"}
        finally:
            result.latency_s = time.monotonic() - t0
        return result
