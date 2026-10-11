"""LangGraph 编排引擎测试（spec/08 T-006，LG-ACC-001/002/003 + C5 错误路由）。

全部离线（AGENTS.md 硬约束 1）：FakeLLM 固定合成文本，不联网、不调用真实 LLM。
覆盖：
- LG-ACC-001：engine=langgraph 时 Agent.ask 端到端产出答案 + 引用（fake LLM 口径）；
- LG-ACC-002：默认 legacy 行为不变（引擎分派不劫持原链路）+ engine 配置校验；
- LG-ACC-003：检索子图与 legacy Retriever.retrieve 逐字段一致（冻结引擎 C6）；
- C5：合成失败 → error 显式路由 error_node，优雅终止、无半截产出；
- LG-0.2：node_metrics 按节点记录（并行召回分支归并）。
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
from agents.config import Config, normalize_config, validate_config
from agents.llm import ChatResult, LLMError, LLMClient

_CHUNK_TEXTS = [
    (
        "RAG（Retrieval-Augmented Generation）的核心是先检索再生成。"
        "索引阶段把文档切成父子分块，叶子块用于精确召回，父块用于扩展上下文。"
        "检索阶段混合向量召回与 BM25 关键词召回，用 RRF 融合两路排名。"
        "生成阶段把命中的父块作为证据上下文，要求模型标注 [n] 引用。"
    ),
    (
        "MCP（Model Context Protocol）是模型与工具之间的标准协议。"
        "工具通过 JSON Schema 描述参数，执行器在调用前做参数校验。"
        "失败步骤按配置重试，重试耗尽后标记 error 并继续后续步骤。"
        "审计轨迹按天落盘，只追加不修改，便于回溯每次工具调用。"
    ),
]

CHUNKS = [
    Chunk(id=i, text=t, file=f"笔记{i}.md", heading=f"主题{i}", source=f"笔记{i}.md")
    for i, t in enumerate(_CHUNK_TEXTS)
]


class FakeLLM(LLMClient):
    """离线假 LLM：固定合成文本并记录调用次数（不联网，AGENTS.md 硬约束 1）。"""

    def __init__(self, config: Config, text: str = "这是合成答案，依据见 [1]。"):
        self.config = config
        self.text = text
        self.calls = 0

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        return ChatResult(text=self.text, prompt_tokens=10, completion_tokens=5, latency_s=0.0)


class ExplodingLLM(FakeLLM):
    """合成阶段必失败的假 LLM：验证 C5 显式错误路由。"""

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        raise LLMError("合成失败（测试注入）")


def make_config(tmp_dir: str, **overrides) -> Config:
    raw = {
        "data_dir": tmp_dir,
        "embedding": {"backend": "tfidf", "hash_dim": 256},
        "retrieval": {"rerank": "off", "fusion_mode": "rrf"},
        "audit": {"enabled": False},
    }
    raw.update(overrides)
    return Config(raw)


def make_agent(config: Config, llm) -> Agent:
    agent = Agent(config, llm=llm, lazy_index=True)
    agent.build_index(chunks=CHUNKS, persist=False)
    return agent


class EngineConfigTests(unittest.TestCase):
    """engine 配置项：默认值、大小写归一、枚举校验（LG-ACC-002 的配置面）。"""

    def test_default_is_legacy(self):
        self.assertEqual(Config({}).engine, "legacy")
        print("✓ engine 默认 legacy")

    def test_case_insensitive(self):
        self.assertEqual(Config({"engine": "LangGraph"}).engine, "langgraph")
        print("✓ engine 大小写不敏感")

    def test_enum_validation(self):
        errors = validate_config({"engine": "bogus"})
        self.assertTrue(any("engine" in e for e in errors))
        self.assertEqual(validate_config({"engine": "langgraph"}), [])
        print("✓ engine 枚举校验")

    def test_normalize(self):
        raw = normalize_config({"engine": "LangGraph"})
        self.assertEqual(raw["engine"], "langgraph")
        print("✓ engine 归一化")


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖，见 requirements-langgraph.txt）")
class RetrievalSubgraphParityTests(unittest.TestCase):
    """LG-ACC-003：检索子图与 legacy 检索逐字段一致（冻结引擎 C6）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agent = make_agent(make_config(self._tmp.name), FakeLLM(make_config(self._tmp.name)))

    def tearDown(self):
        self._tmp.cleanup()

    def test_parity_with_legacy_retriever(self):
        from agents.agent_graph import build_retrieval_subgraph

        query = "RAG 检索增强生成"
        legacy = self.agent.retriever.retrieve(query)
        subgraph = build_retrieval_subgraph(self.agent.retriever)
        final = subgraph.invoke({"question": query})
        graph_chunks = final["chunks"]

        self.assertEqual(len(legacy), len(graph_chunks))
        self.assertEqual([c.file for c in legacy], [c.file for c in graph_chunks])
        self.assertEqual([c.leaf_text for c in legacy], [c.leaf_text for c in graph_chunks])
        self.assertEqual(
            [round(c.final_score, 6) for c in legacy],
            [round(c.final_score, 6) for c in graph_chunks],
        )
        print("✓ 检索子图与 legacy 检索一致（文件/排序/分数/叶子）")


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖，见 requirements-langgraph.txt）")
class GraphEngineTests(unittest.TestCase):
    """LG-ACC-001 端到端（fake LLM）+ C5 错误路由 + 节点指标。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self._tmp.name, engine="langgraph")

    def tearDown(self):
        self._tmp.cleanup()

    def test_end_to_end_with_fake_llm(self):
        agent = make_agent(self.config, FakeLLM(self.config, "LangGraph 引擎合成答案，依据 [1]。"))
        answer = agent.ask("什么是 RAG？")
        self.assertTrue(answer.ok, answer.error)
        self.assertIn("LangGraph 引擎合成答案", answer.final_answer)
        self.assertEqual(answer.citations_valid, 1)
        self.assertEqual(answer.citations_invalid, 0)
        self.assertTrue(answer.sources, "应收集到检索来源")
        self.assertEqual(len(answer.steps), 1)
        self.assertEqual(answer.steps[0].action, "search_knowledge_base")
        self.assertEqual(answer.llm_calls, 1, "图路径仅合成一次 LLM 调用（与 legacy 计数语义对齐）")
        print("✓ langgraph 引擎端到端（fake LLM）：答案+引用+来源+步骤")

    def test_synthesis_failure_routes_to_error_node(self):
        agent = make_agent(self.config, ExplodingLLM(self.config))
        answer = agent.ask("什么是 RAG？")
        self.assertFalse(answer.ok)
        self.assertIn("generate", answer.error)
        self.assertIn("LLMError", answer.error)
        self.assertEqual(answer.final_answer, "", "C5：失败不得产出半截答案")
        print("✓ 合成失败 → error_node 优雅终止（C5）")

    def test_node_metrics_recorded(self):
        from agents.agent_graph import run_graph_ask

        agent = make_agent(self.config, FakeLLM(self.config))
        answer = run_graph_ask(agent, question="什么是 RAG？")
        self.assertTrue(answer.ok, answer.error)
        names = [m["node"] for m in answer.node_metrics]
        for expected in ("route_entry", "vector_recall", "bm25_recall", "fuse", "expand_parents", "rerank", "generate", "validate_citations"):
            self.assertIn(expected, names)
        self.assertEqual(len(names), len(set(names)), "T-011 验收修正：metrics 持有者不应产生子图回写双条目")
        print("✓ 节点指标按节点记录（无重复条目，含并行召回分支）")

    def test_empty_question_routes_to_error(self):
        from agents.agent_graph import build_answer_graph

        agent = make_agent(self.config, FakeLLM(self.config))
        graph, _run_ctx = build_answer_graph(agent)
        final = graph.invoke({"question": "  "})
        self.assertTrue(final.get("error"))
        self.assertEqual(final.get("draft", ""), "")
        print("✓ 空问题 → error_node（不进检索）")


class LegacyUnchangedTests(unittest.TestCase):
    """LG-ACC-002：默认 legacy 行为不变（引擎分派不劫持原链路）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_legacy_ask_still_works(self):
        config = make_config(self._tmp.name)
        llm = FakeLLM(config, "legacy 链路合成答案 [1]。")
        agent = make_agent(config, llm)
        answer = agent.ask("什么是 RAG？")
        self.assertTrue(answer.ok, answer.error)
        self.assertIn("legacy 链路合成答案", answer.final_answer)
        # G9 快路径：简单事实题跳过规划，LLM 只被合成调用 1 次
        self.assertEqual(llm.calls, 1)
        print("✓ 默认 legacy 行为不变（快路径 1 次 LLM 调用）")


if __name__ == "__main__":
    unittest.main(verbosity=1)
