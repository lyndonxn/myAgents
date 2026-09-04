"""S2 ReAct 迭代 + 反思重规划 + 步骤数据传递测试。

全部离线：LLM 用记录调用次数、按脚本返回文本的假客户端（子类化 LLMClient
覆写 _chat，不发真实请求）；工具用闭包实现；Agent 不加载真实索引（直接注入
假工具注册表）。覆盖 spec ACC-S2-01..04，以及 reflect 开关关闭、
planner_max_reflections=0 两种护栏行为与 Planner.reflect 直测。
"""
from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent import Agent
from agents.config import Config
from agents.executor import Executor
from agents.llm import ChatResult, LLMClient, LLMError
from agents.planner import Plan, PlanStep, Planner
from agents.tools import Tool, ToolContext


class ScriptedLLM(LLMClient):
    """离线假 LLM：按脚本顺序返回文本，记录每次调用与收到的 messages。"""

    def __init__(self, texts: list[str], config: Config):
        self.config = config
        self._texts = list(texts)
        self.calls = 0
        self.seen: list[list[dict]] = []

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


class _ListHandler(logging.Handler):
    """收集日志消息的内存 handler（用于断言占位符置空 warning）。"""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _make_tool(name: str, func, properties: dict, required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=f"测试工具 {name}",
        parameters={"type": "object", "properties": properties, "required": required},
        func=func,
    )


def _make_agent(llm: ScriptedLLM, tools: dict[str, Tool], raw: dict | None = None) -> Agent:
    """离线 Agent：不加载索引，直接注入假工具注册表与执行上下文。"""
    merged: dict = {"planner": {"fast_path": False}}  # G9：考察完整路径（调用方可覆盖）
    for key, value in (raw or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    agent = Agent(Config(merged), llm=llm, lazy_index=True)
    agent._index_loaded = True  # 跳过索引加载
    agent.tools = tools
    agent._ctx = ToolContext()
    return agent


def _plan_json(steps: list[dict], summary: str = "测试计划") -> str:
    return json.dumps({"reasoning": "测试规划", "steps": steps, "plan_summary": summary}, ensure_ascii=False)


def _failing_first_round_tools() -> dict[str, Tool]:
    def flaky(ctx, **kwargs):
        raise RuntimeError("失败")

    return {"flaky": _make_tool("flaky", flaky, {"query": {"type": "string"}}, ["query"])}


class ReActLoopTests(unittest.TestCase):
    """spec ACC-S2-01..04：占位符传递 / 反思重规划 / 步数硬上限 / 坏 JSON 静默，及护栏与直测。"""

    # ---------------- ACC-S2-01 步骤数据传递 ----------------

    def test_placeholder_field_and_full(self):
        """ACC-S2-01（前半）：@step:N.field 取上游字段、@step:N 取全文、子串替换、解析先于校验。"""
        received: list[dict] = []

        def probe(ctx, **kwargs):
            received.append(kwargs)
            return {"text": "步骤一全文", "query": "X", "top_k": 3}

        probe_tool = _make_tool(
            "probe", probe,
            {
                "q": {"type": "string"},
                "full": {"type": "string"},
                "mixed": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            [],
        )
        executor = Executor(Config({"tools": {"max_retries": 0}}), {"probe": probe_tool}, ToolContext())
        plan = Plan(steps=[
            PlanStep(action="probe", input={"q": "第一步"}, step_id=1),
            PlanStep(action="probe", input={
                "q": "@step:1.query",           # 上游 dict 的 query 字段
                "full": "@step:1",              # 上游输出全文 text
                "mixed": "前缀@step:1.query后缀",  # 占位符作为子串出现
                "top_k": "@step:1.top_k",       # 解析为 "3" 后再按 schema 纠偏为整数
            }, step_id=2),
        ])
        results = executor.execute(plan)

        self.assertTrue(results[0].ok)
        self.assertTrue(results[1].ok)
        self.assertEqual(received[0], {"q": "第一步"})
        self.assertEqual(
            received[1], {"q": "X", "full": "步骤一全文", "mixed": "前缀X后缀", "top_k": 3},
            f"占位符解析结果不符: {received[1]}",
        )
        self.assertIsInstance(received[1]["top_k"], int, "解析出的 '3' 应经 schema 纠偏为整数（解析先于校验）")
        # 只在执行时替换：Plan 的 step.input 保持占位符原样
        self.assertEqual(plan.steps[1].input["q"], "@step:1.query")
        self.assertEqual(plan.steps[1].input["full"], "@step:1")

        print("✓ ACC-S2-01 占位符解析（field/全文/子串替换/解析先于校验，Plan 不被改动）")

    def test_placeholder_upstream_missing_or_failed(self):
        """ACC-S2-01（后半）：上游步骤缺失或 ok=False → 相关值置空串、不崩溃、记录 warning。"""
        received: list[dict] = []

        def probe(ctx, **kwargs):
            received.append(kwargs)
            return {"text": "ok"}

        def broken(ctx, **kwargs):
            raise RuntimeError("上游失败")

        tools = {
            "probe": _make_tool("probe", probe, {"q": {"type": "string"}}, []),
            "broken": _make_tool("broken", broken, {"q": {"type": "string"}}, []),
        }
        executor = Executor(Config({"tools": {"max_retries": 0}}), tools, ToolContext())
        plan = Plan(steps=[
            PlanStep(action="broken", input={"q": "x"}, step_id=1),   # 执行失败
            PlanStep(action="probe", input={"q": "@step:1.text"}, step_id=2),  # 上游失败 → 置空
            PlanStep(action="probe", input={"q": "@step:9.query"}, step_id=3),  # 上游缺失 → 置空
        ])

        handler = _ListHandler()
        logger = logging.getLogger("executor")
        logger.addHandler(handler)
        try:
            results = executor.execute(plan)
        finally:
            logger.removeHandler(handler)

        self.assertTrue(results[1].ok)
        self.assertEqual(received[-2], {"q": ""}, "上游失败的引用应置空串且不崩溃")
        self.assertTrue(results[2].ok)
        self.assertEqual(received[-1], {"q": ""}, "缺失步骤的引用应置空串且不崩溃")
        joined = "\n".join(handler.messages)
        self.assertIn("@step:1.text", joined)
        self.assertIn("未成功", joined, f"应记录上游失败的 warning: {joined}")
        self.assertIn("@step:9.query", joined)
        self.assertIn("缺失", joined, f"应记录缺失步骤的 warning: {joined}")

        print("✓ ACC-S2-01 上游缺失/失败置空并记录 warning（不崩溃）")

    def test_placeholder_non_dict_output(self):
        """上游输出非 dict / 无 text 字段时的占位符语义：@step:N 用 str(output)，@step:N.field 置空。"""
        received: list[dict] = []

        def probe(ctx, **kwargs):
            received.append(kwargs)
            return {"text": "ok"}

        def text_tool(ctx, **kwargs):
            return "纯文本输出"

        def no_text_tool(ctx, **kwargs):
            return {"query": "X"}  # dict 但没有 text

        tools = {
            "probe": _make_tool("probe", probe, {"a": {"type": "string"}, "b": {"type": "string"}}, []),
            "text": _make_tool("text", text_tool, {"q": {"type": "string"}}, []),
            "no_text": _make_tool("no_text", no_text_tool, {"q": {"type": "string"}}, []),
        }
        executor = Executor(Config({"tools": {"max_retries": 0}}), tools, ToolContext())
        plan = Plan(steps=[
            PlanStep(action="text", input={"q": "x"}, step_id=1),
            PlanStep(action="no_text", input={"q": "y"}, step_id=2),
            PlanStep(action="probe", input={"a": "@step:1", "b": "@step:2.detail"}, step_id=3),
        ])
        results = executor.execute(plan)

        self.assertTrue(results[2].ok)
        self.assertEqual(received[0], {"a": "纯文本输出", "b": ""}, f"非 dict 输出语义不符: {received[0]}")

        # dict 无 text：@step:N 用 str(output)；缺字段：整体置空
        received.clear()
        plan2 = Plan(steps=[
            PlanStep(action="no_text", input={"q": "y"}, step_id=1),
            PlanStep(action="probe", input={"a": "@step:1", "b": "@step:1.missing"}, step_id=2),
        ])
        executor2 = Executor(Config({"tools": {"max_retries": 0}}), tools, ToolContext())
        executor2.execute(plan2)
        self.assertEqual(received[0]["a"], str({"query": "X"}), f"dict 无 text 应 str(output): {received[0]['a']}")
        self.assertEqual(received[0]["b"], "")

        print("✓ 占位符对非 dict / 无 text 输出的降级语义")

    # ---------------- ACC-S2-02 反思重规划 ----------------

    def test_reflection_on_failure_appends_steps(self):
        """ACC-S2-02：首轮失败触发反思，need_more 补步被执行，reflections 非空、rounds==2。"""
        kb_calls: list[str] = []

        def flaky(ctx, **kwargs):
            raise RuntimeError("首轮检索失败")

        def probe(ctx, topic=""):
            return {"text": f"关于{topic}的初步信息", "topic": topic}

        def fake_kb(ctx, query="", top_k=5):
            kb_calls.append(query)
            return {"text": f"补充检索：{query}", "sources": ["kb://x.md"]}

        tools = {
            "flaky": _make_tool("flaky", flaky, {"query": {"type": "string"}}, ["query"]),
            "probe": _make_tool("probe", probe, {"topic": {"type": "string"}}, []),
            "search_knowledge_base": _make_tool(
                "search_knowledge_base", fake_kb, {"query": {"type": "string"}}, ["query"]
            ),
        }
        plan_json = _plan_json([
            {"action": "flaky", "input": {"query": "q1"}, "purpose": "会失败的一步"},
            {"action": "probe", "input": {"topic": "X"}, "purpose": "拿初步信息"},
        ])
        reflect_json = json.dumps({
            "need_more": True,
            "reasoning": "步骤1失败，需要用步骤2的主题补充检索",
            "steps": [
                {"action": "search_knowledge_base", "input": {"query": "@step:2.topic 补充"}, "purpose": "补步检索"},
            ],
        }, ensure_ascii=False)

        llm = ScriptedLLM([plan_json, reflect_json, "最终答案"], Config({"tools": {"max_retries": 0}}))
        agent = _make_agent(llm, tools, {"tools": {"max_retries": 0}})
        answer = agent.ask("测试问题")

        self.assertTrue(answer.ok, f"补步成功后 ask 不应失败: {answer.error}")
        self.assertEqual(answer.final_answer, "最终答案")
        self.assertEqual(answer.plan.rounds, 2, f"补步后 rounds 应为 2，实际 {answer.plan.rounds}")
        self.assertEqual(answer.plan.reflections, ["步骤1失败，需要用步骤2的主题补充检索"])
        self.assertEqual([s.step_id for s in answer.plan.steps], [1, 2, 3], "补步 step_id 应续号")
        self.assertTrue(len(answer.steps) == 3 and answer.steps[2].ok, "补步应被执行且成功")
        self.assertEqual(kb_calls, ["X 补充"], f"补步应执行且 @step:2.topic 解析为首轮输出字段: {kb_calls}")
        # 反思 prompt 携带轨迹（含失败步骤 ok=false 与输出摘要）
        reflect_user = llm.seen[1][1]["content"]
        self.assertIn('"ok": false', reflect_user)
        self.assertIn("flaky", reflect_user)
        self.assertEqual(llm.calls, 3, f"规划+反思+合成共 3 次调用，实际 {llm.calls}")
        self.assertEqual(answer.llm_calls, 3)

        print("✓ ACC-S2-02 首轮失败触发反思、补步执行、reflections 非空、rounds==2")

    # ---------------- ACC-S2-03 步数硬上限 ----------------

    def test_hard_cap_truncates_and_stops_reflection(self):
        """ACC-S2-03：反思连续返回步骤使总步数将超 max_steps*2 → 截断且不再新增反思轮。"""
        probe_calls: list[dict] = []

        def probe(ctx, q=""):
            probe_calls.append({"q": q})
            return {"text": f"结果{len(probe_calls)}"}

        tools = {"probe": _make_tool("probe", probe, {"q": {"type": "string"}}, [])}

        def reflect_json(round_no: int) -> str:
            return json.dumps({
                "need_more": True,
                "reasoning": f"第{round_no}轮补步",
                "steps": [
                    {"action": "probe", "input": {"q": f"补{round_no}-{i}"}, "purpose": "补步"}
                    for i in range(3)  # 每轮都想补 3 步，超出预算
                ],
            }, ensure_ascii=False)

        plan_json = _plan_json([
            {"action": "probe", "input": {"q": "s1"}, "purpose": ""},
            {"action": "probe", "input": {"q": "s2"}, "purpose": ""},
        ])
        raw = {"planner": {"max_steps": 2, "reflect": True, "max_reflections": 3}, "tools": {"max_retries": 0}}
        llm = ScriptedLLM([plan_json, reflect_json(1), "答案"], Config({"planner": {"fast_path": False}, **raw}))
        agent = _make_agent(llm, tools, raw)
        answer = agent.ask("护栏问题")

        # 首轮 2 步 + 补步预算 2（第 3 个被截断）= 4 == max_steps*2；达到硬上限后不再反思
        self.assertEqual([s.step_id for s in answer.plan.steps], [1, 2, 3, 4], "应截断到硬上限 4 步并续号")
        self.assertEqual(len(probe_calls), 4)
        self.assertEqual(probe_calls[-1], {"q": "补1-1"}, f"只执行截断后的补步: {probe_calls}")
        self.assertEqual(answer.plan.rounds, 2)
        self.assertEqual(len(answer.plan.reflections), 1)
        self.assertEqual(llm.calls, 3, f"达到硬上限后不应再反思（规划+反思+合成=3），实际 {llm.calls}")
        self.assertEqual(answer.final_answer, "答案")

        print("✓ ACC-S2-03 总步数超 planner_max_steps*2 截断且不再反思")

    # ---------------- ACC-S2-04 反思坏 JSON 静默 ----------------

    def test_bad_reflection_json_is_silent(self):
        """ACC-S2-04：反思返回坏 JSON 且修复轮也坏 → 无异常、无补步、答案正常生成。"""
        probe_calls: list[dict] = []

        def probe(ctx, q=""):
            probe_calls.append({"q": q})
            return {"text": "已有足够信息"}

        tools = {"probe": _make_tool("probe", probe, {"q": {"type": "string"}}, [])}
        plan_json = _plan_json([{"action": "probe", "input": {"q": "s1"}, "purpose": ""}])

        # 反思初始输出与修复轮都坏（json_repair_rounds 默认 1）
        llm = ScriptedLLM([plan_json, "完全不是 JSON", "仍然不是 JSON", "最终答案"], Config({}))
        agent = _make_agent(llm, tools)
        answer = agent.ask("坏 JSON 问题")

        self.assertTrue(answer.ok, f"反思失败不应影响回答: {answer.error}")
        self.assertEqual(answer.final_answer, "最终答案")
        self.assertTrue(len(answer.plan.steps) == 1 and len(probe_calls) == 1, "不应有补步")
        self.assertEqual(answer.plan.rounds, 1)
        self.assertEqual(answer.plan.reflections, [])
        self.assertEqual(llm.calls, 4, f"规划1+反思2（含修复轮）+合成1=4，实际 {llm.calls}")

        print("✓ ACC-S2-04 反思坏 JSON（修复轮也坏）→ 无异常、无补步、答案正常")

    # ---------------- 反思开关护栏 ----------------

    def test_reflect_disabled_skips_reflection(self):
        """reflect 开关关闭：即使首轮有失败步骤也不反思。"""
        plan_json = _plan_json([{"action": "flaky", "input": {"query": "q"}, "purpose": ""}])
        llm = ScriptedLLM(
            [plan_json, "答案"],
            Config({"planner": {"reflect": False}, "tools": {"max_retries": 0}}),
        )
        agent = _make_agent(llm, _failing_first_round_tools(), {"planner": {"reflect": False}, "tools": {"max_retries": 0}})
        answer = agent.ask("开关关闭")

        self.assertTrue(answer.ok)  # 步骤失败不等于问答失败
        self.assertEqual(answer.plan.rounds, 1)
        self.assertEqual(answer.plan.reflections, [])
        self.assertEqual(len(answer.plan.steps), 1)
        self.assertEqual(llm.calls, 2, f"关闭反思时只有规划+合成两次调用，实际 {llm.calls}")

        print("✓ reflect 开关关闭时不反思（即使有失败步骤）")

    def test_max_reflections_zero_skips_reflection(self):
        """planner_max_reflections=0：不反思。"""
        plan_json = _plan_json([{"action": "flaky", "input": {"query": "q"}, "purpose": ""}])
        llm = ScriptedLLM(
            [plan_json, "答案"],
            Config({"planner": {"max_reflections": 0}, "tools": {"max_retries": 0}}),
        )
        agent = _make_agent(llm, _failing_first_round_tools(), {"planner": {"max_reflections": 0}, "tools": {"max_retries": 0}})
        answer = agent.ask("零轮反思")

        self.assertTrue(answer.ok)
        self.assertEqual(answer.plan.rounds, 1)
        self.assertEqual(answer.plan.reflections, [])
        self.assertEqual(len(answer.plan.steps), 1)
        self.assertEqual(llm.calls, 2, f"max_reflections=0 时不应反思，实际 {llm.calls}")

        print("✓ planner_max_reflections=0 时不反思")

    # ---------------- Planner.reflect 直测 ----------------

    def test_planner_reflect_direct(self):
        """Planner.reflect 直测：need_more=false → 空补步；结构不合法 → LLMError；超预算截断。"""
        cfg = Config({})
        trajectory = [{"step_id": 1, "action": "probe", "ok": True, "error": "", "output": "x"}]

        llm = ScriptedLLM(['{"need_more": false, "reasoning": "够了", "steps": [{"action": "probe"}]}'], cfg)
        plan = Planner(cfg, llm).reflect("q", ["工具描述"], "", trajectory, remaining_budget=3)
        self.assertEqual(plan.steps, [])
        self.assertEqual(plan.reasoning, "够了")

        # 结构不合法（解析成 list）→ 抛 LLMError（调用方静默吞掉）
        llm2 = ScriptedLLM(["[1, 2]"], cfg)
        with self.assertRaises(LLMError) as cm:
            Planner(cfg, llm2).reflect("q", [], "", [], remaining_budget=3)
        self.assertIn("反思解析失败", str(cm.exception))

        # 补步超出 remaining_budget → 截断；step_id 为临时编号（调用方续号）
        steps = [
            {"action": "probe", "input": {"q": "a"}, "purpose": ""},
            {"action": "probe", "input": {"q": "b"}, "purpose": ""},
            {"action": "probe", "input": {"q": "c"}, "purpose": ""},
        ]
        llm3 = ScriptedLLM([json.dumps({"need_more": True, "reasoning": "补", "steps": steps})], cfg)
        plan3 = Planner(cfg, llm3).reflect("q", [], "", [], remaining_budget=2)
        self.assertEqual(len(plan3.steps), 2)
        self.assertEqual([s.step_id for s in plan3.steps], [1, 2])

        print("✓ Planner.reflect 直测（need_more=false / 坏结构抛 LLMError / 预算截断）")


if __name__ == "__main__":
    unittest.main()
