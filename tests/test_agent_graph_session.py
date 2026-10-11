"""LangGraph 会话与恢复测试（spec/08 T-007，LG-ACC-004/005/006）。

全部离线（AGENTS.md 硬约束 1）：ScriptedLLM/ExitLLM 均为内存假 LLM，不联网。
覆盖：
- LG-ACC-004：同 checkpoint 库跨实例续接会话——新实例追问时历史经 checkpoint 可见，
  追问改写复用 planner.rewrite_query；不双写 legacy SessionMemory；
- LG-ACC-005：检索零命中 interrupt 澄清 → Answer.pending_clarification；同会话下一问
  经 Command(resume) 注回图继续；跳过澄清按 miss_hint 口径合成；澄清轮数上限 1；
- LG-ACC-006：子进程在合成调用点 os._exit(9) 模拟崩溃 → 父进程 invoke(None) 续跑，
  合成恰好调用一次、已完成检索节点不重跑；
- 无 session_id 的调用保持无状态（不落 checkpoint、不泄漏历史）。
"""
from __future__ import annotations

import subprocess
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
from agents.bm25 import BM25Index
from agents.chunking import Chunk
from agents.config import Config
from agents.llm import ChatResult, LLMClient
from agents.vector_store import VectorStore

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


class ScriptedLLM(LLMClient):
    """离线脚本 LLM：按调用次序返回固定文本，并记录完整 messages 供断言。"""

    def __init__(self, config: Config, texts: list[str]):
        self.config = config
        self._texts = list(texts)
        self.calls = 0
        self.seen: list[list[dict]] = []

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text, prompt_tokens=10, completion_tokens=5, latency_s=0.0)


def make_config(tmp_dir: str, checkpoint_db: str = "", clarify: bool = False) -> Config:
    raw = {
        "data_dir": tmp_dir,
        "embedding": {"backend": "tfidf", "hash_dim": 256},
        "retrieval": {"rerank": "off", "fusion_mode": "rrf"},
        "audit": {"enabled": False},
        "engine": "langgraph",
        "graph": {"checkpoint_db": checkpoint_db, "clarify_on_miss": clarify},
    }
    return Config(raw)


def make_agent(config: Config, llm) -> Agent:
    agent = Agent(config, llm=llm, lazy_index=True)
    agent.build_index(chunks=CHUNKS, persist=False)
    return agent


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖）")
class SessionContinuityTests(unittest.TestCase):
    """LG-ACC-004：跨实例会话续接 + 不双写 legacy SessionMemory。"""

    def test_cross_instance_continuity_and_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "checkpoints.sqlite")
            # 第一轮：实例 A 建立会话历史
            agent_a = make_agent(make_config(tmp, db), ScriptedLLM(make_config(tmp, db), ["RAG 是检索增强生成 [1]。"]))
            answer1 = agent_a.ask("什么是 RAG？", session_id="s1")
            self.assertTrue(answer1.ok, answer1.error)
            self.assertEqual(answer1.llm_calls, 1)

            # 第二轮：全新实例 B（同 checkpoint 库）继续同一会话
            agent_b = make_agent(
                make_config(tmp, db),
                ScriptedLLM(make_config(tmp, db), ["父块", "父块是检索命中的完整章节上下文 [1]。"]),
            )
            answer2 = agent_b.ask("那父块呢？", session_id="s1")
            self.assertTrue(answer2.ok, answer2.error)
            # 改写 1 次 + 合成 1 次
            self.assertEqual(answer2.llm_calls, 2)
            # 追问改写提示词里包含第一轮历史（"什么是 RAG"）
            rewrite_msgs = agent_b.llm.seen[0]
            self.assertIn("查询改写器", rewrite_msgs[0]["content"])
            self.assertIn("什么是 RAG", rewrite_msgs[1]["content"])
            self.assertIn("父块是检索命中的完整章节上下文", answer2.final_answer)
            # 不双写：图引擎不写 legacy SessionMemory
            self.assertTrue(agent_b.memory.is_empty)
            print("✓ LG-ACC-004 跨实例会话续接 + 追问改写 + 不双写")

    def test_sessionless_calls_stay_stateless(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            agent = make_agent(config, ScriptedLLM(config, ["答一 [1]。", "答二 [1]。"]))
            a1 = agent.ask("第一问", session_id="")
            a2 = agent.ask("第二问", session_id="")
            self.assertTrue(a1.ok and a2.ok)
            # 无 session → 两问各自无历史：都只有合成 1 次调用（共 2 次）
            self.assertEqual(agent.llm.calls, 2)
            print("✓ 无 session_id 调用保持无状态（不落 checkpoint）")


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖）")
class ClarifyInterruptTests(unittest.TestCase):
    """LG-ACC-005：interrupt 澄清 → 新问题当澄清恢复 / 跳过按 miss_hint 合成。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "checkpoints.sqlite")
        config = make_config(self._tmp.name, self.db, clarify=True)
        self.agent = make_agent(config, ScriptedLLM(config, ["澄清后回答：知识库未命中，请补充背景。"]))

    def tearDown(self):
        self._tmp.cleanup()

    def _empty_index(self):
        """制造真·零命中：清空检索器持有的向量库与 BM25（与 legacy _kb_miss 同口径）。"""
        self.agent.retriever.store = VectorStore()
        self.agent.retriever.bm25 = BM25Index().build([])

    def test_miss_pauses_with_pending_clarification(self):
        self._empty_index()
        answer = self.agent.ask("完全不相关的问题", session_id="s2")
        self.assertTrue(answer.ok, answer.error)
        self.assertEqual(answer.final_answer, "", "挂起澄清时不得产出半截答案")
        self.assertTrue(answer.pending_clarification, "应携带 interrupt 载荷")
        self.assertEqual(answer.pending_clarification.get("type"), "clarify")
        self.assertEqual(answer.llm_calls, 0, "挂起点在合成之前，无 LLM 调用")
        print("✓ 零命中 → interrupt 挂起，pending_clarification 暴露")

    def test_next_question_resumes_as_clarification(self):
        self._empty_index()
        first = self.agent.ask("完全不相关的问题", session_id="s2")
        self.assertTrue(first.pending_clarification)
        # 同会话下一问 → 被当作澄清内容注回图，澄清一轮仍未命中 → miss_hint 口径合成
        second = self.agent.ask("其实我想问 RAG", session_id="s2")
        self.assertTrue(second.ok, second.error)
        self.assertEqual(second.final_answer, "澄清后回答：知识库未命中，请补充背景。")
        self.assertEqual(second.pending_clarification, {})
        self.assertEqual(second.llm_calls, 1)
        # 合成以澄清后的意图为准（验收修复项）：提示词中的用户问题应为澄清文本
        synth_msgs = self.agent.llm.seen[-1]
        self.assertIn("其实我想问 RAG", synth_msgs[1]["content"])
        print("✓ 下一问经 Command(resume) 恢复 → 澄清接管问题语义 → 合成完成")

    def test_skip_clarify_answers_with_miss_hint(self):
        from agents.agent_graph import build_answer_graph
        from agents.agent_graph.runner import _get_checkpointer
        from langgraph.types import Command

        self._empty_index()
        graph = build_answer_graph(self.agent, checkpointer=_get_checkpointer(self.agent))
        config = {"configurable": {"thread_id": "s3"}}
        first = graph.invoke({"question": "不相关问题", "node_metrics": []}, config)
        self.assertTrue(first.get("__interrupt__"))
        # 用户跳过（空恢复值）→ 直接按未命中口径合成，不循环
        final = graph.invoke(Command(resume=""), config)
        self.assertTrue(final.get("draft"))
        self.assertFalse(final.get("pending_requery"))
        print("✓ 跳过澄清 → miss_hint 口径合成（无循环）")


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph 未安装（可选依赖）")
class CrashRecoveryTests(unittest.TestCase):
    """LG-ACC-006：子进程崩溃（os._exit(9)）→ 父进程 invoke(None) 续跑，已完成节点不重跑。"""

    def test_crash_then_resume_without_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "checkpoints.sqlite")
            root = Path(__file__).resolve().parent.parent
            proc = subprocess.run(
                [str(root / ".venv" / "bin" / "python"), str(root / "tests" / "_lg_crash_child.py"), db],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 9, f"子进程应在合成点退出 9：{proc.stderr[-500:]}")

            # 全新实例（同 checkpoint 库）续跑
            config = make_config(tmp, db)
            agent = make_agent(config, ScriptedLLM(config, ["恢复后的合成答案 [1]。"]))
            search_calls: list[int] = []
            original_search = agent.retriever.store.search

            def counting_search(*args, **kwargs):
                search_calls.append(1)
                return original_search(*args, **kwargs)

            agent.retriever.store.search = counting_search  # type: ignore[method-assign]
            answer = agent.ask("", resume=True, session_id="crash")
            self.assertTrue(answer.ok, answer.error)
            self.assertIn("恢复后的合成答案", answer.final_answer)
            # 合成恰好在父进程执行一次；检索（崩溃前已完成）不重跑
            self.assertEqual(agent.llm.calls, 1)
            self.assertEqual(search_calls, [])
            print("✓ LG-ACC-006 崩溃续跑：合成一次、检索零重跑")

    def test_resume_unknown_thread_fails_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp, str(Path(tmp) / "cp.sqlite"))
            agent = make_agent(config, ScriptedLLM(config, ["x"]))
            answer = agent.ask("", resume=True, session_id="no-such-thread")
            self.assertFalse(answer.ok)
            self.assertIn("未找到可恢复的会话线程", answer.error)
            print("✓ resume 无线程 → 中文可读错误（不崩溃）")


if __name__ == "__main__":
    unittest.main(verbosity=1)
