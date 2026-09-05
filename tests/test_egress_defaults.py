"""G3 数据外发默认关闭 + 回答状态披露测试（P0-3 + ACC-U3-01..03）。

全部离线：LLM 用记录调用、按脚本返回文本的假客户端（子类化 LLMClient 覆写 _chat）；
配置路径 monkeypatch 到临时目录（绝不读写用户 data/runtime.json 与 .env）；
Web 层用裸 Handler（object.__new__）直测 payload 组装与请求解析，不经 socket。
覆盖：三开关默认 false / runtime 显式值优先 / 迁移提示一次 / allow_web 强禁强许 /
remember 不写记忆 / metrics 外发标记 / API 透传 / webui 静态文案。
"""
from __future__ import annotations

import io
import json
import logging
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import config as config_mod
from agents.agent import Agent, Answer
from agents.config import Config, load_config
from agents.executor import StepResult
from agents.llm import ChatResult, LLMClient
from agents.memory import SessionMemory
from agents.planner import Plan, PlanStep
from agents.task_runner import TaskRunner
from agents.task_store import TaskStore
from agents.tools import Tool, ToolContext
from agents.web_server import Handler, _egress_flags, parse_opt_bool

REPO = Path(__file__).resolve().parent.parent

PLAN_KB_JSON = json.dumps(
    {
        "reasoning": "测试规划",
        "plan_summary": "检索一步",
        "steps": [{"action": "search_knowledge_base", "input": {"query": "RAG"}, "purpose": ""}],
    },
    ensure_ascii=False,
)
REFLECT_NONE_JSON = json.dumps({"need_more": False, "reasoning": "信息足够", "steps": []}, ensure_ascii=False)
ENTITY_JSON = json.dumps({"entities": {"RAG": "检索增强生成"}}, ensure_ascii=False)


class ScriptedLLM(LLMClient):
    """离线假 LLM：按脚本顺序返回文本，记录每次调用与收到的 messages。"""

    def __init__(self, texts: list[str], config: Config):
        self.config = config
        self._texts = list(texts)
        self.calls = 0
        self.seen: list[list[dict]] = []

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


class FakeWebSearch:
    """假 Web 搜索后端：记录 query，返回固定结果（不联网）。"""

    def __init__(self):
        self.calls: list[str] = []

    def search(self, query: str, max_results: int = 5) -> dict:
        self.calls.append(query)
        return {"text": f"网页结果 {query}", "sources": ["https://example.com/x"]}


def _make_tool(name: str, func, properties: dict, required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=f"测试工具 {name}",
        parameters={"type": "object", "properties": properties, "required": required},
        func=func,
    )


def broken_kb_tool() -> Tool:
    """必抛错的 KB 工具（用于验证降级路径是否被跳过）。"""
    def broken(ctx, **kwargs):
        raise RuntimeError("知识库索引损坏")

    return _make_tool("search_knowledge_base", broken, {"query": {"type": "string"}}, ["query"])


def kb_hit_tool() -> Tool:
    def probe(ctx, query=""):
        return {"text": f"检索 {query} 的片段", "sources": ["kb://a.md"], "hit_count": 2}

    return _make_tool("search_knowledge_base", probe, {"query": {"type": "string"}}, ["query"])


def _make_agent(llm: ScriptedLLM, tools: dict[str, Tool], raw: dict | None = None) -> Agent:
    """离线 Agent：不加载索引，注入假工具注册表与执行上下文。

    G9：本文件测试考察完整规划路径，默认关闭快路径（调用方可显式覆盖）。
    """
    merged = {"planner": {"fast_path": False}, **(raw or {})}
    agent = Agent(Config(merged), llm=llm, lazy_index=True)
    agent._index_loaded = True
    agent.tools = tools
    agent._ctx = ToolContext()
    return agent


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class EgressDefaultsTests(unittest.TestCase):
    """P0-3 + ACC-U3-01..03：外发默认关闭、请求级许可与状态披露。"""

    # ---------------- 默认值翻转（config.yaml 与 property 兜底一致） ----------------

    def test_three_switches_default_false(self):
        """P0-3：config.yaml 三开关为 false，Config 属性兜底默认同为 false。"""
        raw = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
        self.assertIs(raw["tools"]["kb_fallback_web"], False, "config.yaml kb_fallback_web 应为 false")
        self.assertIs(raw["memory"]["long_term_enabled"], False, "config.yaml long_term_enabled 应为 false")
        self.assertIs(raw["memory"]["entities_enabled"], False, "config.yaml entities_enabled 应为 false")

        cfg = Config({})
        self.assertIs(cfg.kb_fallback_web, False, "property 兜底默认应与 config.yaml 一致")
        self.assertIs(cfg.memory_long_term_enabled, False)
        self.assertIs(cfg.memory_entities_enabled, False)

        print("✓ 三开关默认 false（config.yaml 与 property 兜底一致）")

    # ---------------- runtime.json 显式值优先 + 迁移提示 ----------------

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {name: getattr(config_mod, name) for name in ("CONFIG_PATH", "RUNTIME_PATH", "ENV_PATH")}
        config_mod.CONFIG_PATH = REPO / "config.yaml"  # 只读仓库配置
        config_mod.RUNTIME_PATH = self.tmp / "runtime.json"
        config_mod.ENV_PATH = self.tmp / ".env"
        config_mod._migration_hint_shown = False

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(config_mod, name, value)
        config_mod._migration_hint_shown = False
        self._tmp.cleanup()

    def _capture_config_log(self) -> tuple[list[str], "_CaptureHandler"]:
        handler = _CaptureHandler()
        logger = logging.getLogger("config")
        logger.addHandler(handler)
        return logger, handler

    def test_runtime_explicit_true_wins(self):
        """P0-3 兼容策略：runtime.json 显式 true 优先于新默认值，不被覆盖。"""
        config_mod.RUNTIME_PATH.write_text(json.dumps({
            "tools": {"kb_fallback_web": True},
            "memory": {"long_term_enabled": True, "entities_enabled": True},
        }), encoding="utf-8")
        cfg = load_config()
        self.assertIs(cfg.kb_fallback_web, True)
        self.assertIs(cfg.memory_long_term_enabled, True)
        self.assertIs(cfg.memory_entities_enabled, True)

        print("✓ runtime.json 显式 true 仍生效（不强制覆盖）")

    def test_migration_hint_once_per_process(self):
        """runtime.json 存在且未显式设置三开关 → INFO 迁移提示一次/进程；显式设置或缺文件不提示。"""
        config_mod.RUNTIME_PATH.write_text(json.dumps({"llm": {"chat_model": "deepseek-chat"}}), encoding="utf-8")
        logger, handler = self._capture_config_log()
        try:
            load_config()
            load_config()  # 同进程第二次：不再提示
        finally:
            logger.removeHandler(handler)
        hinted = [m for m in handler.messages if "默认行为已变更" in m]
        self.assertEqual(len(hinted), 1, f"迁移提示应恰好一次，实际 {hinted}")
        self.assertIn("联网降级与长期记忆默认关闭", hinted[0])

        # runtime.json 显式设置全部三键 → 不提示
        handler.messages.clear()
        config_mod._migration_hint_shown = False
        config_mod.RUNTIME_PATH.write_text(json.dumps({
            "tools": {"kb_fallback_web": False},
            "memory": {"long_term_enabled": True, "entities_enabled": True},
        }), encoding="utf-8")
        logger, handler = self._capture_config_log()
        try:
            load_config()
        finally:
            logger.removeHandler(handler)
        self.assertEqual([m for m in handler.messages if "默认行为已变更" in m], [], "显式设置后不应提示")

        # runtime.json 不存在 → 不提示
        handler.messages.clear()
        config_mod._migration_hint_shown = False
        config_mod.RUNTIME_PATH.unlink()
        logger, handler = self._capture_config_log()
        try:
            load_config()
        finally:
            logger.removeHandler(handler)
        self.assertEqual([m for m in handler.messages if "默认行为已变更" in m], [], "无 runtime.json 不应提示")

        print("✓ 迁移提示：未显式设置时提示一次/进程，显式设置或缺文件不提示")

    # ---------------- allow_web 请求级许可 ----------------

    def test_allow_web_false_hard_forbids(self):
        """allow_web=False 强禁（配置 true 也禁）：规划/反思工具清单无 web_search、降级跳过、不外发。"""
        web = FakeWebSearch()
        llm = ScriptedLLM([PLAN_KB_JSON, REFLECT_NONE_JSON, "回答"], Config({}))
        agent = _make_agent(
            llm, {"search_knowledge_base": broken_kb_tool()}, {"tools": {"kb_fallback_web": True, "max_retries": 0}}
        )
        answer = agent.ask("什么是 RAG", allow_web=False)

        self.assertTrue(answer.ok, f"ask 不应失败: {answer.error}")
        planner_user = llm.seen[0][1]["content"]
        self.assertNotIn("web_search", planner_user, "规划工具清单不应含 web_search")
        reflect_user = llm.seen[1][1]["content"]
        self.assertNotIn("web_search", reflect_user, "反思工具清单不应含 web_search")
        step = answer.steps[0]
        self.assertFalse(step.ok, "KB 工具必抛错，步骤应为失败")
        self.assertFalse(step.degraded, "强禁联网时降级不应触发")
        self.assertNotIn("[降级]", str(step.output))
        self.assertEqual(web.calls, [], "强禁联网时不应有任何 Web 调用")

        print("✓ allow_web=false：工具清单无 web_search、降级跳过、零 Web 调用（配置 true 也禁）")

    def test_allow_web_true_overrides_disabled_config(self):
        """allow_web=True 强许（配置 false 也生效）：web_search 进入规划清单且降级可用。"""
        web = FakeWebSearch()
        llm = ScriptedLLM([PLAN_KB_JSON, REFLECT_NONE_JSON, "回答[1]"], Config({}))
        agent = _make_agent(
            llm, {"search_knowledge_base": broken_kb_tool()}, {"tools": {"kb_fallback_web": False, "max_retries": 0}}
        )
        agent._ctx = ToolContext(web_search=web)  # 执行时 web 工具经 ctx 取后端
        agent.web_search = web  # 注册表无 web_search：强许补挂路径依赖 Agent.web_search 判存

        answer = agent.ask("什么是 RAG", allow_web=True)

        self.assertTrue(answer.ok, f"ask 不应失败: {answer.error}")
        planner_user = llm.seen[0][1]["content"]
        self.assertIn("web_search", planner_user, "强许时规划工具清单应含 web_search")
        step = answer.steps[0]
        self.assertTrue(step.ok)
        self.assertTrue(step.degraded, "强许时 KB 失败应走 Web 降级")
        self.assertTrue(str(step.output["text"]).startswith("[降级]"))
        self.assertEqual(web.calls, ["RAG"], "降级应使用同一 query 调 Web")

        print("✓ allow_web=true：配置 false 时工具清单含 web_search、降级允许并成功")

    def test_allow_web_none_uses_config(self):
        """缺省 None → 按 config：默认（false）不外发；显式开启配置后恢复现状行为。"""
        # 默认配置（kb_fallback_web=false）：规划清单无 web_search、无降级
        web = FakeWebSearch()
        llm = ScriptedLLM([PLAN_KB_JSON, REFLECT_NONE_JSON, "回答"], Config({}))
        agent = _make_agent(llm, {"search_knowledge_base": broken_kb_tool()}, {"tools": {"max_retries": 0}})
        answer = agent.ask("什么是 RAG")
        self.assertNotIn("web_search", llm.seen[0][1]["content"], "默认配置下规划清单不应含 web_search")
        self.assertFalse(answer.steps[0].degraded)
        self.assertEqual(web.calls, [])
        self.assertFalse(answer.steps[0].ok, "KB 失败且未联网 → 普通失败（可解释未命中）")

        # 配置显式 true（恢复旧行为）：注册表含 web_search → 规划清单含 web_search
        web_tool = _make_tool(
            "web_search", lambda ctx, query="", max_results=5: {"text": "w", "sources": []},
            {"query": {"type": "string"}}, ["query"],
        )
        llm2 = ScriptedLLM([PLAN_KB_JSON, REFLECT_NONE_JSON, "回答"], Config({}))
        agent2 = _make_agent(
            llm2, {"search_knowledge_base": broken_kb_tool(), "web_search": web_tool},
            {"tools": {"kb_fallback_web": True, "max_retries": 0}},
        )
        agent2.ask("什么是 RAG")
        self.assertIn("web_search", llm2.seen[0][1]["content"], "配置开启时规划清单应含 web_search")

        print("✓ allow_web 缺省 None → 按 config（默认关不外发；显式开启恢复现状）")

    def test_kb_miss_treats_forbidden_web_as_offline(self):
        """_kb_miss「未联网」口径（G3）：web_allowed=False 时即使存在 web 步骤也视为未联网。"""
        zero = [StepResult(step_id=1, action="search_knowledge_base", output={"text": "0 条", "hit_count": 0})]
        web_step = StepResult(step_id=2, action="web_search", output={"text": "网页"})
        self.assertIs(Agent._kb_miss(zero + [web_step], web_allowed=True), False)
        self.assertIs(Agent._kb_miss(zero + [web_step], web_allowed=False), True, "禁网口径应视为未联网")
        self.assertIs(Agent._kb_miss(zero), True)

        print("✓ _kb_miss 按未联网口径（web_allowed=False 时忽略 web 步骤）")

    # ---------------- remember 请求级许可 ----------------

    def test_remember_false_skips_long_term_writes(self):
        """remember=False：问答成功但不写长期记忆、不抽实体（文件不变、零额外 LLM 调用）。"""
        with tempfile.TemporaryDirectory() as td:
            llm = ScriptedLLM(
                [PLAN_KB_JSON, REFLECT_NONE_JSON, "最终答案[1]", ENTITY_JSON,   # 第一次 ask（remember=False）
                 PLAN_KB_JSON, REFLECT_NONE_JSON, "第二次答案[1]", ENTITY_JSON],  # 第二次 ask（缺省）
                Config({}),
            )
            agent = _make_agent(
                llm, {"search_knowledge_base": kb_hit_tool()},
                {"data_dir": td, "memory": {"long_term_enabled": True, "entities_enabled": True,
                                            "embedding_backend": "tfidf", "max_episodes": 50},
                 "tools": {"max_retries": 0}},
            )
            a1 = agent.ask("什么是 RAG", session_id="s1", remember=False)
            self.assertTrue(a1.ok, f"第一次 ask 不应失败: {a1.error}")
            self.assertEqual(llm.calls, 3, f"remember=False 不应触发实体抽取，实际 {llm.calls}")
            self.assertFalse((Path(td) / "memory" / "episodes.json").exists(), "不应写 episodes")
            self.assertFalse((Path(td) / "memory" / "entities.json").exists(), "不应写 entities")
            self.assertEqual(agent.long_memory.size, 0)
            self.assertEqual(agent.memory.count, 1, "会话内记忆不受 remember 影响")

            # 缺省（None）→ 按配置（开启）：正常写长期记忆与实体
            # （第二次 ask 有会话历史 → 追问改写 1 次 + 规划/反思/合成 + 实体抽取 = 5 次）
            a2 = agent.ask("再讲讲 RAG", session_id="s1")
            self.assertTrue(a2.ok)
            self.assertEqual(llm.calls, 8, "第二次 ask 应含追问改写与实体抽取（3+5=8）")
            self.assertEqual(agent.long_memory.size, 1, "缺省时按配置写入 1 条 episode")
            self.assertTrue((Path(td) / "memory" / "entities.json").exists())

        print("✓ remember=false 不写长期记忆/不抽实体；缺省时行为同配置")

    def test_remember_default_off_config_writes_nothing(self):
        """缺省 remember + 默认配置（记忆关）→ 无记忆落盘（行为同配置）。"""
        with tempfile.TemporaryDirectory() as td:
            llm = ScriptedLLM([PLAN_KB_JSON, REFLECT_NONE_JSON, "答案[1]"], Config({}))
            agent = _make_agent(
                llm, {"search_knowledge_base": kb_hit_tool()},
                {"data_dir": td, "memory": {"embedding_backend": "tfidf"}, "tools": {"max_retries": 0}},
            )
            answer = agent.ask("什么是 RAG", session_id="s1")
            self.assertTrue(answer.ok)
            self.assertEqual(llm.calls, 3, "默认配置下不应有实体抽取调用")
            self.assertIsNone(agent.long_memory, "默认配置不建长期记忆库")
            self.assertFalse((Path(td) / "memory" / "episodes.json").exists())

        print("✓ remember 缺省 + 默认配置（记忆关）→ 零记忆写入")

    # ---------------- 状态披露：metrics 外发标记（ACC-U3-01） ----------------

    def test_answer_payload_egress_flags(self):
        """_answer_payload metrics 增 degraded/web_used 布尔键，旧键齐全、口径正确。"""
        handler = object.__new__(Handler)

        def payload(steps) -> dict:
            answer = Answer(question="q", final_answer="答[1]", sources=["kb://a.md"], steps=steps)
            return handler._answer_payload(answer)

        # 任一步 degraded=True → degraded True（ACC-U3-01）
        degraded_kb = StepResult(step_id=1, action="search_knowledge_base",
                                 output={"text": "[降级] x", "sources": ["https://e.com"]}, degraded=True)
        metrics = payload([degraded_kb])["metrics"]
        self.assertIs(metrics["degraded"], True)
        self.assertIs(metrics["web_used"], False, "降级走 web_search 工具实现，但步骤 action 仍是 KB 检索")

        # ok 的 web_search 步骤 → web_used True；failed 的不算
        web_ok = StepResult(step_id=1, action="web_search", output={"text": "网页", "sources": ["https://e.com"]})
        self.assertIs(payload([web_ok])["metrics"]["web_used"], True)
        web_failed = StepResult(step_id=1, action="web_search", output={"text": "失败"}, ok=False)
        self.assertIs(payload([web_failed])["metrics"]["web_used"], False)
        self.assertIs(payload([web_failed])["metrics"]["degraded"], False)

        # 普通 KB 命中步骤 → 双 False
        kb = StepResult(step_id=1, action="search_knowledge_base",
                        output={"text": "x", "hit_count": 2, "sources": ["kb://a.md"]})
        metrics = payload([kb])["metrics"]
        self.assertIs(metrics["degraded"], False)
        self.assertIs(metrics["web_used"], False)

        # 旧键齐全
        for key in ("latency_s", "llm_calls", "prompt_tokens", "completion_tokens",
                    "cost_yuan", "citations_valid", "citations_invalid"):
            self.assertIn(key, metrics, f"旧 metrics 键 {key} 不应缺失")

        # 持久化步骤 dict（任务记录路径）同口径
        flags = _egress_flags([{"step_id": 1, "action": "search_knowledge_base", "ok": True, "degraded": True}])
        self.assertEqual(flags, {"degraded": True, "web_used": False})

        print("✓ metrics.degraded/web_used 口径正确、旧键齐全、内存与持久化步骤同口径")

    # ---------------- API 透传（/api/ask 与 /api/tasks） ----------------

    def _bare_handler(self, path: str, payload: dict) -> tuple[Handler, io.BytesIO]:
        """裸 Handler + 常规桩：headers/rfile/wfile/读体（不经 socket）。"""
        handler = object.__new__(Handler)
        handler.path = path
        handler.headers = {"Host": "127.0.0.1:8787"}
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"POST {path} HTTP/1.1"  # log_request 需要
        handler.client_address = ("127.0.0.1", 0)
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler._read_payload = lambda: payload  # instance 属性遮蔽类方法
        return handler, handler.wfile

    @staticmethod
    def _response_body(wfile: io.BytesIO) -> tuple[int, dict]:
        raw = wfile.getvalue()
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n")[0].split()[1])
        return status, json.loads(body)

    def test_api_ask_passthrough_and_metrics(self):
        """/api/ask 透传 remember/allow_web 到 agent.ask，响应 metrics 带外发标记（ACC-U3-01）。"""
        captured: dict = {}

        def fake_ask(question, verbose=False, session_id="", remember=None, allow_web=None):
            captured.update(question=question, session_id=session_id, remember=remember, allow_web=allow_web)
            step = StepResult(step_id=1, action="search_knowledge_base",
                              output={"text": "[降级] 网页", "sources": ["https://e.com"]}, degraded=True)
            return Answer(question=question, final_answer="答[1]", sources=["https://e.com"], steps=[step])

        store_stub = SimpleNamespace(
            active_workspace=lambda: {"id": "w1"},
            session_belongs_to=lambda sid, wid: True,
            memory=lambda sid: SessionMemory(),
            add_message=lambda *a, **k: 1,
            set_summary=lambda *a: None,
            record_query=lambda *a: None,
        )
        handler, wfile = self._bare_handler("/api/ask", {
            "question": "什么是 RAG", "session_id": "s1", "allow_web": False, "remember": True,
        })
        handler.agent = SimpleNamespace(config=SimpleNamespace(llm_api_key="sk-test"), ask=fake_ask)
        handler.store = store_stub

        handler._do_post()

        self.assertEqual(captured, {"question": "什么是 RAG", "session_id": "s1",
                                    "remember": True, "allow_web": False}, "新字段应原样透传到 agent.ask")
        status, body = self._response_body(wfile)
        self.assertEqual(status, 200)
        self.assertIs(body["metrics"]["degraded"], True, "降级步骤 → metrics.degraded==true")
        self.assertIs(body["metrics"]["web_used"], False)
        self.assertIn("citations_valid", body["metrics"], "旧键不应缺失")

        # 非法值 → 400 且不调用 ask
        handler2, wfile2 = self._bare_handler("/api/ask", {"question": "q", "allow_web": "maybe"})
        handler2.agent = SimpleNamespace(config=SimpleNamespace(llm_api_key="sk-test"), ask=fake_ask)
        handler2.store = store_stub
        handler2._do_post()
        status2, body2 = self._response_body(wfile2)
        self.assertEqual(status2, 400)
        self.assertIn("allow_web", body2["error"])

        print("✓ /api/ask 透传 remember/allow_web（非法值 400），metrics 带外发标记")

    def test_api_tasks_passthrough(self):
        """/api/tasks 透传 remember/allow_web 到 TaskRunner.submit（字符串布尔归一化）。"""
        captured: dict = {}

        def fake_submit(session_id, workspace_id, question, remember=None, allow_web=None):
            captured.update(session_id=session_id, workspace_id=workspace_id,
                            question=question, remember=remember, allow_web=allow_web)
            return "abc123456789"

        handler, wfile = self._bare_handler("/api/tasks", {
            "question": "长任务", "session_id": "s1", "allow_web": "false", "remember": "true",
        })
        handler.agent = SimpleNamespace(config=SimpleNamespace(llm_api_key="sk-test"))
        handler.store = SimpleNamespace(
            active_workspace=lambda: {"id": "w1"},
            session_belongs_to=lambda sid, wid: True,
        )
        handler.task_runner = SimpleNamespace(submit=fake_submit)

        handler._do_post()

        self.assertEqual(captured, {"session_id": "s1", "workspace_id": "w1", "question": "长任务",
                                    "remember": True, "allow_web": False})
        status, body = self._response_body(wfile)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        # 非法值 → 400，submit 不被调用
        captured.clear()
        handler2, wfile2 = self._bare_handler("/api/tasks", {"question": "q", "remember": 3})
        handler2.agent = SimpleNamespace(config=SimpleNamespace(llm_api_key="sk-test"))
        handler2.store = SimpleNamespace(
            active_workspace=lambda: {"id": "w1"},
            session_belongs_to=lambda sid, wid: True,
        )
        handler2.task_runner = SimpleNamespace(submit=fake_submit)
        handler2._do_post()
        status2, body2 = self._response_body(wfile2)
        self.assertEqual(status2, 400)
        self.assertIn("remember", body2["error"])
        self.assertNotIn("question", captured, "非法请求不应触发 submit")

        print("✓ /api/tasks 透传 remember/allow_web（字符串布尔归一化、非法值 400）")

    def test_task_runner_executes_with_request_flags(self):
        """TaskRunner.submit 透传 → _execute 用 allow_web 过滤规划清单并门控降级。"""
        with tempfile.TemporaryDirectory() as td:
            captured: dict = {}

            def fake_plan_only(question, session_id="", allow_web=None):
                captured["allow_web"] = allow_web
                plan = Plan(plan_summary="检索一步",
                            steps=[PlanStep(action="search_knowledge_base", input={"query": "RAG"}, step_id=1)])
                return plan, question, ""

            def fake_finish(question, plan, steps, history_text=""):
                return "任务答案[1]", ["kb://a.md"], SimpleNamespace(valid_count=1, invalid_count=0)

            # 配置 kb_fallback_web=true，请求 allow_web=False → 降级仍应被门控
            stub_agent = SimpleNamespace(
                config=Config({"tools": {"kb_fallback_web": True, "max_retries": 0}}),
                tools={"search_knowledge_base": broken_kb_tool()},
                _ctx=ToolContext(),
                memory=SessionMemory(),
                prompt_tokens=0, completion_tokens=0, estimated_cost=0.0,
                plan_only=fake_plan_only,
                finish_task=fake_finish,
            )
            stub_agent.resolve_allow_web = MethodType(Agent.resolve_allow_web, stub_agent)

            store = TaskStore(Path(td) / "tasks")
            runner = TaskRunner(store, threading.Lock(), agent_provider=lambda: stub_agent,
                                memory_provider=lambda sid: SessionMemory())
            runner._queue = queue.Queue()  # 摘除异步消费（daemon 空转），同步直测 _execute
            tid = runner.submit("s1", "w1", "任务问题", remember=True, allow_web=False)
            self.assertEqual(runner._egress[tid], {"remember": True, "allow_web": False})

            record = store.get(tid)
            runner._execute(record, threading.Event(), threading.Event())
            self.assertEqual(captured["allow_web"], False, "plan_only 应收到 allow_web=False")
            self.assertEqual(record.status, "completed")
            step = record.steps[0]
            self.assertFalse(step["ok"], "KB 工具必抛错，步骤应失败")
            self.assertFalse(step["degraded"], "allow_web=False 应门控降级（配置 true 也不例外）")
            self.assertEqual(record.error, "", "工具失败不中断任务（任务级 error 仍为空）")

        print("✓ 任务路径：submit 透传的 allow_web 生效（规划清单 + 降级门控）")

    # ---------------- WebUI 静态文案（ACC-U3-02 / 设置页说明 / 清空确认） ----------------

    def test_webui_static_disclosure(self):
        """前端源码（Next.js，W6 迁移自 webui.html）含答案状态行、设置页数据外发说明与清空会话确认文案。"""
        frontend = REPO / "frontend"
        answer_card = (frontend / "components" / "AnswerCard.tsx").read_text(encoding="utf-8")
        settings = (frontend / "components" / "SettingsModal.tsx").read_text(encoding="utf-8")
        page_src = (frontend / "app" / "page.tsx").read_text(encoding="utf-8")
        self.assertIn("本回答来自 Web 搜索降级", answer_card, "ACC-U3-02 降级状态行")
        self.assertIn("本回答使用了 Web 搜索", answer_card, "web_used 状态行")
        self.assertIn("开启后问题可能发送到互联网", settings, "联网降级说明文案")
        self.assertIn("记忆数据保存到本机 data/", settings, "记忆落盘说明文案")
        self.assertIn("可同时删除该会话长期记忆", page_src, "清空会话确认文案")
        # 状态行渲染条件（degraded 优先、web_used 次之、皆 false 不渲染——Next 版三元条件）
        self.assertIn("data.metrics?.degraded ?", answer_card, "degraded 渲染条件")
        self.assertIn("data.metrics?.web_used ?", answer_card, "web_used 渲染条件")

        print("✓ 前端源码静态断言：状态行与说明/确认文案齐备，渲染条件正确")

    # ---------------- parse_opt_bool 单元 ----------------

    def test_parse_opt_bool(self):
        """可选布尔解析：缺省 None、bool 原样、字符串归一化、非法值报错。"""
        cases = [
            (None, None, None), (True, True, None), (False, False, None),
            ("true", True, None), ("FALSE", False, None), (" 1 ", True, None), ("no", False, None),
            ("maybe", None, "须为布尔值（true/false）"), (3, None, "须为布尔值（true/false）"),
        ]
        for value, expected, err in cases:
            got, got_err = parse_opt_bool(value)
            self.assertEqual(got, expected, f"parse_opt_bool({value!r}) 值不符")
            self.assertEqual(got_err, err, f"parse_opt_bool({value!r}) 错误消息不符")

        print("✓ parse_opt_bool：None/bool/字符串归一化/非法值报错")


class WebSettingsEgressSwitchTests(unittest.TestCase):
    """G3 遗留项：设置面板三开关接入（CONFIG_FIELDS 白名单 + _config_view 透出 + webui 控件）。

    全部离线：CONFIG_PATH/RUNTIME_PATH/ENV_PATH 在 setUp monkeypatch 到临时目录，
    绝不读写用户 data/runtime.json 与 .env；_save_config/_config_view 用裸 Handler
    （object.__new__）直测，agent 为最小 SimpleNamespace 桩（同 test_config_validation 用法）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {name: getattr(config_mod, name) for name in ("CONFIG_PATH", "RUNTIME_PATH", "ENV_PATH")}
        config_mod.CONFIG_PATH = REPO / "config.yaml"  # 只读仓库配置
        config_mod.RUNTIME_PATH = self.tmp / "runtime.json"
        config_mod.ENV_PATH = self.tmp / ".env"

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(config_mod, name, value)
        self._tmp.cleanup()

    def bare_handler(self, raw: dict | None = None) -> tuple[Handler, Config]:
        """裸 Handler + 最小 fake agent（config/reconfigure 桩），保存路径直测用。"""
        handler = object.__new__(Handler)
        cfg = Config(raw or {})
        handler.agent = SimpleNamespace(config=cfg, reconfigure=lambda: None)
        return handler, cfg

    # ---------------- _config_view 透出 ----------------

    def test_config_view_exposes_three_switches(self):
        """_config_view 新增 tools/memory 节：三键值与 Config property 一致，既有节不回归。"""
        handler, cfg = self.bare_handler({
            "tools": {"kb_fallback_web": True},
            "memory": {"long_term_enabled": True, "entities_enabled": False},
        })
        view = handler._config_view()
        self.assertEqual(view["tools"], {"kb_fallback_web": True})
        self.assertEqual(view["memory"], {"long_term_enabled": True, "entities_enabled": False})
        self.assertIs(view["tools"]["kb_fallback_web"], cfg.kb_fallback_web)
        self.assertIs(view["memory"]["long_term_enabled"], cfg.memory_long_term_enabled)
        self.assertIs(view["memory"]["entities_enabled"], cfg.memory_entities_enabled)
        # 既有节与键不回归（新键追加，不改既有键）
        for section in ("llm", "retrieval", "vision"):
            self.assertIn(section, view)
        self.assertIn("api_key_masked", view["llm"])
        self.assertIn("multi_query", view["retrieval"])
        self.assertIn("configured", view["vision"])

        print("✓ _config_view 透出 tools/memory 三键且与 config property 一致，既有节不变")

    # ---------------- 保存路径：/api/config 白名单放行三键 ----------------

    def test_save_config_kb_fallback_web_true_persists_and_hot_updates(self):
        """{"tools":{"kb_fallback_web":true}} → 200、临时 runtime.json 落盘 true、agent.config 热更新。"""
        handler, cfg = self.bare_handler({"retrieval": {"top_k": 6}})
        status, body = handler._save_config({"tools": {"kb_fallback_web": True}})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        saved = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertIs(saved["tools"]["kb_fallback_web"], True, "应落盘为真 bool")
        self.assertIs(cfg.kb_fallback_web, True, "property 应读到热更新后的内存配置")
        self.assertIs(body["config"]["tools"]["kb_fallback_web"], True, "响应 config 应回读新值")

        print("✓ 保存 kb_fallback_web=true：200 落盘 true 且 agent.config 热更新")

    def test_save_config_bool_string_normalization_and_rejection(self):
        """字符串 "yes" → 200 且归一化为 true 落盘；"maybe" → 400（错误含 dotted 路径）不落盘。"""
        handler, cfg = self.bare_handler()
        status, body = handler._save_config({"tools": {"kb_fallback_web": "maybe"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "配置校验失败")
        self.assertTrue(body["errors"], "400 响应应含 errors 列表")
        self.assertIn("tools.kb_fallback_web", body["errors"][0], "错误消息应含完整 dotted 路径")
        self.assertFalse(config_mod.RUNTIME_PATH.exists(), "校验失败不得写 runtime.json")
        self.assertIs(cfg.kb_fallback_web, False, "校验失败不得热更新内存配置")

        status2, _body2 = handler._save_config({"memory": {"long_term_enabled": "yes"}})
        self.assertEqual(status2, 200)
        saved = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertIs(saved["memory"]["long_term_enabled"], True, "落盘前应归一化为真 bool")
        self.assertIs(cfg.memory_long_term_enabled, True)

        print("✓ 布尔字符串归一化落盘（yes→true）；非法值 maybe 400 且不落盘不热更新")

    def test_save_config_entities_false_persists(self):
        """{"memory":{"entities_enabled":false}} → 200、落盘 false、property False（显式关闭不丢）。"""
        handler, cfg = self.bare_handler({"memory": {"entities_enabled": True}})
        status, body = handler._save_config({"memory": {"entities_enabled": False}})
        self.assertEqual(status, 200)
        saved = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertIs(saved["memory"]["entities_enabled"], False)
        self.assertIs(cfg.memory_entities_enabled, False)
        self.assertIs(body["config"]["memory"]["entities_enabled"], False)

        print("✓ 保存 entities_enabled=false：200 落盘 false 且热更新")

    def test_save_config_unknown_tools_keys_dropped_and_noop_kept(self):
        """回归：tools 节未知键仍被白名单丢弃；不含三键的保存请求行为不变。"""
        handler, cfg = self.bare_handler()
        status, _body = handler._save_config({"tools": {"kb_fallback_web": True, "web_search_timeout": 99}})
        self.assertEqual(status, 200)
        saved = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertEqual(sorted(saved["tools"]), ["kb_fallback_web"], "未知键应被白名单丢弃")
        self.assertIs(cfg.kb_fallback_web, True)

        # 不传这些键的保存请求行为不变（清空临时 runtime 后单独保存 retrieval）
        config_mod.RUNTIME_PATH.unlink()
        handler2, _cfg2 = self.bare_handler()
        status2, _body2 = handler2._save_config({"retrieval": {"top_k": 9}})
        self.assertEqual(status2, 200)
        saved2 = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("tools", saved2, "未传 tools 节时不应出现在落盘结果")
        self.assertNotIn("memory", saved2, "未传 memory 节时不应出现在落盘结果")
        self.assertEqual(saved2["retrieval"]["top_k"], 9)

        print("✓ tools 未知键被丢弃；不含三键的保存请求行为不变")

    # ---------------- WebUI 静态断言（控件/payload/hint） ----------------

    def test_webui_static_egress_switches(self):
        """前端源码静态断言（W6 迁移自 webui.html）：三个开关存在、保存 payload 含 tools/memory 节、既有 hint 未破坏。"""
        settings = (REPO / "frontend" / "components" / "SettingsModal.tsx").read_text(encoding="utf-8")
        for state_name in ("kbFallbackWeb", "memLong", "memEntities"):
            self.assertIn(f"const [{state_name}, set{state_name[0].upper()}{state_name[1:]}]", settings, f"开关状态 {state_name} 应存在")
        # 保存 payload 字段（布尔经 'true' 字符串比较）
        self.assertIn('kb_fallback_web: kbFallbackWeb === "true"', settings)
        self.assertIn('long_term_enabled: memLong === "true"', settings)
        self.assertIn('entities_enabled: memEntities === "true"', settings)
        # 加载回填（读 cfg.tools/cfg.memory）
        self.assertIn("(cfg.tools || {}).kb_fallback_web", settings)
        self.assertIn("(cfg.memory || {}).long_term_enabled", settings)
        self.assertIn("(cfg.memory || {}).entities_enabled", settings)
        # 三处控件 label
        self.assertIn("联网降级（KB 未命中时自动搜索）", settings)
        self.assertIn("<label>长期记忆</label>", settings)
        self.assertIn("<label>实体记忆</label>", settings)
        # 既有 G3 披露 hint 未被破坏
        self.assertIn("数据外发与记忆（默认关闭）：联网降级开启后问题可能发送到互联网", settings)
        self.assertIn("长期记忆与实体记忆默认关闭；开启后成功问答会写入本机 data/memory/", settings)

        print("✓ 前端源码静态断言：三开关/payload/回填/label/hint 文案齐备")


if __name__ == "__main__":
    unittest.main()
