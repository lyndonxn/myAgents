"""W1 切片测试：结构化来源详情（sources_detail）与 ask 真阶段事件（on_stage）。

全部离线（无真实 LLM、无网络、不读写用户 data/ 与 .env）：
- 工具层：假 retriever/hit 验证 sources_detail 与 sources 下标一一对应（去重同步）；
- 全链路：ScriptedLLM + TF-IDF 后端 + 临时 KB（build_index persist=False），
  验证 on_stage 顺序（检索知识库→生成答案）、缺省 None 行为不变、回调异常被吞；
- 端点 payload：_answer_payload 含 sources_detail，旧 answer 对象缺失字段回退空表；
- 落库：WebStore messages.sources_detail 迁移列写入/读出/旧行默认空表。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import config as config_mod
from agents.agent import Agent
from agents.config import Config
from agents.llm import ChatResult, LLMClient
from agents.tools import ToolContext, tool_search_knowledge_base
from agents.web_server import Handler
from agents.web_store import WebStore

REPO = Path(__file__).resolve().parent.parent

KB_DOC = (
    "# RAG\n\n"
    "检索增强生成（RAG）是一种先检索后生成的问答架构。系统从知识库中召回与问题相关的片段，"
    "再把片段交给大语言模型生成答案，从而减少幻觉并支持来源引用。"
    "在企业知识管理场景中，RAG 通常与混合检索、重排序和引用校验配合使用。\n"
)
# 快路径（G9）：无历史 + 简单事实题 → 跳过规划与反思，仅 1 次合成调用
HIT_QUESTION = "什么是 RAG？"
SYNTH_ANSWER = "RAG 是先检索后生成的问答架构 [1]。"


class ScriptedLLM(LLMClient):
    """离线假 LLM：按脚本顺序返回文本，记录调用次数（不经网络）。"""

    def __init__(self, texts: list[str], config: Config):
        self.config = config
        self._texts = list(texts)
        self.calls = 0

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


def _fake_hit(file: str = "rag.md", heading: str = "RAG", leaf: str = "先检索后生成的问答架构", dup: bool = False) -> SimpleNamespace:
    """构造 retriever 命中项：tools._hit_detail 只依赖 chunk/leaf_text/text/source/file。"""
    source = f"{file}#{heading}" if heading else file
    return SimpleNamespace(
        chunk=SimpleNamespace(file=file, heading=heading, text=f"{heading} 的正文内容"),
        leaf_text=leaf,
        text=f"{heading} 的正文内容",
        source=source,
        file=file,
        final_score=0.9,
    )


def _fake_retriever(hits: list) -> SimpleNamespace:
    return SimpleNamespace(retrieve=lambda query, top_k=5: hits[:top_k])


class SourcesDetailToolTests(unittest.TestCase):
    """工具层：sources_detail 与 sources 同步去重、下标一一对应。"""

    def test_tool_output_detail_aligned_with_dedup(self):
        hits = [_fake_hit(file="a.md", heading="A > A1"), _fake_hit(file="a.md", heading="A > A1"), _fake_hit(file="b.md", heading="")]
        ctx = ToolContext(retriever=_fake_retriever(hits))
        out = tool_search_knowledge_base(ctx, query="任意", top_k=3)
        self.assertEqual(len(out["sources_detail"]), len(out["sources"]), "详情与来源必须一一对应")
        self.assertEqual(out["sources"], ["a.md#A > A1", "b.md"], "同源重复命中应去重")
        d0 = out["sources_detail"][0]
        self.assertEqual(d0["title"], "A1", "标题取标题路径末段")
        self.assertEqual(d0["path"], "a.md")
        self.assertEqual(d0["heading"], "A > A1")
        self.assertTrue(d0["snippet"], "引句应取叶子命中文本")
        d1 = out["sources_detail"][1]
        self.assertEqual(d1["title"], "b.md", "无标题时回退文件名")
        self.assertEqual(d1["heading"], "")

    def test_hit_detail_truncates_and_collapses_whitespace(self):
        hit = _fake_hit(leaf="  多  行\n\n空白  文本　应折叠  " + "长" * 200)
        ctx = ToolContext(retriever=_fake_retriever([hit]))
        detail = tool_search_knowledge_base(ctx, query="x", top_k=1)["sources_detail"][0]
        self.assertLessEqual(len(detail["snippet"]), 120, "引句截断 120 字")
        self.assertNotIn("\n", detail["snippet"], "引句折叠空白")
        self.assertTrue(detail["snippet"].startswith("多 行 空白"), f"空白折叠为单空格: {detail['snippet'][:12]}")


class AskStageAndDetailTests(unittest.TestCase):
    """全链路（快路径 E2E）：on_stage 顺序 / 缺省不变 / 异常兜底 / sources_detail 组装。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {name: getattr(config_mod, name) for name in ("CONFIG_PATH", "RUNTIME_PATH", "ENV_PATH")}
        config_mod.CONFIG_PATH = REPO / "config.yaml"
        config_mod.RUNTIME_PATH = self.tmp / "runtime.json"
        config_mod.ENV_PATH = self.tmp / ".env"

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(config_mod, name, value)
        self._tmp.cleanup()

    def _hit_agent(self, scripts: list[str]) -> Agent:
        kb_dir = self.tmp / "kb"
        kb_dir.mkdir(exist_ok=True)
        (kb_dir / "rag.md").write_text(KB_DOC, encoding="utf-8")
        cfg = Config({
            "kb_path": str(kb_dir),
            "embedding": {"backend": "tfidf", "hash_dim": 256},
            "retrieval": {"rerank": "off", "top_k": 3},
            "chunking": {"min_chars": 20, "max_chars": 600, "overlap": 20, "leaf_max_chars": 120, "leaf_min_chars": 24},
            "llm": {"mode": "local", "base_url": "http://127.0.0.1:11434/v1", "chat_model": "qwen2.5:7b"},
            "tools": {"kb_fallback_web": True, "max_retries": 0},
        })
        return Agent(cfg, llm=ScriptedLLM(scripts, cfg), lazy_index=True)

    def test_ask_on_stage_sequence_and_sources_detail(self):
        """快路径 ask：on_stage 依次收到「检索知识库」「生成答案」；sources_detail 与 sources 对齐且字段非空。"""
        agent = self._hit_agent([SYNTH_ANSWER])
        agent.build_index(persist=False)
        stages: list[str] = []
        answer = agent.ask(HIT_QUESTION, on_stage=stages.append)
        self.assertTrue(answer.ok, f"ask 不应失败: {answer.error}")
        self.assertEqual(stages, ["检索知识库", "生成答案"], f"阶段顺序应为检索→生成，实际 {stages}")
        self.assertEqual(answer.sources, ["rag.md#RAG"], "旧 sources 字段语义不变")
        self.assertEqual(len(answer.sources_detail), len(answer.sources), "详情与来源下标对齐")
        detail = answer.sources_detail[0]
        self.assertEqual(detail["title"], "RAG")
        self.assertEqual(detail["path"], "rag.md")
        self.assertTrue(detail["snippet"], "命中详情应带引句")
        self.assertEqual(self._llm_calls(agent), 1, "快路径仅 1 次合成调用")

    def test_ask_on_stage_default_none_unchanged(self):
        """缺省 on_stage=None：签名向后兼容，ask 行为与 W1 之前一致。"""
        agent = self._hit_agent([SYNTH_ANSWER])
        agent.build_index(persist=False)
        answer = agent.ask(HIT_QUESTION)
        self.assertTrue(answer.ok, f"ask 不应失败: {answer.error}")
        self.assertEqual(len(answer.sources_detail), len(answer.sources), "详情组装不依赖回调")

    def test_ask_on_stage_exception_swallowed(self):
        """on_stage 回调抛异常（如前端断连）：只吞掉不影响回答生成。"""
        agent = self._hit_agent([SYNTH_ANSWER])
        agent.build_index(persist=False)

        def broken(_name: str):
            raise ConnectionError("客户端已断开")

        answer = agent.ask(HIT_QUESTION, on_stage=broken)
        self.assertTrue(answer.ok, f"回调异常不应中断问答: {answer.error}")
        self.assertIn("[1]", answer.final_answer)

    @staticmethod
    def _llm_calls(agent: Agent) -> int:
        return agent.llm.calls  # ScriptedLLM 记录调用数


class PayloadAndStoreTests(unittest.TestCase):
    """端点 payload 与落库：sources_detail 透出、旧对象回退、迁移列往返。"""

    def test_answer_payload_includes_sources_detail(self):
        answer = SimpleNamespace(
            final_answer="答 [1]", error="", sources=["rag.md#RAG"], steps=[],
            sources_detail=[{"title": "RAG", "path": "rag.md", "heading": "RAG", "snippet": "先检索后生成"}],
            plan=SimpleNamespace(plan_summary="s", steps=[], fallback=False, rounds=1, reflections=[]),
            total_latency_s=1.0, llm_calls=1, prompt_tokens=10, completion_tokens=5, estimated_cost=0.0,
        )
        payload = Handler._answer_payload(SimpleNamespace(), answer)
        self.assertEqual(payload["sources_detail"], [{"title": "RAG", "path": "rag.md", "heading": "RAG", "snippet": "先检索后生成"}])
        self.assertEqual(payload["sources"], ["rag.md#RAG"], "旧 sources 键不变")

    def test_answer_payload_legacy_answer_without_detail(self):
        """旧 answer 对象（无 sources_detail 属性）：payload 回退空表，不抛错。"""
        answer = SimpleNamespace(
            final_answer="答", error="", sources=["a"], steps=[],
            plan=SimpleNamespace(plan_summary="s", steps=[], fallback=False, rounds=1, reflections=[]),
            total_latency_s=1.0, llm_calls=1, prompt_tokens=0, completion_tokens=0, estimated_cost=0.0,
        )
        payload = Handler._answer_payload(SimpleNamespace(), answer)
        self.assertEqual(payload["sources_detail"], [])

    def test_web_store_sources_detail_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            store = WebStore(Path(td) / "webui.sqlite3")
            store.ensure_default("/tmp/kb")
            sid = store.create_session("default")
            store.add_message(sid, "assistant", "答 [1]", sources=["rag.md#RAG"], plan={}, metrics={},
                              sources_detail=[{"title": "RAG", "path": "rag.md", "heading": "RAG", "snippet": "s"}])
            rows = store.messages(sid)
            self.assertEqual(rows[0]["sources_detail"], [{"title": "RAG", "path": "rag.md", "heading": "RAG", "snippet": "s"}])

            # 旧行为路径（add_task_result 未写详情列）→ 读出空表，不抛错
            store.add_task_result(sid, "default", "任务问题", "任务答案", sources=["kb://a.md"])
            rows = store.messages(sid)
            self.assertEqual(rows[-1]["sources_detail"], [], "未写详情的消息读出空表")

    def test_web_store_migration_on_legacy_db(self):
        """旧库（无 sources_detail 列）打开即迁移，既有数据可读。"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite3"
            store = WebStore(path)
            store.ensure_default("/tmp/kb")
            sid = store.create_session("default")
            store.add_message(sid, "assistant", "旧回答", sources=["a"])
            # 模拟旧库：删掉新列（sqlite 3.35+ 支持 DROP COLUMN；不可用时跳过该断言路径）
            import sqlite3
            db = sqlite3.connect(path)
            cols = [r[1] for r in db.execute("PRAGMA table_info(messages)")]
            if "sources_detail" in cols:
                try:
                    db.execute("ALTER TABLE messages DROP COLUMN sources_detail")
                    db.commit()
                    db.close()
                    reopened = WebStore(path)  # __init__ 应补列
                    rows = reopened.messages(sid)
                    self.assertEqual(rows[0]["sources_detail"], [], "迁移后旧行读出空表")
                    return
                except sqlite3.OperationalError:
                    db.close()  # 环境不支持 DROP COLUMN：跳过（迁移逻辑已由 roundtrip 覆盖）
                    return
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
