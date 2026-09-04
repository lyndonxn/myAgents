"""G10 检索迭代循环测试（ACC-U10-01/02 + 预算约束）。

全部离线：ScriptedLLM 按脚本返回规划/反思/合成文本；KB 工具按查询词区分命中与未命中。
覆盖：首轮未命中 → 反思改写 query 二轮命中且总搜索 ≤ 预算（U10-01）；
预算耗尽 → 反思提示禁检 + 硬约束剔除检索补步、按未命中路径披露（U10-02）；
build_reflect_prompt 预算注入；配置预算生效。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent import Agent
from agents.config import Config
from agents.llm import ChatResult, LLMClient
from agents.planner import build_reflect_prompt
from agents.tools import Tool, ToolContext

PLAN_ONE_SEARCH = json.dumps({
    "reasoning": "检索一步", "plan_summary": "检索",
    "steps": [{"action": "search_knowledge_base", "input": {"query": "RAG"}, "purpose": ""}],
}, ensure_ascii=False)

PLAN_THREE_SEARCH = json.dumps({
    "reasoning": "三连检索", "plan_summary": "三步检索",
    "steps": [{"action": "search_knowledge_base", "input": {"query": f"q{i}"}, "purpose": ""} for i in (1, 2, 3)],
}, ensure_ascii=False)


def reflect_json(query: str) -> str:
    return json.dumps({
        "need_more": True,
        "reasoning": f"首轮未命中，改写查询为「{query}」再检索",
        "steps": [{"action": "search_knowledge_base", "input": {"query": query}, "purpose": "改写再检索"}],
    }, ensure_ascii=False)


class QueryAwareLLM(LLMClient):
    """离线假 LLM：按脚本返回文本；记录 messages 供预算断言。"""

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


def query_aware_kb():
    """KB 工具：查询含「检索增强生成」才命中，否则 0 命中。"""

    def probe(ctx, query=""):
        if "检索增强生成" in query:
            return {"text": f"检索增强生成：先查资料再作答（{query}）", "sources": ["kb://rag.md"], "hit_count": 2}
        return {"text": "0 条相关片段", "hit_count": 0, "sources": []}

    return Tool(
        name="search_knowledge_base", description="测试 KB 工具",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        func=probe,
    )


def always_miss_kb():
    def probe(ctx, query=""):
        return {"text": "0 条相关片段", "hit_count": 0, "sources": []}

    return Tool(
        name="search_knowledge_base", description="测试 KB 工具",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        func=probe,
    )


def make_agent(llm, tool, raw: dict | None = None) -> Agent:
    merged: dict = {"planner": {"fast_path": False}, "tools": {"max_retries": 0}}
    for key, value in (raw or {}).items():  # 深合并，避免用例 planner 配置覆盖 fast_path
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    agent = Agent(Config(merged), llm=llm, lazy_index=True)
    agent._index_loaded = True
    agent.tools = {"search_knowledge_base": tool}
    agent._ctx = ToolContext()
    return agent


class SearchLoopTests(unittest.TestCase):
    """ACC-U10-01/02 + 预算注入。"""

    def test_build_reflect_prompt_budget_injection(self):
        """反思 prompt：预算注入三种形态（有剩余/已耗尽/未启用）。"""
        msgs = build_reflect_prompt("q", ["工具"], "", [], 3, search_budget=(1, 3))
        self.assertIn("已用 1 / 上限 3", msgs[1]["content"])
        self.assertIn("改写查询词", msgs[1]["content"])

        msgs = build_reflect_prompt("q", ["工具"], "", [], 3, search_budget=(3, 3))
        self.assertIn("预算已耗尽", msgs[1]["content"])
        self.assertIn("不得再提出任何 search_knowledge_base", msgs[1]["content"])

        msgs = build_reflect_prompt("q", ["工具"], "", [], 3)
        self.assertNotIn("搜索次数", msgs[1]["content"])

        print("✓ build_reflect_prompt：预算注入（有剩余/耗尽/未启用）")

    def test_acc_u10_01_rewrite_then_hit_within_budget(self):
        """多跳/改写题：首轮未命中 → 反思改写 query 二轮命中；总搜索 2 ≤ 预算 3。"""
        llm = QueryAwareLLM([PLAN_ONE_SEARCH, reflect_json("检索增强生成 的整体流程"), "答案[1]。"], Config({}))
        agent = make_agent(llm, query_aware_kb())
        answer = agent.ask("RAG 的流程是什么？")

        self.assertTrue(answer.ok, f"ask 不应失败: {answer.error}")
        searches = [s for s in answer.steps if s.action == "search_knowledge_base"]
        self.assertEqual(len(searches), 2, f"应有 2 次搜索（首轮+改写），实际 {len(searches)}")
        self.assertLessEqual(len(searches), agent.config.planner_max_search_calls)
        self.assertEqual(searches[0].output.get("hit_count"), 0, "首轮应未命中")
        self.assertEqual(searches[1].output.get("hit_count"), 2, "改写后二轮应命中")
        self.assertEqual(searches[1].input.get("query"), "检索增强生成 的整体流程", "补步应使用改写后的查询")
        self.assertEqual(answer.sources, ["kb://rag.md"], "来源来自二轮命中")
        # 反思 prompt 带预算信息（已用 1 / 上限 3）
        self.assertIn("已用 1 / 上限 3", llm.seen[1][1]["content"])

        print("✓ ACC-U10-01：改写再检索二轮命中，总搜索次数 ≤ 预算")

    def test_acc_u10_02_budget_exhausted_stops_iteration(self):
        """预算耗尽：反思提示禁检 + 硬约束剔除检索补步，按未命中路径披露。"""
        llm = QueryAwareLLM([PLAN_THREE_SEARCH, reflect_json("q4 改写"), "未在知识库内找到相关内容[1]。"], Config({}))
        agent = make_agent(llm, always_miss_kb())
        answer = agent.ask("知识库里没有的问题")

        self.assertTrue(answer.ok)
        searches = [s for s in answer.steps if s.action == "search_knowledge_base"]
        self.assertEqual(len(searches), 3, f"预算 3：超出的检索补步应被剔除，实际 {len(searches)}")
        self.assertEqual(agent.config.planner_max_search_calls, 3)
        # 反思 prompt 明确「预算已耗尽」
        self.assertIn("预算已耗尽", llm.seen[1][1]["content"])
        # 未命中披露：合成提示要求按未命中口径回答
        synth_user = llm.seen[-1][1]["content"]
        self.assertIn("未在知识库内", synth_user)

        print("✓ ACC-U10-02：预算耗尽停止迭代，检索补步被剔除，未命中路径披露")

    def test_budget_config_enforced(self):
        """配置预算生效：max_search_calls=1 时，2 步计划的第 2 次检索……首轮按计划执行不受预算约束，
        反思补步在预算内才保留。"""
        plan_two = json.dumps({
            "reasoning": "两步检索", "plan_summary": "两步",
            "steps": [{"action": "search_knowledge_base", "input": {"query": f"q{i}"}, "purpose": ""} for i in (1, 2)],
        }, ensure_ascii=False)
        llm = QueryAwareLLM([plan_two, reflect_json("q3"), "答案[1]。"],
                            Config({"planner": {"max_search_calls": 1}}))
        agent = make_agent(llm, always_miss_kb(), {"planner": {"max_search_calls": 1}})
        self.assertEqual(agent.config.planner_max_search_calls, 1)
        answer = agent.ask("预算测试")

        searches = [s for s in answer.steps if s.action == "search_knowledge_base"]
        self.assertEqual(len(searches), 2, "计划内的 2 步不受反思预算追溯影响")
        # 反思补步被预算剔除（已用 2 ≥ 上限 1）
        self.assertIn("预算已耗尽", llm.seen[1][1]["content"])
        self.assertEqual(len(answer.steps), 2, "无其他类型补步可追加")

        print("✓ 配置预算生效：反思补步按 planner.max_search_calls 硬约束")


if __name__ == "__main__":
    unittest.main()
