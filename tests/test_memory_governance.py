"""G4 记忆治理测试（P0-2）。

全部离线：向量后端用内置 TfidfHashEmbeddingBackend；存储目录用 tempfile 临时目录；
Web 层用裸 Handler（object.__new__）直测路由与 payload 组装，不经 socket。
覆盖：list_episodes 过滤/搜索/截断、单条删除（含向量行对齐与会话持久化 roundtrip）、
按会话删除、清空（长期 + 实体）、/api/memory 系列 GET/DELETE/POST 路由、
/api/reset 联动删除长期记忆、webui 静态文案。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.embeddings import TfidfHashEmbeddingBackend
from agents.long_memory import EntityMemory, LongTermMemory, MemoryEpisode
from agents.web_server import Handler

REPO = Path(__file__).resolve().parent.parent


def make_memory(store_dir: Path, episodes: list[MemoryEpisode]) -> LongTermMemory:
    """TF-IDF 后端记忆库：写入给定条目（add 内部含持久化与淘汰）。"""
    lm = LongTermMemory(store_dir, TfidfHashEmbeddingBackend(hash_dim=256), max_episodes=100)
    for ep in episodes:
        lm.add(ep)
    return lm


def ep(session_id: str, question: str, answer_summary: str = "答案摘要", ts: str = "") -> MemoryEpisode:
    return MemoryEpisode(session_id=session_id, question=question, answer_summary=answer_summary, ts=ts)


class MemoryGovernanceTests(unittest.TestCase):
    """P0-2：查看、撤回、删除、清空 + 清空会话联动。"""

    # ---------------- list_episodes ----------------

    def test_list_episodes_filter_search_limit(self):
        """查看：session 过滤、q 大小写不敏感子串、limit 截断、total 为过滤后总数、新→旧排序。"""
        with tempfile.TemporaryDirectory() as td:
            lm = make_memory(Path(td), [
                ep("s1", "什么是 RAG", ts="2026-09-01T10:00:00"),
                ep("s1", "RAG 的应用场景", ts="2026-09-02T10:00:00"),
                ep("s2", "向量数据库选型", "RAG 系统离不开向量库", ts="2026-09-03T10:00:00"),
            ])
            # 全量：新→旧
            view = lm.list_episodes()
            self.assertEqual(view["total"], 3)
            self.assertEqual([e["question"] for e in view["episodes"]],
                             ["向量数据库选型", "RAG 的应用场景", "什么是 RAG"])
            # session 过滤
            view = lm.list_episodes(session_id="s1")
            self.assertEqual(view["total"], 2)
            self.assertTrue(all(e["session_id"] == "s1" for e in view["episodes"]))
            # q 子串（大小写不敏感，命中 answer_summary）
            view = lm.list_episodes(q="rag 系统")
            self.assertEqual([e["question"] for e in view["episodes"]], ["向量数据库选型"])
            # limit 截断但 total 不变
            view = lm.list_episodes(limit=1)
            self.assertEqual(len(view["episodes"]), 1)
            self.assertEqual(view["total"], 3)
            # 条目字段齐全（P0-2：来源会话、创建时间、命中次数）
            item = lm.list_episodes()["episodes"][0]
            for key in ("id", "session_id", "ts", "question", "answer_summary", "sources", "entities", "hits"):
                self.assertIn(key, item)

        print("✓ list_episodes：过滤/搜索/截断/排序/字段齐全")

    # ---------------- delete_episode ----------------

    def test_delete_episode_roundtrip_and_vectors(self):
        """单条删除：返回 True、条目消失、重新 load 后仍消失（持久化）；不存在返回 False。"""
        with tempfile.TemporaryDirectory() as td:
            target = ep("s1", "什么是 RAG", ts="2026-09-01T10:00:00")
            lm = make_memory(Path(td), [target, ep("s1", "第二个问题", ts="2026-09-02T10:00:00")])
            self.assertTrue(lm.delete_episode(target.id))
            self.assertEqual(lm.size, 1)
            self.assertEqual(lm.list_episodes()["episodes"][0]["question"], "第二个问题")
            self.assertFalse(lm.delete_episode("no-such-id"))

            # 持久化 roundtrip：新实例加载后同样只剩 1 条
            lm2 = LongTermMemory(Path(td), TfidfHashEmbeddingBackend(hash_dim=256), max_episodes=100)
            self.assertEqual(lm2.size, 1)

            # 非 TF-IDF 后端：删除后向量行对齐（search 不因维度错位抛错）
            class _HashBackend:
                name = "fakehash"
                dim = 32
                def embed_texts(self, texts):
                    import numpy as _np
                    rows = _np.zeros((len(texts), self.dim), dtype=_np.float32)
                    for i, t in enumerate(texts):  # one-hot：不同末字符正交，避免常量向量余弦恒为 1
                        rows[i, ord(t[-1]) % self.dim] = 1.0
                    return rows
                def embed_query(self, query):
                    return self.embed_texts([query])[0]
            with tempfile.TemporaryDirectory() as td2:
                lm3 = LongTermMemory(Path(td2), _HashBackend(), max_episodes=100)
                a, b, c = (ep("s", f"问题{i}") for i in range(3))
                for e in (a, b, c):
                    lm3.add(e)
                self.assertTrue(lm3.delete_episode(b.id))
                hits = lm3.search("问题0", k=3)
                self.assertEqual(hits[0].id, a.id, "问题0 应最相似")
                self.assertNotIn(b.id, [h.id for h in hits], "b 已删除，不应出现")

        print("✓ delete_episode：删除/持久化 roundtrip/不存在 False/非 TF-IDF 向量行对齐")

    # ---------------- delete_by_session / clear ----------------

    def test_delete_by_session_and_clear(self):
        """按会话删除只删该会话；清空清掉全部并落盘；空 session_id 返回 0。"""
        with tempfile.TemporaryDirectory() as td:
            lm = make_memory(Path(td), [
                ep("s1", "问题一", ts="2026-09-01T10:00:00"),
                ep("s1", "问题二", ts="2026-09-02T10:00:00"),
                ep("s2", "问题三", ts="2026-09-03T10:00:00"),
            ])
            self.assertEqual(lm.delete_by_session("s1"), 2)
            self.assertEqual(lm.size, 1)
            self.assertEqual(lm.list_episodes()["episodes"][0]["session_id"], "s2")
            self.assertEqual(lm.delete_by_session(""), 0)
            self.assertEqual(lm.clear(), 1)
            self.assertEqual(lm.size, 0)
            # 落盘：重新加载仍为空
            lm2 = LongTermMemory(Path(td), TfidfHashEmbeddingBackend(hash_dim=256), max_episodes=100)
            self.assertEqual(lm2.size, 0)

        print("✓ delete_by_session/clear：会话隔离、清空落盘、空参数返回 0")

    def test_entity_memory_clear(self):
        """实体记忆清空：返回数量、内存与磁盘同步清空。"""
        with tempfile.TemporaryDirectory() as td:
            em = EntityMemory(Path(td))
            em.entities = {"RAG": {"facts": ["检索增强生成"], "last_seen": "2026-09-01T00:00:00"},
                           "MCP": {"facts": ["模型上下文协议"], "last_seen": "2026-09-02T00:00:00"}}
            em.save()
            self.assertEqual(em.clear(), 2)
            self.assertEqual(em.entities, {})
            em2 = EntityMemory(Path(td))
            em2.load()
            self.assertEqual(em2.entities, {})

        print("✓ EntityMemory.clear：数量返回 + 内存/磁盘同步")

    # ---------------- Web API ----------------

    def _bare_handler(self, method_route: str) -> Handler:
        """裸 Handler：_check_host/_send_json 桩，不经 socket。method_route 形如 'GET /api/memory?q=x'。"""
        method, _, path = method_route.partition(" ")
        handler = object.__new__(Handler)
        handler.path = path
        handler.command = method
        handler.responses = {}
        handler._check_host = lambda: True
        captured: dict = {}

        def _send_json(obj, status=200):
            captured.update(body=obj, status=status)

        handler._send_json = _send_json
        handler._captured = captured
        return handler

    def test_api_memory_get_routes(self):
        """GET /api/memory 与 /api/memory/entities：参数透传与空库 disabled 标记。"""
        with tempfile.TemporaryDirectory() as td:
            lm = make_memory(Path(td), [ep("s1", "什么是 RAG", ts="2026-09-01T10:00:00")])
            em = EntityMemory()
            em.entities = {"RAG": {"facts": ["检索增强生成"], "last_seen": "2026-09-01T00:00:00"}}

            agent = NS(long_memory=lm, entity_memory=em)

            h = self._bare_handler("GET /api/memory?session_id=s1&q=rag&limit=10")
            h.agent = agent
            h._do_get()
            self.assertEqual(h._captured["status"], 200)
            body = h._captured["body"]
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["episodes"][0]["question"], "什么是 RAG")

            h = self._bare_handler("GET /api/memory/entities")
            h.agent = agent
            h._do_get()
            self.assertIn("RAG", h._captured["body"]["entities"])

        # 长期记忆未开启（long_memory None）→ 空列表 + disabled 标记
        h = self._bare_handler("GET /api/memory")
        h.agent = NS(long_memory=None, entity_memory=None)
        h._do_get()
        self.assertEqual(h._captured["body"], {"episodes": [], "total": 0, "disabled": True})

        print("✓ GET /api/memory[/entities]：参数透传、条目视图、未开启返回 disabled")

    def test_api_memory_delete_routes(self):
        """DELETE /api/memory/{id} 与 DELETE /api/memory?session_id=：成功 200、缺失 404。"""
        from types import SimpleNamespace as NS
        with tempfile.TemporaryDirectory() as td:
            target = ep("s1", "什么是 RAG", ts="2026-09-01T10:00:00")
            other = ep("s2", "别的问题", ts="2026-09-02T10:00:00")
            lm = make_memory(Path(td), [target, other])
            agent = NS(long_memory=lm, entity_memory=None)

            h = self._bare_handler(f"DELETE /api/memory/{target.id}")
            h.agent = agent
            h._do_delete()
            self.assertEqual(h._captured["status"], 200)
            self.assertTrue(h._captured["body"]["ok"])
            self.assertEqual(lm.size, 1)

            h = self._bare_handler("DELETE /api/memory/no-such-id")
            h.agent = agent
            h._do_delete()
            self.assertEqual(h._captured["status"], 404)

            h = self._bare_handler("DELETE /api/memory?session_id=s2")
            h.agent = agent
            h._do_delete()
            self.assertEqual(h._captured["body"]["removed"], 1)
            self.assertEqual(lm.size, 0)

            # 长期记忆未开启：删除幂等成功
            h = self._bare_handler("DELETE /api/memory?session_id=s1")
            h.agent = NS(long_memory=None, entity_memory=None)
            h._do_delete()
            self.assertEqual(h._captured["body"], {"ok": True, "removed": 0})

        print("✓ DELETE /api/memory[/{id}]：单条/按会话/404/未开启幂等")

    def test_api_memory_clear_route(self):
        """POST /api/memory/clear：清空长期记忆与实体记忆并返回计数。"""
        from types import SimpleNamespace as NS
        with tempfile.TemporaryDirectory() as td:
            lm = make_memory(Path(td), [ep("s1", "问题一"), ep("s1", "问题二")])
            em = EntityMemory()
            em.entities = {"RAG": {"facts": ["x"], "last_seen": ""}}
            agent = NS(long_memory=lm, entity_memory=em)

            handler = object.__new__(Handler)
            handler.path = "/api/memory/clear"
            handler._check_host = lambda: True
            handler._read_payload = lambda: {}
            handler.lock = threading.Lock()
            captured: dict = {}
            handler._send_json = lambda obj, status=200: captured.update(body=obj, status=status)
            handler.agent = agent
            handler._do_post()

            self.assertEqual(captured["status"], 200)
            self.assertEqual(captured["body"], {"ok": True, "episodes": 2, "entities": 1})
            self.assertEqual(lm.size, 0)
            self.assertEqual(em.entities, {})

        print("✓ POST /api/memory/clear：长期 + 实体同步清空并返回计数")

    def test_api_reset_syncs_long_term(self):
        """/api/reset 清空会话时同步删除该会话长期记忆（P0-2 问题点）。"""
        from types import SimpleNamespace as NS
        reset_calls: list[str] = []
        deleted: list[str] = []

        class _LM:
            def delete_by_session(self, session_id):
                deleted.append(session_id)
                return 3

        store = NS(
            session_belongs_to=lambda sid, wid: sid == "s1",
            clear_session=lambda sid: reset_calls.append(sid),
        )
        agent = NS(long_memory=_LM(), reset_memory=lambda: None)

        handler = object.__new__(Handler)
        handler.path = "/api/reset"
        handler._check_host = lambda: True
        handler._read_payload = lambda: {"session_id": "s1"}
        handler._active_workspace = lambda: {"id": "w1"}
        handler.lock = threading.Lock()
        captured: dict = {}
        handler._send_json = lambda obj, status=200: captured.update(body=obj, status=status)
        handler.store = store
        handler.agent = agent

        handler._do_post()

        self.assertEqual(reset_calls, ["s1"])
        self.assertEqual(deleted, ["s1"])
        self.assertEqual(captured["body"], {"ok": True, "memory_turns": 0, "long_term_removed": 3})

        # 长期记忆未开启（None）→ 联动跳过但不报错
        handler2 = object.__new__(Handler)
        handler2.path = "/api/reset"
        handler2._check_host = lambda: True
        handler2._read_payload = lambda: {"session_id": "s1"}
        handler2._active_workspace = lambda: {"id": "w1"}
        handler2.lock = threading.Lock()
        captured2: dict = {}
        handler2._send_json = lambda obj, status=200: captured2.update(body=obj, status=status)
        handler2.store = store
        handler2.agent = NS(long_memory=None, reset_memory=lambda: None)
        handler2._do_post()
        self.assertEqual(captured2["body"]["long_term_removed"], 0)

        print("✓ /api/reset 联动删除该会话长期记忆（未开启时幂等跳过）")

    # ---------------- webui 静态文案 ----------------

    def test_webui_memory_page_static(self):
        """webui.html 含记忆设置页：搜索/刷新/清空、逐条删除、实体事实展示与确认文案。"""
        html = (REPO / "scripts" / "webui.html").read_text(encoding="utf-8")
        self.assertIn('data-settings-page="memoryPage"', html, "记忆设置页标签")
        self.assertIn("此处可查看被记住的内容，并逐条撤回或全部清除", html)
        self.assertIn("async function loadMemory()", html)
        self.assertIn("fetch(`/api/memory?${params}`)", html)
        self.assertIn("fetch('/api/memory/entities')", html)
        self.assertIn("method:'DELETE'", html, "逐条删除调用 DELETE")
        self.assertIn("'/api/memory/clear'", html, "清空全部调用 clear 接口")
        self.assertIn("确定清空全部长期记忆与实体记忆吗", html, "清空确认文案")
        self.assertIn("长期记忆未开启", html, "未开启状态提示")

        print("✓ webui.html 记忆页静态断言：标签/接口调用/确认文案齐备")


if __name__ == "__main__":
    unittest.main()
