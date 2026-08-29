"""执行器（Executor）：按计划逐步骤调用工具，收集中间结果。

每个步骤的执行结果包含：动作、输入、输出、耗时、错误、尝试次数（attempts）、
是否走了降级（degraded）。工具调用失败不会中断整个问答：该步骤标记 error，后续继续。

容错策略（S1 工具层强化）：
- 调用前先按工具 JSON Schema 校验参数（ToolValidationError → 不重试，直接失败）；
- 其他异常按 tools.max_retries 重试（总尝试次数 = max_retries + 1）；
- search_knowledge_base 重试耗尽仍失败且启用了 web_search 时，用同一 query 降级
  调 Web 搜索（degraded=True，ok 保持 True）；降级也失败则按普通失败处理。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .logger import get_logger
from .planner import Plan
from .tools import Tool, ToolContext, ToolValidationError, validate_tool_input

LOG = get_logger("executor")

_FALLBACK_PREFIX = "[降级] 知识库检索失败，已改用 Web 搜索\n"


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
    attempts: int = 1      # 本步骤实际尝试次数（含降级调用）
    degraded: bool = False  # 知识库检索失败后是否走了 Web 降级

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

        # 1) 参数校验：不符合 schema 的入参无法靠重试恢复，直接失败
        try:
            kwargs = validate_tool_input(tool, dict(step.input or {}))
        except ToolValidationError as exc:
            result.ok = False
            result.error = f"参数校验失败: {exc}"
            result.output = {"text": f"工具执行失败：{result.error}"}
            result.latency_s = time.monotonic() - t0
            return result

        max_retries = max(0, int(getattr(self.config, "tools_max_retries", 1) or 0))

        # 2) 执行：失败按 max_retries 重试（总尝试次数 = max_retries + 1）
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            result.attempts = attempt + 1
            try:
                result.output = tool(self.ctx, **kwargs)
                result.latency_s = time.monotonic() - t0
                return result
            except Exception as exc:  # noqa: BLE001 - 工具错误不中断问答
                last_exc = exc
                LOG.warning(
                    "工具 %s 第 %d/%d 次执行失败: %s", step.action, attempt + 1, max_retries + 1, exc
                )
        kb_error = f"{type(last_exc).__name__}: {last_exc}"

        # 3) KB→Web 降级：知识库检索重试耗尽仍失败，且注册表里有启用的 web_search 工具
        if (
            step.action == "search_knowledge_base"
            and "web_search" in self.tools
            and bool(getattr(self.config, "kb_fallback_web", True))
        ):
            fallback_err = self._try_web_fallback(result, kwargs, max_retries)
            result.latency_s = time.monotonic() - t0
            if fallback_err is None:
                return result  # 降级成功：degraded=True、ok 保持 True
            # 降级也失败：按普通失败处理，error 汇总两次错误
            result.ok = False
            result.error = f"知识库检索失败: {kb_error}；{fallback_err}"
            result.output = {"text": f"工具执行失败：{result.error}"}
            return result

        # 4) 普通失败（重试耗尽，无降级路径）
        result.ok = False
        result.error = kb_error
        result.output = {"text": f"工具执行失败：{result.error}"}
        result.latency_s = time.monotonic() - t0
        return result

    def _try_web_fallback(self, result: StepResult, kb_kwargs: dict, max_retries: int) -> str | None:
        """用同一 query 调 web_search 降级。

        成功：写回 result.output（text 加降级前缀）、degraded=True，返回 None；
        失败：返回错误描述（调用方汇总进 result.error）。
        """
        web_tool = self.tools["web_search"]
        try:
            web_kwargs = validate_tool_input(web_tool, {"query": str(kb_kwargs.get("query", ""))})
        except ToolValidationError as exc:
            return f"Web 降级失败: 参数校验失败: {exc}"
        last_err = ""
        for attempt in range(max_retries + 1):
            result.attempts += 1
            try:
                out = web_tool(self.ctx, **web_kwargs)
            except Exception as exc:  # noqa: BLE001 - 降级失败也不中断问答
                last_err = f"Web 降级失败: {type(exc).__name__}: {exc}"
                LOG.warning(
                    "工具 web_search 降级第 %d/%d 次失败: %s", attempt + 1, max_retries + 1, exc
                )
                continue
            if isinstance(out, dict) and "text" in out:
                out["text"] = _FALLBACK_PREFIX + str(out["text"])
            result.output = out
            result.degraded = True
            return None
        return last_err
