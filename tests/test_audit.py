"""G6 审计轨迹测试（spec ACC-U6-01..04）。

全部离线：审计目录用 tempfile（绝不写用户 data/audit/）；LLM 用记录调用、
按脚本返回文本的假客户端（子类化 LLMClient 覆写 _chat，经公开方法 chat 的
埋点生效）；Web 层用裸 Handler 直测。覆盖：分层事件（ask/tool_call/llm_call/admin）、
正文默认不记/显式开启才记、无 Key、按天 JSONL、保留期清理、查询过滤与非法参数 400。
"""
from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import audit as audit_mod
from agents import config as config_mod
from agents.agent import Agent
from agents.audit import AuditLogger
from agents.config import Config
from agents.llm import ChatResult, LLMClient
from agents.memory import SessionMemory
from agents.planner import Plan, PlanStep
from agents.task_runner import TaskRunner
from agents.task_store import TaskRecord, TaskStore
from agents.tools import Tool, ToolContext
from agents.web_server import Handler

REPO = Path(__file__).resolve().parent.parent

PLAN_KB_JSON = json.dumps({
    "reasoning": "测试规划", "plan_summary": "检索一步",
    "steps": [{"action": "search_knowledge_base", "input": {"query": "RAG"}, "purpose": ""}],
}, ensure_ascii=False)
REFLECT_NONE_JSON = json.dumps({"need_more": False, "reasoning": "信息足够", "steps": []}, ensure_ascii=False)
QUESTION = "审计测试的提问内容XYZ"
ANSWER_TEXT = "审计测试的答案内容ABC"


class ScriptedLLM(LLMClient):
    """离线假 LLM：覆写 _chat（经公开方法 chat/chat_json 的埋点触发 llm_call 事件）。"""

    def __init__(self, texts: list[str], config: Config):
        self.config = config
        self._texts = list(texts)
        self.calls = 0

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


def _make_tool(name, func):
    return Tool(
        name=name, description=f"测试工具 {name}",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        func=func,
    )


def kb_hit_tool():
    def probe(ctx, query=""):
        return {"text": f"检索 {query} 的片段", "sources": ["kb://a.md"], "hit_count": 2}
    return _make_tool("search_knowledge_base", probe)


def make_agent(llm, tmp: Path):
    agent = Agent(Config({"tools": {"max_retries": 0}}), llm=llm, lazy_index=True)
    agent._index_loaded = True
    agent.tools = {"search_knowledge_base": kb_hit_tool()}
    agent._ctx = ToolContext()
    return agent


class AuditTrailTests(unittest.TestCase):
    """ACC-U6-01..04 + 保留期清理。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.audit_dir = self.tmp / "audit"
        self.logger = AuditLogger(self.audit_dir, retention_days=30, log_content=False)
        audit_mod.set_logger(self.logger)

    def tearDown(self):
        audit_mod.set_logger(None)
        audit_mod.set_current_session("")  # 清理会话上下文
        self._tmp.cleanup()

    def _read_today(self) -> list[dict]:
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.audit_dir / f"{today}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _ask_once(self):
        llm = ScriptedLLM([PLAN_KB_JSON, REFLECT_NONE_JSON, ANSWER_TEXT], Config({}))
        agent = make_agent(llm, self.tmp)
        return agent.ask(QUESTION, session_id="s-audit")

    def test_acc_u6_01_layered_events_no_content_no_key(self):
        """成功问答 → ask 1 条、每工具 1 条 tool_call、每 LLM 调用 1 条 llm_call；无正文无 Key。"""
        answer = self._ask_once()
        self.assertTrue(answer.ok)

        events = self._read_today()
        types = [e["type"] for e in events]
        self.assertEqual(types.count("ask"), 1)
        self.assertEqual(types.count("tool_call"), 1)
        self.assertEqual(types.count("llm_call"), 3, "规划+反思+合成共 3 次 LLM 调用")

        ask_event = next(e for e in events if e["type"] == "ask")
        self.assertEqual(ask_event["session_id"], "s-audit")
        self.assertTrue(ask_event["ok"])
        self.assertFalse(ask_event["degraded"])
        self.assertFalse(ask_event["web_used"])

        blob = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(QUESTION, blob, "默认不记录问题正文")
        self.assertNotIn(ANSWER_TEXT, blob, "默认不记录答案正文")
        self.assertNotIn("sk-", blob)
        self.assertNotIn("api_key", blob)

        tool_event = next(e for e in events if e["type"] == "tool_call")
        self.assertEqual(tool_event["action"], "search_knowledge_base")
        self.assertEqual(tool_event["input_keys"], ["query"], "参数只记键名摘要")
        self.assertNotIn("RAG", json.dumps(tool_event, ensure_ascii=False), "参数值不得入审计")

        print("✓ ACC-U6-01：分层事件齐全、会话关联、无正文无 Key、参数只记键名")

    def test_acc_u6_02_content_opt_in(self):
        """正文记录：默认关不写问题/答案；显式开启后 ask 事件含问题与答案。"""
        self._ask_once()
        default_events = self._read_today()
        ask_event = next(e for e in default_events if e["type"] == "ask")
        self.assertNotIn("question", ask_event)
        self.assertNotIn("answer", ask_event)

        # 开启正文记录
        self.audit_dir = self.tmp / "audit2"
        self.logger = AuditLogger(self.audit_dir, retention_days=30, log_content=True)
        audit_mod.set_logger(self.logger)
        self._ask_once()
        events = self._read_today()
        ask_event = next(e for e in events if e["type"] == "ask")
        self.assertEqual(ask_event["question"], QUESTION)
        self.assertEqual(ask_event["answer"], ANSWER_TEXT)

        print("✓ ACC-U6-02：正文默认不记，显式开启后 ask 事件含问题与答案")

    def test_query_filters_and_limit(self):
        """query：date 过滤、type 过滤、limit 截断（新→旧）。"""
        self.logger.log_event("admin", action="a1")
        self.logger.log_event("admin", action="a2")
        self.logger.log_event("ask", ok=True)
        today = datetime.now().strftime("%Y-%m-%d")

        self.assertEqual(len(self.logger.query()), 3)
        admins = self.logger.query(event_type="admin")
        self.assertEqual([e["action"] for e in admins], ["a2", "a1"], "新→旧排序")
        self.assertEqual(len(self.logger.query(limit=1)), 1)
        self.assertEqual(self.logger.query(date="1999-01-01"), [])
        # 未来日期文件不存在 → 空列表（不是错误）
        self.assertEqual(self.logger.query(date="2030-01-01"), [])

        print("✓ query：date/type/limit 过滤与排序正确")

    def test_retention_cleanup(self):
        """保留期：超过 retention_days 的审计文件在下次写入时被清理。"""
        old_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        old_file = self.audit_dir / f"{old_date}.jsonl"
        old_file.write_text('{"type": "old"}\n', encoding="utf-8")
        recent_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        recent_file = self.audit_dir / f"{recent_date}.jsonl"
        recent_file.write_text('{"type": "recent"}\n', encoding="utf-8")

        AuditLogger._last_cleanup_date = ""  # 重置惰性标记
        self.logger.log_event("admin", action="x")

        self.assertFalse(old_file.exists(), "超期文件应被清理")
        self.assertTrue(recent_file.exists(), "保留期内文件不动")

        print("✓ 保留期清理：超期删除、保留期内不动")

    def test_acc_u6_03_admin_events(self):
        """记忆删除与配置保存 → admin 事件存在且含操作者会话与结果。"""
        handler = object.__new__(Handler)
        handler.path = "/api/memory/clear"
        handler._check_host = lambda: True
        handler._read_payload = lambda: {}
        handler.lock = threading.Lock()
        handler._send_json = lambda obj, status=200: None
        handler.agent = NS(long_memory=None, entity_memory=None)  # 记忆未开启：ok + 0
        handler._do_post()

        # DELETE 单条（不存在 → ok=False 事件）
        handler2 = object.__new__(Handler)
        handler2.path = "/api/memory/no-such-id"
        handler2._check_host = lambda: True
        handler2._send_json = lambda obj, status=200: None
        handler2.agent = NS(long_memory=None, entity_memory=None)
        handler2._do_delete()

        # 配置保存（成功路径）
        import os
        rt = self.tmp / "runtime.json"
        rt.write_text("{}", encoding="utf-8")
        os.chmod(rt, 0o600)
        saved = {n: getattr(config_mod, n) for n in ("ENV_PATH", "RUNTIME_PATH")}
        config_mod.ENV_PATH = self.tmp / ".env"
        config_mod.RUNTIME_PATH = rt
        try:
            handler3 = object.__new__(Handler)
            handler3.path = "/api/config"
            handler3._check_host = lambda: True
            handler3.lock = threading.Lock()
            handler3._send_json = lambda obj, status=200: None
            handler3.agent = NS(config=Config({}), reconfigure=lambda: None)
            handler3._save_config({"llm": {"chat_model": "deepseek-chat"}})
        finally:
            for n, v in saved.items():
                setattr(config_mod, n, v)

        events = self._read_today()
        admins = [e for e in events if e["type"] == "admin"]
        actions = {e["action"] for e in admins}
        self.assertIn("memory_clear", actions)
        self.assertIn("memory_delete", actions)
        self.assertIn("config_save", actions)
        by_action = {e["action"]: e for e in admins}
        self.assertEqual(by_action["memory_clear"]["detail"], "episodes=0,entities=0")
        self.assertFalse(by_action["memory_delete"]["ok"], "不存在的条目 → ok=False")
        self.assertIn("llm.chat_model", by_action["config_save"]["detail"])
        self.assertNotIn("deepseek-chat", json.dumps(admins, ensure_ascii=False), "配置值不入审计")

        print("✓ ACC-U6-03：admin 事件含动作/结果摘要；配置只记键名不记值")

    def test_acc_u6_04_api_audit_query(self):
        """GET /api/audit：过滤返回匹配事件；非法参数 4xx。"""
        self.logger.log_event("ask", ok=True)
        self.logger.log_event("admin", action="a1")

        def make_get(path):
            handler = object.__new__(Handler)
            handler.path = path
            handler._check_host = lambda: True
            captured = {}
            handler._send_json = lambda obj, status=200: captured.update(body=obj, status=status)
            return handler, captured

        h, captured = make_get("/api/audit?type=admin&limit=10")
        h._do_get()
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["body"]["total"], 1)
        self.assertEqual(captured["body"]["events"][0]["action"], "a1")

        for path, expect_status in (
            ("/api/audit?date=bad-date", 400),
            ("/api/audit?limit=abc", 400),
            ("/api/audit?limit=0", 400),
            ("/api/audit?limit=99999", 400),
            ("/api/audit/nope", 404),
        ):
            h, captured = make_get(path)
            h._do_get()
            self.assertEqual(captured["status"], expect_status, f"{path} 应返回 {expect_status}")

        print("✓ ACC-U6-04：/api/audit 过滤返回匹配事件，非法参数 4xx")

    def test_task_ask_event_with_task_id(self):
        """任务路径：completed 后产生带 task_id 的 ask 事件。"""
        with tempfile.TemporaryDirectory() as td:
            def fake_plan_only(question, session_id="", allow_web=None):
                return (Plan(plan_summary="检索一步",
                             steps=[PlanStep(action="search_knowledge_base", input={"query": "q"}, step_id=1)]),
                        question, "")

            def fake_finish(question, plan, steps, history_text=""):
                return "任务答案[1]", ["kb://a.md"], NS(valid_count=1, invalid_count=0)

            stub_agent = NS(
                config=Config({"tools": {"max_retries": 0}}),
                tools={"search_knowledge_base": kb_hit_tool()},
                _ctx=ToolContext(),
                memory=SessionMemory(),
                prompt_tokens=0, completion_tokens=0, estimated_cost=0.0,
                plan_only=fake_plan_only,
                finish_task=fake_finish,
            )
            from types import MethodType

            stub_agent.resolve_allow_web = MethodType(Agent.resolve_allow_web, stub_agent)

            store = TaskStore(Path(td) / "tasks")
            runner = TaskRunner(store, threading.Lock(), agent_provider=lambda: stub_agent,
                                memory_provider=lambda sid: SessionMemory())
            runner._queue = queue.Queue()
            record = store.get(runner.submit("s1", "w1", "任务问题"))
            runner._execute(record, threading.Event(), threading.Event())

            events = self._read_today()
            ask_events = [e for e in events if e["type"] == "ask" and e.get("task_id")]
            self.assertEqual(len(ask_events), 1)
            self.assertEqual(ask_events[0]["task_id"], record.task_id)
            self.assertEqual(ask_events[0]["session_id"], "s1")

        print("✓ 任务路径：completed 后 ask 事件带 task_id 与会话")


if __name__ == "__main__":
    unittest.main()
