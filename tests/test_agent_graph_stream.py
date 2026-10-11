"""LangGraph 流式与节点指标测试（spec/08 T-008，LG-ACC-007/009）。

全部离线（AGENTS.md 硬约束 1）：MultiFakeLLM 内存假 LLM（chat 与 chat_stream
双通道按脚本返回），不联网、不调用真实 LLM。
覆盖：
- LG-2.1：stream_graph_ask 产出真 token delta 事件（非 legacy 整段切块），
  终止事件 _result 携带完整 Answer；invoke 路径（run_graph_ask）不触达流式实现；
- LG-2.2：Answer.node_metrics 按节点记录（duration_ms 可归因）；
- 流式 × 会话：session_id 下流式与 Checkpointer 共存，追问改写照常发生。
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

CHUNKS = [
    Chunk(
        id=0,
        text="RAG（Retrieval-Augmented Generation）的核心是先检索再生成。索引阶段把文档切成父子分块，"
        "叶子块用于精确召回，父块用于扩展上下文。检索阶段混合向量召回与 BM25 关键词召回，用 RRF 融合两路排名。",
        file="笔记0.md",
        heading="RAG",
        source="笔记0.md",
    ),
    Chunk(
        id=1,
        text="MCP（Model Context Protocol）是模型与工具之间的标准协议。工具通过 JSON Schema 描述参数，"
        "执行器在调用前做参数校验。失败步骤按配置重试，重试耗尽后标记 error 并继续后续步骤。",
        file="笔记1.md",
        heading="MCP",
        source="笔记1.md",
    ),
]


class MultiFakeLLM(LLMClient):
    """离线假 LLM：chat（改写/规划）与 chat_stream（合成）双通道，记录全部调用。

    _stream_tokens 模拟真实 SSE 的多个增量 chunk。
    """

    def __init__(self, config: Config, chat_texts: list[str] | None = None,
                 stream_text: str = "", stream_tokens: list[str] | None = None):
        self.config = config
        self._chat_texts = list(chat_texts or [])
        self._stream_text = stream_text
        self._stream_tokens = stream_tokens or []
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.stream_calls = 0

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        text = self._chat_texts[self.calls - 1] if self.calls <= len(self._chat_texts) else "兜底回复"
        return ChatResult(text=text, prompt_tokens=10, completion_tokens=5, latency_s=0.0)

    def chat_stream(self, messages: list[dict], on_token=None, **kwargs) -> ChatResult:
        self.calls += 1
        self.stream_calls += 1
        self.seen.append([dict(m) for m in messages])
        for tok in self._stream_tokens:
            if on_token is not None:
                on_token(tok)
        return ChatResult(text=self._stream_text, prompt_tokens=10, completion_tokens=5, latency_s=0.0)


class StreamGuardLLM(MultiFakeLLM):
    """流式守卫：chat_stream 被触达即测试失败（验证 invoke 路径零流式）。"""

    def chat_stream(self, messages: list[dict], on_token=None, **kwargs) -> ChatResult:
        raise AssertionError("invoke 路径不应触达流式实现（LG-2.1 闸门失效）")


def make_config(tmp_dir: str) -> Config:
    return Config({
        "data_dir": tmp_dir,
        "embedding": {"backend": "tfidf", "hash_dim": 256},
        "retrieval": {"rerank": "off", "fusion_mode": "rrf"},
        "audit": {"enabled": False},
        "engine": "langgraph",
    })


def make_agent(config: Config, llm) -> Agent:
    agent = Agent(config, llm=llm, lazy_index=True)
    agent.build_index(chunks=CHUNKS, persist=False)
    return agent


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖）")
class StreamAskTests(unittest.TestCase):
    """LG-2.1：真 token 流 + invoke 零流式闸门。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stream_emits_true_token_deltas(self):
        from agents.agent_graph import stream_graph_ask

        tokens = ["RAG ", "是检索", "增强生成。"]
        llm = MultiFakeLLM(self.config, stream_text="RAG 是检索增强生成。", stream_tokens=tokens)
        agent = make_agent(self.config, llm)

        events = list(stream_graph_ask(agent, question="什么是 RAG？"))
        deltas = [ev["text"] for ev in events if ev.get("event") == "delta"]
        self.assertEqual(deltas, tokens, "delta 应为逐 token 的真流式事件（非整段切块）")
        self.assertEqual([ev["event"] for ev in events][-1], "_result")
        answer = events[-1]["answer"]
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(answer.final_answer, "RAG 是检索增强生成。")
        self.assertEqual(answer.citations_invalid, 0)
        self.assertEqual(llm.stream_calls, 1, "合成应走流式通道")
        print("✓ LG-2.1 流式：逐 token delta 事件 + _result 终止事件")

    def test_node_metrics_attributable(self):
        from agents.agent_graph import stream_graph_ask

        llm = MultiFakeLLM(self.config, stream_text="答案 [1]。", stream_tokens=["答案 [1]。"])
        agent = make_agent(self.config, llm)
        events = list(stream_graph_ask(agent, question="什么是 RAG？"))
        answer = events[-1]["answer"]
        names = [m["node"] for m in answer.node_metrics]
        for expected in ("route_entry", "vector_recall", "bm25_recall", "fuse", "expand_parents", "rerank",
                         "generate", "validate_citations", "commit_turn"):
            self.assertIn(expected, names)
        for m in answer.node_metrics:
            self.assertIsInstance(m["duration_ms"], float)
        print("✓ LG-2.2 节点指标随 Answer 暴露（可按节点归因）")

    def test_invoke_path_never_streams(self):
        from agents.agent_graph import run_graph_ask

        llm = StreamGuardLLM(self.config, stream_text="不应出现", stream_tokens=["x"])
        agent = make_agent(self.config, llm)
        answer = run_graph_ask(agent, question="什么是 RAG？")
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(llm.stream_calls, 0)
        print("✓ invoke 路径零流式（闸门生效，legacy 行为不变）")

    def test_stream_with_session_and_rewrite(self):
        from agents.agent_graph import stream_graph_ask

        db = str(Path(self._tmp.name) / "checkpoints.sqlite")
        self.config._raw["graph"] = {"checkpoint_db": db, "clarify_on_miss": False}
        llm = MultiFakeLLM(
            self.config,
            chat_texts=["改写后的独立查询"],
            stream_text="父块是检索命中的完整章节 [1]。",
            stream_tokens=["父块是", "检索命中的", "完整章节 [1]。"],
        )
        agent = make_agent(self.config, llm)
        first = list(stream_graph_ask(agent, question="什么是 RAG？", session_id="s9"))
        self.assertTrue(first[-1]["answer"].ok)
        second = list(stream_graph_ask(agent, question="那父块呢？", session_id="s9"))
        answer2 = second[-1]["answer"]
        self.assertTrue(answer2.ok, answer2.error)
        self.assertEqual(answer2.llm_calls, 2, "改写 1 次 + 流式合成 1 次")
        self.assertIn("什么是 RAG？", llm.seen[1][1]["content"], "改写提示词应含第一轮历史")
        self.assertTrue([ev for ev in second if ev.get("event") == "delta"], "第二轮仍为真流式")
        print("✓ 流式 × Checkpointer 会话：追问改写 + 逐 token 流式共存")


if __name__ == "__main__":
    unittest.main(verbosity=1)
