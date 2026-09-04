"""G9 速度与成本包测试（ACC-U9-01/02 + 快路径门控 + prompt 重排 + 证据压缩）。

全部离线：ScriptedLLM 记录调用与 kwargs；KB 工具注入固定输出。
覆盖：简单事实题 ≤1 次规划调用（快路径跳过规划与反思）、多跳题回归走完整规划、
门控开关、合成 prompt 稳定前缀重排（上下文在前、历史与问题在后）、
synthesis.max_tokens 生效、compress_evidence 压缩 ≥30% 且保留相关句、
端到端合成输入缩减。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent import Agent, compress_evidence, _is_simple_question
from agents.config import Config
from agents.executor import StepResult
from agents.llm import ChatResult, LLMClient
from agents.memory import SessionMemory
from agents.planner import Plan, PlanStep
from agents.tools import Tool, ToolContext

REFLECT_NONE_JSON = json.dumps({"need_more": False, "reasoning": "信息足够", "steps": []}, ensure_ascii=False)
PLAN_JSON = json.dumps({
    "reasoning": "完整规划", "plan_summary": "检索一步",
    "steps": [{"action": "search_knowledge_base", "input": {"query": "RAG"}, "purpose": ""}],
}, ensure_ascii=False)


class ScriptedLLM(LLMClient):
    """离线假 LLM：记录 messages 与 kwargs（验证 max_tokens 传递）。"""

    def __init__(self, texts: list[str], config: Config):
        self.config = config
        self._texts = list(texts)
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.kwargs_seen: list[dict] = []

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        self.kwargs_seen.append(dict(kwargs))
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


def kb_hit_tool():
    def probe(ctx, query=""):
        return {"text": f"检索 {query} 的片段", "sources": ["kb://a.md"], "hit_count": 2}
    return Tool(
        name="search_knowledge_base", description="测试 KB 工具",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        func=probe,
    )


def make_agent(llm, raw: dict | None = None) -> Agent:
    agent = Agent(Config(raw or {}), llm=llm, lazy_index=True)
    agent._index_loaded = True
    agent.tools = {"search_knowledge_base": kb_hit_tool()}
    agent._ctx = ToolContext()
    return agent


class FastPathTests(unittest.TestCase):
    """ACC-U9-01/02：快路径生效与回归。"""

    def test_acc_u9_01_simple_question_skips_planner(self):
        """简单事实题（无历史）：全程 ≤1 次规划调用（快路径 0 次规划），答案与来源正常。"""
        llm = ScriptedLLM(["RAG 是先查资料再作答[1]。"], Config({}))
        agent = make_agent(llm)
        answer = agent.ask("什么是 RAG")

        self.assertTrue(answer.ok, f"ask 不应失败: {answer.error}")
        self.assertEqual(llm.calls, 1, "快路径应只有合成 1 次 LLM 调用（0 次规划、跳过反思）")
        self.assertEqual(len(answer.plan.steps), 1)
        self.assertEqual(answer.plan.steps[0].action, "search_knowledge_base")
        self.assertIn("快路径", answer.plan.plan_summary)
        self.assertEqual(answer.sources, ["kb://a.md"], "来源正常收集")
        self.assertTrue(answer.final_answer)

        print("✓ ACC-U9-01：简单事实题 0 次规划调用（快路径），答案与来源正常")

    def test_acc_u9_02_multi_hop_uses_full_planner(self):
        """多跳/对比题：快路径不触发，走完整规划（回归）。"""
        llm = ScriptedLLM([PLAN_JSON, "对比答案[1]。"], Config({}))
        agent = make_agent(llm)
        answer = agent.ask("对比 RAG 和混合检索的优劣，分别说明适用场景")

        self.assertTrue(answer.ok)
        self.assertEqual(llm.calls, 3, "完整路径 = 规划 + 反思 + 合成")
        planner_user = llm.seen[0][1]["content"]
        self.assertIn("search_knowledge_base", planner_user, "第一次调用应是规划器（含工具清单）")

        print("✓ ACC-U9-02：多跳题快路径不触发，行为与现版本一致")

    def test_fast_path_gate_config_and_heuristics(self):
        """门控：配置关闭走完整规划；启发式规则（长度/多问句/多跳线索）正确。"""
        llm = ScriptedLLM([PLAN_JSON, REFLECT_NONE_JSON, "答案[1]。"], Config({"planner": {"fast_path": False}}))
        agent = make_agent(llm, {"planner": {"fast_path": False}})
        agent.ask("什么是 RAG")
        self.assertEqual(llm.calls, 3, "fast_path=false 时简单题也走完整规划（规划+反思+合成）")

        # 启发式：短、单问句、无线索 → 简单；长/多问句/多跳线索 → 完整
        self.assertTrue(_is_simple_question("什么是 RAG"))
        self.assertFalse(_is_simple_question("对比 RAG 与混合检索，分别说明优劣"))
        self.assertFalse(_is_simple_question("什么是 RAG？它和混合检索的关系？"))
        self.assertFalse(_is_simple_question("x" * 100))

        print("✓ 门控：配置开关生效，启发式规则正确")

    def test_history_disables_fast_path(self):
        """有会话历史（追问场景）不走红路径：改写 + 规划照常。"""
        llm = ScriptedLLM(
            ["改写：RAG 定义", PLAN_JSON, REFLECT_NONE_JSON, "追问答案[1]。"], Config({})
        )
        agent = make_agent(llm)
        agent.memory.add("什么是 RAG", "RAG 是检索增强生成", ["kb://a.md"], "s")
        answer = agent.ask("它有什么好处")
        self.assertTrue(answer.ok)
        self.assertEqual(llm.calls, 4, "有历史 = 改写 + 规划 + 反思 + 合成")

        print("✓ 有历史追问不走快路径（回归正常）")


class SynthesisOptimizationTests(unittest.TestCase):
    """prompt 重排 + max_tokens + 证据压缩。"""

    def _agent(self, raw: dict | None = None) -> tuple[Agent, ScriptedLLM]:
        llm = ScriptedLLM(["合成答案[1]。"], Config(raw or {}))
        return make_agent(llm, raw), llm

    def _steps_with_evidence(self, text: str) -> list[StepResult]:
        return [StepResult(step_id=1, action="search_knowledge_base",
                           output={"text": text, "sources": ["kb://a.md"], "hit_count": 2})]

    def test_prompt_reorder_stable_prefix_first(self):
        """重排：工具结果（稳定前缀）在「用户问题」之前；历史在问题之前、上下文之后。"""
        agent, llm = self._agent()
        evidence = "短证据文本。"
        agent._synthesize(
            "什么是 RAG", Plan(plan_summary="检索一步"), self._steps_with_evidence(evidence),
            history_text="用户: 之前的问题\n助手: 之前的回答",
        )
        user = llm.seen[0][1]["content"]
        self.assertLess(user.index("以下是工具执行结果"), user.index("用户问题："),
                        "证据上下文（稳定前缀）应在问题之前")
        self.assertLess(user.index("对话历史"), user.index("用户问题："),
                        "历史在问题之前")
        self.assertGreater(user.index("对话历史"), user.index("以下是工具执行结果"),
                           "历史在稳定前缀之后（变化部分靠后）")

        print("✓ prompt 重排：稳定前缀在前，历史与问题在后（缓存友好）")

    def test_synthesis_max_tokens_applied(self):
        """synthesis.max_tokens 传递到 LLM 调用（默认 1024，可配）。"""
        agent, llm = self._agent()
        agent._synthesize("q", Plan(), self._steps_with_evidence("证据。"))
        self.assertEqual(llm.kwargs_seen[0].get("max_tokens"), 1024)

        agent2, llm2 = self._agent({"synthesis": {"max_tokens": 512}})
        agent2._synthesize("q", Plan(), self._steps_with_evidence("证据。"))
        self.assertEqual(llm2.kwargs_seen[0].get("max_tokens"), 512)

        print("✓ synthesis.max_tokens：默认 1024、可配并传递到 LLM")

    def test_compress_evidence_reduces_30_percent(self):
        """证据压缩：构造一半无关句的文本 → 压缩 ≥30%，相关关键词保留。"""
        query = "RAG 的整体流程包括哪些环节"
        parts = []
        for i in range(4):
            parts.append(f"RAG 的整体流程包含检索、重排与生成等环节，第 {i} 步依赖上游输出。")
            parts.append(f"今天天气不错，公园里的花朵在微风中摇曳，第 {i} 处风景令人心旷神怡。")
            parts.append(f"隔壁餐厅新推出的甜品套餐受到了顾客的普遍欢迎，销量稳步上升。")
        text = "".join(parts)
        compressed = compress_evidence(text, query)

        self.assertLessEqual(len(compressed), len(text) * 0.7, f"压缩后应 ≤70%，实际 {len(compressed)}/{len(text)}")
        self.assertIn("整体流程", compressed)
        self.assertIn("检索", compressed)
        self.assertNotIn("甜品套餐", compressed)
        # 相关句全部保留（4 句相关，均 ≤ 预算）
        self.assertEqual(compressed.count("RAG 的整体流程"), 4)

        print(f"✓ 证据压缩：{len(text)}→{len(compressed)} 字符（降 {100 - len(compressed) * 100 // len(text)}%），相关句保留")

    def test_compress_evidence_guards(self):
        """压缩守卫：短文本不动、无相关句不动、全相关不动、空输入安全。"""
        self.assertEqual(compress_evidence("短文本。", "查询"), "短文本。")
        irrelevant = "。".join(["今天天气不错我们在公园散步"] * 20) + "。"
        self.assertEqual(compress_evidence(irrelevant, "RAG 检索"), irrelevant, "无相关句应原样返回")
        all_relevant = "。".join([f"RAG 检索流程第 {i} 步" for i in range(20)]) + "。"
        self.assertEqual(compress_evidence(all_relevant, "RAG 检索"), all_relevant, "全相关无收益应原样返回")
        self.assertEqual(compress_evidence("", "q"), "")

        print("✓ 压缩守卫：短文本/无相关/全相关/空输入安全")

    def test_synthesize_applies_compression(self):
        """端到端：长证据经 _synthesize 后送入 LLM 的上下文显著缩减。"""
        query = "RAG 的整体流程"
        parts = []
        for i in range(4):
            parts.append(f"RAG 的整体流程包含检索、重排与生成等环节，第 {i} 步依赖上游输出。")
            parts.append(f" unrelated 无关句子 {i}：街角的咖啡店今天打八折，生意兴隆，顾客排起长队。")
        evidence = "".join(parts)

        agent, llm = self._agent()
        agent._synthesize(query, Plan(plan_summary="检索"), self._steps_with_evidence(evidence))
        user = llm.seen[0][1]["content"]
        ctx_start = user.index("【工具: search_knowledge_base】")
        ctx_end = user.index("\n\n请基于以上信息回答")
        context_len = ctx_end - ctx_start
        self.assertLess(context_len, len(evidence), "压缩后上下文应短于原证据")
        self.assertNotIn("咖啡店", user, "无关句应被裁剪")

        # 关闭压缩 → 原文送入
        agent2, llm2 = self._agent({"synthesis": {"evidence_compression": False}})
        agent2._synthesize(query, Plan(plan_summary="检索"), self._steps_with_evidence(evidence))
        user2 = llm2.seen[0][1]["content"]
        self.assertIn("咖啡店", user2, "关闭压缩时应保留原文")

        print("✓ 端到端证据压缩：合成上下文缩减、无关句剔除、开关生效")


if __name__ == "__main__":
    unittest.main()
