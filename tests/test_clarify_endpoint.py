"""澄清 Web 端点测试（spec/09 T-010，PH3-ACC-001）。

全部离线。覆盖：
- _answer_payload 透出 pending_clarification（有挂起/无挂起两态）；
- get_pending_clarification 守卫：无 session 返回空；挂起会话返回 interrupt 载荷；
- 澄清恢复→答案→挂起清除的完整闭环（复用 Phase 1 的图级语义，经 /api/clarify
  同一条 ask 路径）。
"""
from __future__ import annotations

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
from agents.llm import ChatResult, LLMClient

if HAS_LANGGRAPH:
    from agents.agent_graph.runner import get_pending_clarification

from agents.web_server import Handler

CHUNKS = [
    Chunk(
        id=0,
        text="RAG 的核心是先检索再生成，叶子块精确召回、父块扩展上下文，RRF 融合两路排名。" * 3,
        file="笔记0.md",
        heading="RAG",
        source="笔记0.md",
    ),
]


class ScriptedLLM(LLMClient):
    def __init__(self, config: Config, texts: list[str]):
        self.config = config
        self._texts = list(texts)
        self.calls = 0

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text, prompt_tokens=10, completion_tokens=5, latency_s=0.0)


def make_config(tmp: str, clarify: bool = True) -> Config:
    return Config({
        "data_dir": tmp,
        "embedding": {"backend": "tfidf", "hash_dim": 256},
        "retrieval": {"rerank": "off", "fusion_mode": "rrf"},
        "audit": {"enabled": False},
        "engine": "langgraph",
        "graph": {"checkpoint_db": str(Path(tmp) / "checkpoints.sqlite"), "clarify_on_miss": clarify},
    })


class PayloadSurfacingTests(unittest.TestCase):
    """PH3-ACC-001（payload 面）：pending_clarification 两态透出。"""

    def test_payload_carries_pending_clarification(self):
        handler = object.__new__(Handler)
        answer = Agent.__new__(Agent)  # 仅属性载体
        from agents.agent import Answer

        ans = Answer(final_answer="", pending_clarification={"type": "clarify", "question": "q", "message": "m"})
        payload = handler._answer_payload(ans)
        self.assertEqual(payload["pending_clarification"]["type"], "clarify")
        legacy = Answer(final_answer="正常答案")
        self.assertEqual(handler._answer_payload(legacy)["pending_clarification"], {})
        print("✓ payload 两态透出 pending_clarification（legacy 恒空 dict）")


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖）")
class ClarifyGuardTests(unittest.TestCase):
    """PH3-ACC-001（守卫面）：get_pending_clarification + 恢复闭环。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        config = make_config(self._tmp.name)
        self.agent = Agent(config, llm=ScriptedLLM(config, ["澄清后的回答 [1]。"]), lazy_index=True)
        self.agent.build_index(chunks=CHUNKS, persist=False)
        # 空 index 制造零命中 → 触发挂起
        from agents.bm25 import BM25Index
        from agents.vector_store import VectorStore

        self.agent.retriever.store = VectorStore()
        self.agent.retriever.bm25 = BM25Index().build([])

    def tearDown(self):
        self._tmp.cleanup()

    def test_guard_and_resume_loop(self):
        # 无会话 → 空
        self.assertEqual(get_pending_clarification(self.agent, ""), {})
        # 首问挂起
        first = self.agent.ask("完全不相关", session_id="s1")
        self.assertTrue(first.pending_clarification)
        # 守卫此时应报告挂起
        pending = get_pending_clarification(self.agent, "s1")
        self.assertEqual(pending.get("type"), "clarify")
        # /api/clarify 走的就是 ask(clarification, session_id) 同一条路径 → 恢复闭环
        second = self.agent.ask("其实我想问 RAG", session_id="s1")
        self.assertTrue(second.ok, second.error)
        self.assertEqual(second.pending_clarification, {})
        self.assertEqual(get_pending_clarification(self.agent, "s1"), {}, "恢复后挂起应清除")
        print("✓ PH3-ACC-001 守卫与恢复闭环（挂起→澄清→清除）")


if __name__ == "__main__":
    unittest.main(verbosity=1)
