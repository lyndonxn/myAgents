"""ReAct 子图测试（spec/09 T-011，PH3-ACC-002/003）。

全部离线：脚本 LLM（plan JSON → reflect JSON → 合成文本），calculator/search
工具本地执行，不联网。覆盖：
- PH3-ACC-002：G9 快路径条件路由到单发分支（回归 Phase 0/1 行为）；
- PH3-ACC-003：复杂问题 plan→execute→reflect(补步)→synth 全链路；反思无需补步
  时不扩展；规划失败 C5 优雅终止；llm_calls 与 legacy 同口径。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import langgraph  # noqa: F401

    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False

from agents.agent import Agent
from agents.chunking import Chunk
from agents.config import Config
from agents.llm import ChatResult, LLMClient, LLMError

CHUNKS = [
    Chunk(
        id=0,
        text="RAG 的核心是先检索再生成，叶子块精确召回、父块扩展上下文，RRF 融合向量与 BM25 两路排名。" * 3,
        file="笔记0.md",
        heading="RAG",
        source="笔记0.md",
    ),
]

PLAN_ONE_SEARCH = json.dumps({
    "reasoning": "检索一步",
    "plan_summary": "单步检索",
    "steps": [{"action": "search_knowledge_base", "input": {"query": "RAG"}, "purpose": ""}],
}, ensure_ascii=False)

REFLECT_NEED_MORE = json.dumps({
    "need_more": True,
    "reasoning": "补充一次精确计算",
    "steps": [{"action": "calculator", "input": {"expression": "1+1"}, "purpose": "计算"}],
}, ensure_ascii=False)

REFLECT_NO_MORE = json.dumps({"need_more": False, "reasoning": "现有信息足以回答", "steps": []}, ensure_ascii=False)


class ScriptedLLM(LLMClient):
    """离线脚本 LLM：chat/chat_json 按序返回；fail_first=True 时所有调用都抛 LLMError（模拟 LLM 全面故障）。"""

    def __init__(self, config: Config, texts: list[str], fail_first: bool = False):
        self.config = config
        self._texts = list(texts)
        self._fail_first = fail_first
        self.calls = 0
        self.seen: list[list[dict]] = []

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self._fail_first:
            raise LLMError("LLM 全面故障（测试注入）")
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text, prompt_tokens=10, completion_tokens=5, latency_s=0.0)


def make_config(tmp: str, fast_path: bool = True) -> Config:
    return Config({
        "data_dir": tmp,
        "embedding": {"backend": "tfidf", "hash_dim": 256},
        "retrieval": {"rerank": "off", "fusion_mode": "rrf"},
        "audit": {"enabled": False},
        "engine": "langgraph",
        "planner": {"fast_path": fast_path},
    })


def make_agent(config: Config, llm) -> Agent:
    agent = Agent(config, llm=llm, lazy_index=True)
    agent.build_index(chunks=CHUNKS, persist=False)
    return agent


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖）")
class FastPathRoutingTests(unittest.TestCase):
    """PH3-ACC-002：简单事实题 → 单发分支（检索子图），回归 Phase 0/1。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self._tmp.name, fast_path=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fast_path_routes_to_single_shot(self):
        llm = ScriptedLLM(self.config, ["快路径合成 [1]。"])
        agent = make_agent(self.config, llm)
        answer = agent.ask("什么是 RAG？")
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(len(answer.steps), 1)
        self.assertEqual(answer.steps[0].action, "search_knowledge_base")
        self.assertIn("快路径", answer.plan.reasoning)
        self.assertEqual(answer.llm_calls, 1, "快路径仅合成一次")
        print("✓ PH3-ACC-002 快路径 → 单发分支（plan.reasoning/llm_calls 同 legacy）")


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖）")
class ReActMultiStepTests(unittest.TestCase):
    """PH3-ACC-003：plan → execute → reflect（补步/不补步）→ synth。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self._tmp.name, fast_path=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan_execute_reflect_with_extension(self):
        llm = ScriptedLLM(self.config, [PLAN_ONE_SEARCH, REFLECT_NEED_MORE, "两步后的合成 [1]。"])
        agent = make_agent(self.config, llm)
        answer = agent.ask("什么是 RAG？这是一个需要多步的问题吗请详细展开说明一下谢谢")
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(len(answer.steps), 2, "反思补步应被执行（search + calculator）")
        self.assertEqual([s.action for s in answer.steps], ["search_knowledge_base", "calculator"])
        self.assertEqual(answer.plan.rounds, 2, "反思补步后 Plan.rounds+1（legacy 口径）")
        # llm_calls：规划 1 + 反思 1 + 合成 1（legacy 同口径）
        self.assertEqual(answer.llm_calls, 3)
        self.assertTrue(answer.sources, "来源来自 search 步骤")
        print("✓ PH3-ACC-003a 多步全链路：补步执行 + rounds+1 + 计数同口径")

    def test_reflect_no_extension(self):
        llm = ScriptedLLM(self.config, [PLAN_ONE_SEARCH, REFLECT_NO_MORE, "无需补步的合成 [1]。"])
        agent = make_agent(self.config, llm)
        answer = agent.ask("什么是 RAG？这是一个需要多步的问题吗请详细展开说明一下谢谢")
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(len(answer.steps), 1)
        self.assertEqual(answer.llm_calls, 3, "反思被调用（+1）但无补步")
        print("✓ PH3-ACC-003b 反思无需补步：不扩展、计数仍含反思调用")

    def test_llm_outage_degrades_gracefully(self):
        """LLM 全面故障：planner.plan 内部回退 fallback 计划（legacy 同款，不抛异常），
        反思被 _reflect_and_extend 内部吞掉，最终合成失败 → C5 优雅终止、无半截答案。"""
        llm = ScriptedLLM(self.config, [], fail_first=True)
        agent = make_agent(self.config, llm)
        answer = agent.ask("什么是 RAG？这是一个需要多步的问题吗请详细展开说明一下谢谢")
        self.assertFalse(answer.ok)
        self.assertIn("LLMError", answer.error)
        self.assertEqual(answer.final_answer, "", "C5：失败不得产出半截答案")
        print("✓ PH3-ACC-003c LLM 故障 → fallback 计划 → 合成失败优雅终止（C5，legacy 同款回退）")


if __name__ == "__main__":
    unittest.main(verbosity=1)
