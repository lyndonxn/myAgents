"""Web UI persistence tests; no model or network required."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.web_store import WebStore


def run():
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
        assert [m["content"] for m in reopened.messages(session_a)] == ["A 的问题", "A 的回答"]
        assert reopened.messages(session_a)[1]["feedback"] == "up"
        assert reopened.memory(session_a).turns[0].question == "A 的问题"
        assert reopened.memory(session_b).turns[0].question == "B 的问题"
        assert reopened.session_belongs_to(session_a, "default")
        assert reopened.stats("default") == {"today_queries": 2, "kb_hits": 1, "hit_rate": 50.0}

        second = reopened.create_workspace("第二工作区", "/kb/second")
        reopened.activate_workspace(second)
        assert reopened.active_workspace()["id"] == second
        assert reopened.sessions(second) == []
        assert not reopened.session_belongs_to(session_a, second)

        reopened.clear_session(session_a)
        assert reopened.messages(session_a) == []
        assert reopened.session_belongs_to(session_a, "default")

        reopened.delete_session(session_b)
        assert not reopened.session_belongs_to(session_b, "default")
        assert reopened.messages(session_b) == []
        assert reopened.stats("default") == {"today_queries": 0, "kb_hits": 0, "hit_rate": 0.0}

    print("✓ web store persistence, isolation, deletion, feedback and stats")


if __name__ == "__main__":
    run()
