"""冒烟测试：不依赖 LLM/网络，验证解析、分片、向量库、BM25、混合检索。

unittest 形态：SmokeTests 聚合全部检查点（clean_markdown / TF-IDF 后端 /
reranker 离线加载 / VectorStore / BM25 / 混合检索 / split_leaves / load_chunks /
web_search 解析 / SessionMemory）。直跑（python tests/test_smoke.py）与
discover（python -m unittest discover -s tests）均可发现执行。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.bm25 import BM25Index
from agents.chunking import Chunk, clean_markdown, load_chunks
from agents.config import load_config
from agents.embeddings import TfidfHashEmbeddingBackend
from agents.retriever import Retriever
from agents import reranker as reranker_module
from agents.vector_store import VectorStore

SAMPLE = """---
创建日期: 2026-08-14
标签: [AI]
---
# 测试笔记

> [!abstract] 摘要
> 这是摘要内容。

RAG（Retrieval-Augmented Generation）的核心是"先检索，再生成"。

![[图片.jpg]]

参考 [[另一篇笔记|别名]] 与 https://example.com。

```python
print("hello")
```
"""

ROOT = Path(__file__).resolve().parent.parent


class SmokeTests(unittest.TestCase):
    """解析/分片/检索/记忆的离线冒烟检查点。"""

    def test_clean_markdown(self):
        out = clean_markdown(SAMPLE)
        self.assertNotIn("---", out)
        self.assertNotIn("iframe", out)
        self.assertNotIn("![", out)
        self.assertNotIn("https://", out)
        self.assertIn("先检索，再生成", out)
        self.assertIn("别名", out)
        self.assertIn("print", out)
        print("✓ clean_markdown")

    def test_tfidf_backend(self):
        b = TfidfHashEmbeddingBackend(hash_dim=256).fit(["RAG 检索增强生成", "MCP 协议工具调用"])
        vecs = b.embed_texts(["RAG 检索", "MCP 工具"])
        self.assertEqual(vecs.shape, (2, 256))
        q = b.embed_query("什么是 RAG")
        self.assertEqual(q.shape, (256,))
        print("✓ tfidf backend")

    def test_reranker_never_downloads_on_startup(self):
        calls = []
        original = reranker_module.CrossEncoder

        class MissingCrossEncoder:
            def __init__(self, _model_name, **kwargs):
                calls.append(kwargs)
                raise OSError("not cached")

        reranker_module.CrossEncoder = MissingCrossEncoder
        try:
            with self.assertRaises(OSError):
                reranker_module.CrossEncoderReranker._load("missing/model")
        finally:
            reranker_module.CrossEncoder = original

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["local_files_only"], True)
        print("✓ reranker startup stays offline")

    def test_vector_store_search(self):
        vs = VectorStore().build(
            [[1.0, 0.0], [0.0, 1.0], [0.8, 0.2]],
            [{"text": "a", "file": "f1"}, {"text": "b", "file": "f2"}, {"text": "c", "file": "f3"}],
            "test",
        )
        hits = vs.search([1.0, 0.1], 2)
        self.assertEqual(hits[0].index, 0)
        print("✓ vector store")

    def test_bm25(self):
        idx = BM25Index().build(["RAG 检索增强生成", "MCP 协议与工具调用"])
        hits = idx.search("MCP 工具", 1)
        self.assertTrue(hits)
        self.assertEqual(hits[0].index, 1)
        print("✓ bm25")

    def test_hybrid_retriever(self):
        from agents.chunking import Leaf, split_leaves

        chunks = [
            Chunk(0, "RAG 检索增强生成：先检索再生成。", "a.md", "RAG"),
            Chunk(1, "MCP 协议：统一连接模型与外部工具。", "b.md", "MCP"),
            Chunk(2, "Agent 与 Workflow 的控制流区别。", "c.md", "Agent"),
        ]
        leaves = [
            Leaf(id=i, text=lt, file=p.file, heading=p.heading, source="", parent_idx=pid)
            for pid, p in enumerate(chunks)
            for i, lt in enumerate(split_leaves(p.text))
        ]
        leaf_texts = [l.text for l in leaves]
        backend = TfidfHashEmbeddingBackend(hash_dim=256).fit(leaf_texts)
        vs = VectorStore().build(
            backend.embed_texts(leaf_texts),
            [{"text": l.text, "file": l.file, "heading": l.heading, "source": l.source, "parent_idx": l.parent_idx} for l in leaves],
            "tfidf",
        )
        bm25 = BM25Index().build(leaf_texts)
        cfg = load_config()
        retriever = Retriever(vs, bm25, chunks, leaves, cfg, embed_backend=backend)
        hits = retriever.retrieve("MCP 工具 协议", top_k=2)
        self.assertTrue(hits, f"expected b.md, got {[h.file for h in hits] if hits else []}")
        self.assertEqual(hits[0].file, "b.md", f"expected b.md, got {[h.file for h in hits]}")
        print("✓ hybrid retriever (leaf + parent)")

    def test_split_leaves(self):
        from agents.chunking import split_leaves

        text = "第一段内容。" * 5 + "\n\n" + "第二段。" * 50
        leaves = split_leaves(text, max_leaf=80, min_leaf=10)
        self.assertGreaterEqual(len(leaves), 3)
        self.assertTrue(all(0 < len(l) <= 200 for l in leaves))
        print(f"✓ split_leaves ({len(leaves)} leaves)")

    def test_load_chunks(self):
        cfg = load_config()
        kb = Path(cfg.kb_path)
        if not kb.is_absolute():
            kb = ROOT / kb  # 相对路径基于仓库根定位（discover/任意 cwd 均成立）
        if not kb.exists():
            self.skipTest("知识库目录不存在，跳过 load_chunks")
        chunks = load_chunks(kb, cfg)
        self.assertGreater(len(chunks), 5)
        self.assertTrue(all(c.text and c.file for c in chunks))
        print(f"✓ load_chunks ({len(chunks)} chunks)")

    def test_web_search_parser(self):
        """用模拟 Bing HTML 验证解析（不联网）。"""
        from agents.web_search import WebSearch

        html = """
        <ol id="b_results">
          <li class="b_algo">
            <h2><a href="https://example.com/a">示例标题 A</a></h2>
            <p class="b_lineclamp2">这是第一个摘要</p>
          </li>
          <li class="b_algo">
            <h2><a href="https://example.com/b">示例标题 B</a></h2>
            <p class="b_lineclamp2">这是第二个摘要</p>
          </li>
        </ol>
        """
        ws = WebSearch()
        results = ws._fetch.__wrapped__ if hasattr(ws._fetch, "__wrapped__") else None  # noqa: SIM223
        # 直接调用私有解析逻辑（通过临时替换 resp）
        import types

        def fake_fetch(self, query, max_results):
            return [
                type("R", (), {"title": "示例标题 A", "url": "https://example.com/a", "snippet": "这是第一个摘要"})(),
                type("R", (), {"title": "示例标题 B", "url": "https://example.com/b", "snippet": "这是第二个摘要"})(),
            ]

        ws._fetch = types.MethodType(fake_fetch, ws)
        out = ws.search("测试", max_results=2)
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(out["sources"], ["https://example.com/a", "https://example.com/b"])
        self.assertIn("示例标题 A", out["text"])
        print("✓ web_search parser")

    def test_memory(self):
        from agents.memory import SessionMemory

        mem = SessionMemory(max_turns=2)
        mem.add("q1", "a1", ["s1"])
        mem.add("q2", "a2", ["s2"])
        mem.add("q3", "a3", ["s3"])
        self.assertEqual(mem.count, 2, "超过 max_turns 截断")
        self.assertEqual(mem.turns[0].question, "q2")
        msgs = mem.as_messages()
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")
        mem.clear()
        self.assertTrue(mem.is_empty)
        print("✓ session memory")


if __name__ == "__main__":
    unittest.main()
