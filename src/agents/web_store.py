"""SQLite persistence for the local Web UI."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .memory import SessionMemory


class WebStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kb_path TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, content TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]',
                    plan TEXT NOT NULL DEFAULT '{}', metrics TEXT NOT NULL DEFAULT '{}', feedback TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS query_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    kb_hit INTEGER NOT NULL, created_at TEXT NOT NULL
                );
            """)
            # 迁移：sessions.summary 保存会话记忆滚动摘要（S4）。
            # 不持久化的话，每次 ask 重建记忆都会重新压缩并丢弃，白付一次 LLM 调用。
            cols = [r["name"] for r in db.execute("PRAGMA table_info(sessions)")]
            if "summary" not in cols:
                db.execute("ALTER TABLE sessions ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            # 迁移：messages.sources_detail 保存结构化来源详情（W1，JSON 数组，与 sources 下标对应）。
            cols = [r["name"] for r in db.execute("PRAGMA table_info(messages)")]
            if "sources_detail" not in cols:
                db.execute("ALTER TABLE messages ADD COLUMN sources_detail TEXT NOT NULL DEFAULT '[]'")

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def ensure_default(self, kb_path: str):
        with self._connect() as db:
            row = db.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
            if row:
                return
            db.execute("INSERT INTO workspaces VALUES (?,?,?,?,?)", ("default", "默认工作区", kb_path, 1, self._now()))

    def workspaces(self):
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM workspaces ORDER BY active DESC, created_at")]

    def active_workspace(self):
        with self._connect() as db:
            row = db.execute("SELECT * FROM workspaces WHERE active=1 LIMIT 1").fetchone()
            return dict(row) if row else None

    def create_workspace(self, name: str, kb_path: str):
        wid = uuid.uuid4().hex[:12]
        with self._connect() as db:
            db.execute("INSERT INTO workspaces VALUES (?,?,?,?,?)", (wid, name, kb_path, 0, self._now()))
        return wid

    def activate_workspace(self, workspace_id: str):
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                raise KeyError(workspace_id)
            db.execute("UPDATE workspaces SET active=0")
            db.execute("UPDATE workspaces SET active=1 WHERE id=?", (workspace_id,))

    def sessions(self, workspace_id: str):
        with self._connect() as db:
            rows = db.execute("""SELECT s.*, COUNT(m.id) message_count FROM sessions s
                LEFT JOIN messages m ON m.session_id=s.id WHERE s.workspace_id=?
                GROUP BY s.id ORDER BY s.updated_at DESC""", (workspace_id,))
            return [dict(r) for r in rows]

    def create_session(self, workspace_id: str, title: str = "新的会话"):
        sid, now = uuid.uuid4().hex, self._now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions(id,workspace_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (sid, workspace_id, title, now, now),
            )
        return sid

    def session_belongs_to(self, session_id: str, workspace_id: str) -> bool:
        with self._connect() as db:
            return db.execute(
                "SELECT 1 FROM sessions WHERE id=? AND workspace_id=?", (session_id, workspace_id)
            ).fetchone() is not None

    def clear_session(self, session_id: str):
        with self._connect() as db:
            db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM query_events WHERE session_id=?", (session_id,))
            db.execute(
                "UPDATE sessions SET title='新的会话', summary='', updated_at=? WHERE id=?",
                (self._now(), session_id),
            )

    def delete_session(self, session_id: str):
        with self._connect() as db:
            db.execute("DELETE FROM query_events WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def messages(self, session_id: str):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))
            out = []
            for row in rows:
                item = dict(row)
                for key in ("sources", "plan", "metrics", "sources_detail"):
                    item[key] = json.loads(item[key])
                out.append(item)
            return out

    def add_message(self, session_id: str, role: str, content: str, sources=None, plan=None, metrics=None, sources_detail=None):
        now = self._now()
        with self._connect() as db:
            cur = db.execute("INSERT INTO messages(session_id,role,content,sources,sources_detail,plan,metrics,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, role, content, json.dumps(sources or [], ensure_ascii=False),
                 json.dumps(sources_detail or [], ensure_ascii=False),
                 json.dumps(plan or {}, ensure_ascii=False), json.dumps(metrics or {}, ensure_ascii=False), now))
            title = content.strip().replace("\n", " ")[:24] if role == "user" else ""
            if title:
                db.execute("UPDATE sessions SET title=CASE WHEN title='新的会话' THEN ? ELSE title END, updated_at=? WHERE id=?", (title, now, session_id))
            else:
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            return cur.lastrowid

    def memory(self, session_id: str, max_turns: int = 6):
        memory = SessionMemory(max_turns=max_turns)
        rows = self.messages(session_id)
        pending = None
        for row in rows:
            if row["role"] == "user":
                pending = row["content"]
            elif row["role"] == "assistant" and pending is not None:
                memory.add(pending, row["content"], row["sources"], row["plan"].get("summary", ""))
                pending = None
        # 恢复会话记忆的滚动摘要（S4）：避免重建后重复压缩
        with self._connect() as db:
            row = db.execute("SELECT summary FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row and row["summary"]:
            memory.summary = row["summary"]
        return memory

    def set_summary(self, session_id: str, summary: str):
        """保存会话记忆的滚动摘要（S4），跨重启复用，避免每次 ask 重复压缩。"""
        with self._connect() as db:
            db.execute("UPDATE sessions SET summary=? WHERE id=?", (summary or "", session_id))

    # ---- 任务写回（P0-5：幂等） ----
    def has_task_writeback(self, session_id: str, task_id: str) -> bool:
        """该会话是否已写回过指定任务的结果（按 assistant 消息 metrics.task_id 判断）。

        覆盖「记录 writeback_id 前崩溃 / 回调重放 / 旧记录升级」等场景，
        与 TaskRecord.writeback_id 双保险，保证重复回调不产生重复消息。
        """
        if not session_id or not task_id:
            return False
        for message in self.messages(session_id):
            metrics = message.get("metrics")
            if (
                message.get("role") == "assistant"
                and isinstance(metrics, dict)
                and metrics.get("task_id") == task_id
            ):
                return True
        return False

    def add_task_result(
        self, session_id: str, workspace_id: str, question: str, answer: str,
        sources=None, plan=None, metrics=None,
    ) -> None:
        """任务结果原子写回：user + assistant 消息与查询事件在同一连接/事务内落库。

        写回失败时整体不生效（无半写状态），配合 has_task_writeback 可安全重试。
        """
        now = self._now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO messages(session_id,role,content,sources,plan,metrics,created_at) VALUES (?,?,?,?,?,?,?)",
                (session_id, "user", question, json.dumps([], ensure_ascii=False),
                 json.dumps({}, ensure_ascii=False), json.dumps({}, ensure_ascii=False), now),
            )
            db.execute(
                "INSERT INTO messages(session_id,role,content,sources,plan,metrics,created_at) VALUES (?,?,?,?,?,?,?)",
                (session_id, "assistant", answer, json.dumps(list(sources or []), ensure_ascii=False),
                 json.dumps(plan or {}, ensure_ascii=False), json.dumps(metrics or {}, ensure_ascii=False), now),
            )
            title = question.strip().replace("\n", " ")[:24]
            if title:
                db.execute(
                    "UPDATE sessions SET title=CASE WHEN title='新的会话' THEN ? ELSE title END, updated_at=? WHERE id=?",
                    (title, now, session_id),
                )
            else:
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            db.execute(
                "INSERT INTO query_events(workspace_id,session_id,kb_hit,created_at) VALUES (?,?,?,?)",
                (workspace_id, session_id, 1 if any(not str(s).startswith("http") for s in (sources or [])) else 0, now),
            )

    def feedback(self, message_id: int, value: str):
        if value not in ("up", "down", ""):
            raise ValueError(value)
        with self._connect() as db:
            db.execute("UPDATE messages SET feedback=? WHERE id=?", (value, message_id))

    def record_query(self, workspace_id: str, session_id: str, kb_hit: bool):
        with self._connect() as db:
            db.execute("INSERT INTO query_events(workspace_id,session_id,kb_hit,created_at) VALUES (?,?,?,?)",
                       (workspace_id, session_id, int(kb_hit), self._now()))

    def stats(self, workspace_id: str):
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) total, COALESCE(SUM(kb_hit),0) hits FROM query_events
                WHERE workspace_id=? AND date(created_at,'localtime')=date('now','localtime')""", (workspace_id,)).fetchone()
        total, hits = int(row["total"]), int(row["hits"])
        return {"today_queries": total, "kb_hits": hits, "hit_rate": round(hits * 100 / total, 1) if total else 0.0}
