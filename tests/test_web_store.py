"""WebStore 持久化测试：不依赖模型与网络。

覆盖：消息与反馈跨实例持久化、SessionMemory 跨实例恢复、工作区创建/激活/隔离、
会话清空（含滚动摘要清除，S4）与删除、统计口径（today_queries/kb_hits/hit_rate）、
滚动摘要 set_summary 落盘（S4）。原 run() 顺序流程整体保留为一个 test 方法，
临时目录用 tempfile 上下文自动清理。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.web_store import WebStore


class WebStoreTests(unittest.TestCase):
    """WebStore 持久化/隔离/删除/反馈/统计检查点。"""

    def test_persistence_isolation_deletion_feedback_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "webui.sqlite3"
            store = WebStore(db_path)
            store.ensure_default("/kb/default")
            session_a = store.create_session("default")
            session_b = store.create_session("default")
            store.add_message(session_a, "user", "A 的问题")
            answer_id = store.add_message(session_a, "assistant", "A 的回答", ["a.md"], {"summary": "A"})
            store.add_message(session_b, "user", "B 的问题")
            store.add_message(session_b, "assistant", "B 的回答", ["b.md"], {"summary": "B"})
            store.feedback(answer_id, "up")
            store.record_query("default", session_a, True)
            store.record_query("default", session_b, False)

            reopened = WebStore(db_path)
            self.assertEqual([m["content"] for m in reopened.messages(session_a)], ["A 的问题", "A 的回答"])
            self.assertEqual(reopened.messages(session_a)[1]["feedback"], "up")
            self.assertEqual(reopened.memory(session_a).turns[0].question, "A 的问题")
            self.assertEqual(reopened.memory(session_b).turns[0].question, "B 的问题")
            self.assertTrue(reopened.session_belongs_to(session_a, "default"))
            self.assertEqual(reopened.stats("default"), {"today_queries": 2, "kb_hits": 1, "hit_rate": 50.0})

            second = reopened.create_workspace("第二工作区", "/kb/second")
            reopened.activate_workspace(second)
            self.assertEqual(reopened.active_workspace()["id"], second)
            self.assertEqual(reopened.sessions(second), [])
            self.assertFalse(reopened.session_belongs_to(session_a, second))

            reopened.clear_session(session_a)
            self.assertEqual(reopened.messages(session_a), [])
            self.assertTrue(reopened.session_belongs_to(session_a, "default"))
            self.assertEqual(reopened.memory(session_a).summary, "")  # 清会话同时清滚动摘要（S4）

            reopened.delete_session(session_b)
            self.assertFalse(reopened.session_belongs_to(session_b, "default"))
            self.assertEqual(reopened.messages(session_b), [])
            self.assertEqual(reopened.stats("default"), {"today_queries": 0, "kb_hits": 0, "hit_rate": 0.0})

            # 会话记忆滚动摘要持久化（S4）：跨实例恢复，避免每次 ask 重复压缩
            store.set_summary(session_a, "此前讨论过 RAG 分块与检索")
            self.assertEqual(store.memory(session_a).summary, "此前讨论过 RAG 分块与检索")
            self.assertEqual(WebStore(db_path).memory(session_a).summary, "此前讨论过 RAG 分块与检索")

        print("✓ web store persistence, isolation, deletion, feedback and stats")


if __name__ == "__main__":
    unittest.main()
