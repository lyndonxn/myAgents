"""G11 本地 LLM 离线档位测试（ACC-U11-01..03 + 配置校验 + 准备脚本）。

全部离线（无真实 LLM、无网络）：
- LLM 用 ScriptedLLM/RecordingLLM（子类化 LLMClient 覆写 _chat，按脚本返回）；
- ACC-U11-01 用真实 LLMClient + monkeypatch requests.post（fake OpenAI 形响应）；
- 全链路 ask 用 TfidfHashEmbeddingBackend + 临时 KB 目录（build_index persist=False），
  并 monkeypatch requests.post/get 计数证明零外呼；
- 配置与脚本测试把 CONFIG_PATH/RUNTIME_PATH/ENV_PATH 指向 tempfile 临时目录，
  绝不读写用户 data/runtime.json 与 .env。
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import agent as agent_mod
from agents import config as config_mod
from agents.agent import Agent
from agents.config import Config, validate_config
from agents.llm import ChatResult, LLMClient, LLMError
from agents.memory import SessionMemory
from agents.planner import Plan, PlanStep
from agents.task_runner import TaskRunner
from agents.task_store import TaskStore
from agents.tools import Tool, ToolContext
from agents.web_server import Handler
import scripts.prepare_offline as prepare_offline

REPO = Path(__file__).resolve().parent.parent

PLAN_EMPTY_QUERY_JSON = json.dumps(
    {
        "reasoning": "离线未命中测试规划",
        "plan_summary": "检索一步",
        "steps": [{"action": "search_knowledge_base", "input": {"query": ""}, "purpose": ""}],
    },
    ensure_ascii=False,
)
REFLECT_NONE_JSON = json.dumps({"need_more": False, "reasoning": "信息足够", "steps": []}, ensure_ascii=False)

OPENAI_OK_PAYLOAD = {
    "choices": [{"message": {"content": "你好，本地模型。"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 5},
}


class ScriptedLLM(LLMClient):
    """离线假 LLM：按脚本顺序返回文本，记录每次调用、messages 与 kwargs（不经网络）。"""

    def __init__(self, texts: list[str], config: Config):
        self.config = config
        self._texts = list(texts)
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.kwargs_seen: list[dict] = []

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        self.kwargs_seen.append(dict(kwargs))
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _FakeResponse:
    """requests.post 的 fake OpenAI 形响应。"""

    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def broken_kb_tool() -> Tool:
    """必抛错的 KB 工具（验证降级门控是否被钳制关闭）。"""
    def broken(ctx, **kwargs):
        raise RuntimeError("知识库索引损坏")

    return Tool(
        name="search_knowledge_base", description="测试 KB 工具",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        func=broken,
    )


# 离线档位 E2E 用的临时 KB 文档与配置（TF-IDF 后端 + 关精排，确定性）
KB_DOC = (
    "# RAG\n\n"
    "检索增强生成（RAG）是一种先检索后生成的问答架构。系统从知识库中召回与问题相关的片段，"
    "再把片段交给大语言模型生成答案，从而减少幻觉并支持来源引用。"
    "在企业知识管理场景中，RAG 通常与混合检索、重排序和引用校验配合使用。\n"
)
E2E_RAW = {
    "kb_path": "",  # setUp 时填临时目录
    "embedding": {"backend": "tfidf", "hash_dim": 256},
    "retrieval": {"rerank": "off", "top_k": 3},
    "chunking": {"min_chars": 20, "max_chars": 600, "overlap": 20, "leaf_max_chars": 120, "leaf_min_chars": 24},
    "llm": {"mode": "local", "base_url": "http://127.0.0.1:11434/v1", "chat_model": "qwen2.5:7b"},
    "tools": {"kb_fallback_web": True, "web_search_enabled": True, "max_retries": 0},
}


class OfflineProfileTests(unittest.TestCase):
    """G11：空 Key 本地端点 / 非深航模型 JSON 约束 / 离线钳制 / E2E / 准备脚本。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {name: getattr(config_mod, name) for name in ("CONFIG_PATH", "RUNTIME_PATH", "ENV_PATH")}
        config_mod.CONFIG_PATH = REPO / "config.yaml"  # 只读仓库配置
        config_mod.RUNTIME_PATH = self.tmp / "runtime.json"
        config_mod.ENV_PATH = self.tmp / ".env"
        # DEEPSEEK_API_KEY 环境变量与本进程级提示旗标逐用例隔离
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("DEEPSEEK_API_KEY", None)
        agent_mod._offline_web_hint_shown = False

    def tearDown(self) -> None:
        self._env.stop()
        for name, value in self._saved.items():
            setattr(config_mod, name, value)
        agent_mod._offline_web_hint_shown = False
        self._tmp.cleanup()

    # ---------------- 默认值与配置校验 ----------------

    def test_01_mode_defaults_to_cloud_no_behavior_change(self):
        """默认 mode=cloud：仓库 config.yaml、Config 兜底、Config({}) 语义全部零变化。"""
        raw = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(raw["llm"]["mode"], "cloud", "config.yaml llm.mode 默认应为 cloud")
        self.assertEqual(Config({}).llm_mode, "cloud", "property 兜底默认应为 cloud")
        self.assertEqual(Config({"llm": {"mode": "cloud"}}).llm_mode, "cloud")

        print("✓ G11 默认 cloud：config.yaml 与 Config 兜底一致，既有行为零变化")

    def test_02_validate_llm_mode_enum(self):
        """枚举：llm.mode="hybrid" 报错；大小写不敏感通过并归一化小写；非字符串报类型错。"""
        errors = validate_config({"llm": {"mode": "hybrid"}})
        self.assertEqual(len(errors), 1, f"hybrid 应报 1 条错误: {errors}")
        self.assertIn("llm.mode", errors[0])
        self.assertIn("cloud/local", errors[0])
        self.assertEqual(validate_config({"llm": {"mode": "LOCAL"}}), [], "大小写不敏感应通过")
        raw = {"llm": {"mode": "LOCAL"}}
        config_mod.normalize_config(raw)
        self.assertEqual(raw["llm"]["mode"], "local", "归一化应转小写")
        self.assertEqual(len(validate_config({"llm": {"mode": 1}})), 1, "非字符串应报类型错误")

        print("✓ llm.mode 枚举校验：非法值报错、归一化小写、类型错误")

    def _bare_handler(self) -> tuple[Handler, Config]:
        """裸 Handler（跳过 socket）+ 最小 agent 桩（config/reconfigure）。"""
        handler = object.__new__(Handler)
        cfg = Config({
            "kb_path": "knowledge_base",
            "llm": {"base_url": "https://api.deepseek.com", "chat_model": "deepseek-chat",
                    "temperature": 0.3, "max_tokens": 2048},
            "retrieval": {"top_k": 6},
        })
        handler.agent = SimpleNamespace(config=cfg, reconfigure=lambda: None)
        return handler, cfg

    def test_03_save_config_mode_local_persists_and_whitelist(self):
        """保存路径：mode=local → 200 且临时 runtime.json 生效、内存热更新；mode=hybrid → 400；白名单外键仍被丢弃。"""
        handler, cfg = self._bare_handler()
        status, body = handler._save_config({"llm": {"mode": "local"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        saved = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["llm"]["mode"], "local", "runtime.json 应持久化 mode=local")
        self.assertEqual(cfg.get("llm.mode"), "local", "内存配置应热更新")
        self.assertEqual(cfg.llm_mode, "local")
        # 白名单外键（max_retries / api_key_env）不得落盘；合法键正常保存
        status2, _body2 = handler._save_config({"llm": {"mode": "local", "max_retries": 9, "api_key_env": "x"}})
        self.assertEqual(status2, 200)
        saved2 = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved2["llm"], {"mode": "local"}, "白名单外的 llm 键应被丢弃")
        # 非法枚举值经保存路径 → 400 + errors，不落盘
        status3, body3 = handler._save_config({"llm": {"mode": "hybrid"}})
        self.assertEqual(status3, 400)
        self.assertIn("llm.mode", body3["errors"][0])

        print("✓ /api/config：mode=local 保存生效；hybrid 400；白名单外键仍拒绝")

    # ---------------- ACC-U11-01：本地端点空 Key ----------------

    def test_04_local_empty_key_constructs_and_chats_without_auth(self):
        """ACC-U11-01：mode=local + 空 Key → 构造不抛；chat 正常且请求头不带 Authorization。"""
        captured: dict = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, headers=headers, payload=json)
            return _FakeResponse(OPENAI_OK_PAYLOAD)

        with mock.patch("agents.llm.requests.post", side_effect=fake_post):
            client = LLMClient(Config({
                "llm": {"mode": "local", "base_url": "http://127.0.0.1:11434/v1", "chat_model": "qwen2.5:7b"},
            }))
            result = client.chat([{"role": "user", "content": "你好"}])

        self.assertEqual(result.text, "你好，本地模型。", "fake 本地服务响应应正常解析")
        self.assertIn("/chat/completions", captured["url"])
        self.assertNotIn("Authorization", captured["headers"], "空 Key 请求头不得带 Authorization")
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["payload"]["model"], "qwen2.5:7b")

        print("✓ ACC-U11-01：mode=local 空 Key 构造/请求正常，无 Authorization 头")

    def test_05_cloud_without_key_still_raises(self):
        """兼容红线：mode=cloud 无 Key 构造仍抛 LLMError（提示信息不变）。"""
        with self.assertRaises(LLMError) as ctx:
            LLMClient(Config({"llm": {"mode": "cloud"}}))
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))

        print("✓ mode=cloud 无 Key 仍抛 LLMError（行为零变化）")

    def test_06_local_with_key_keeps_auth_header(self):
        """mode=local 配了 Key：请求头照带 Authorization（Ollama 兼容层可选鉴权）。"""
        captured: dict = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(headers=headers)
            return _FakeResponse(OPENAI_OK_PAYLOAD)

        with mock.patch("agents.llm.requests.post", side_effect=fake_post):
            client = LLMClient(Config({
                "llm": {"mode": "local", "api_key": "sk-local-123",
                        "base_url": "http://127.0.0.1:11434/v1", "chat_model": "qwen2.5:7b"},
            }))
            client.chat([{"role": "user", "content": "你好"}])

        self.assertEqual(captured["headers"].get("Authorization"), "Bearer sk-local-123")

        print("✓ mode=local 有 Key 时 Authorization 照带")

    # ---------------- 非 deepseek 模型 JSON 约束（chat_json） ----------------

    def test_07_chat_json_non_deepseek_appends_constraint_on_copy(self):
        """非 deepseek 模型：请求消息为原 messages 副本 + 末尾 JSON 约束 system 消息；调用方列表不被修改；无 response_format。"""
        llm = ScriptedLLM(['{"a": 1}'], Config({"llm": {"chat_model": "qwen2.5:7b"}}))
        original = [{"role": "user", "content": "给我 JSON"}]
        snapshot = [dict(m) for m in original]

        obj = llm.chat_json(original)

        self.assertEqual(obj, {"a": 1})
        self.assertEqual(llm.calls, 1, "合法 JSON 不应触发修复轮")
        sent = llm.seen[0]
        self.assertEqual(len(sent), 2, "请求消息应为原 1 条 + 追加 1 条约束")
        self.assertEqual(sent[0], snapshot[0], "原消息应原样在前")
        self.assertEqual(sent[-1]["role"], "system")
        self.assertIn("JSON", sent[-1]["content"], "末尾应追加 JSON 强约束提示")
        self.assertEqual(original, snapshot, "调用方 messages 列表绝不能被修改")

        print("✓ chat_json 非 deepseek：副本末尾追加 JSON 约束，原列表不变")

    def test_08_chat_json_repair_rounds_keep_constraint_and_deepseek_regression(self):
        """非 deepseek 修复轮仍生效（坏→好）；约束随修复轮保留；deepseek 模型仍走 response_format（回归）。"""
        llm = ScriptedLLM(["这不是 JSON", '{"ok": true}'], Config({"llm": {"chat_model": "qwen2.5:7b"}}))
        obj = llm.chat_json([{"role": "user", "content": "给我 JSON"}])
        self.assertEqual(obj, {"ok": True}, "第一次坏 JSON、第二次好 → 修复成功")
        self.assertEqual(llm.calls, 2)
        roles = [m["role"] for m in llm.seen[1]]
        self.assertEqual(roles, ["user", "system", "assistant", "user"],
                         "修复轮请求 = 原消息 + JSON 约束 + 坏输出 + 修复指令")
        self.assertEqual(llm.seen[1][2]["content"], "这不是 JSON")
        self.assertIn("无法解析为 JSON", llm.seen[1][3]["content"])

        # 回归：deepseek 模型不追加提示、仍传 response_format
        llm2 = ScriptedLLM(['{"ok": 1}'], Config({}))
        llm2.chat_json([{"role": "user", "content": "x"}])
        self.assertEqual(llm2.seen[0], [{"role": "user", "content": "x"}], "deepseek 不应追加约束消息")
        self.assertEqual(llm2.kwargs_seen[0]["response_format"], {"type": "json_object"},
                         "deepseek 模型仍应传 response_format")

        print("✓ chat_json：非 deepseek 修复轮带约束；deepseek 仍走 response_format")

    # ---------------- ACC-U11-02：离线档位钳制联网 ----------------

    def _offline_agent(self, texts: list[str], raw: dict | None = None) -> Agent:
        merged = {"llm": {"mode": "local"}, "tools": {"kb_fallback_web": True}}
        merged.update(raw or {})
        cfg = Config(merged)
        return Agent(cfg, llm=ScriptedLLM(texts, cfg), lazy_index=True)

    def test_09_resolve_allow_web_always_false_in_local_mode(self):
        """ACC-U11-02：local 档位 resolve_allow_web 恒 False（True/None/False 全钳制，配置 kb_fallback_web=true 也不例外）；cloud 档位语义不变。"""
        agent = self._offline_agent([])
        self.assertIs(agent.resolve_allow_web(True), False, "allow_web=true 必须被钳制")
        self.assertIs(agent.resolve_allow_web(None), False, "None（按配置 true）也必须被钳制")
        self.assertIs(agent.resolve_allow_web(False), False)

        # cloud 档位回归：None→配置、False 强禁、True 强许
        cloud = Agent(Config({"tools": {"kb_fallback_web": True}}), llm=ScriptedLLM([], Config({})), lazy_index=True)
        self.assertIs(cloud.resolve_allow_web(None), True)
        self.assertIs(cloud.resolve_allow_web(False), False)
        self.assertIs(cloud.resolve_allow_web(True), True)

        print("✓ ACC-U11-02：local 档位 allow_web 恒 False；cloud 档位语义不变")

    def test_10_offline_clamp_logs_once_per_process(self):
        """钳制联网请求时 LOG.info「离线档位」恰好一次/进程；未触发外发的调用不提示。"""
        agent = self._offline_agent([])
        handler = _CaptureHandler()
        logger = logging.getLogger("agent")
        logger.addHandler(handler)
        try:
            self.assertIs(agent.resolve_allow_web(False), False)
            self.assertEqual([m for m in handler.messages if "离线档位" in m], [], "未请求联网不应提示")
            agent.resolve_allow_web(True)
            agent.resolve_allow_web(True)
            agent.resolve_allow_web(None)
        finally:
            logger.removeHandler(handler)
        hinted = [m for m in handler.messages if "离线档位" in m]
        self.assertEqual(len(hinted), 1, f"提示应恰好一次/进程，实际 {hinted}")
        self.assertIn("离线档位忽略联网请求", hinted[0])

        print("✓ 离线钳制提示：一次/进程，不刷屏")

    def test_11_offline_ask_kb_miss_discloses_without_network(self):
        """ACC-U11-02 全链路：local 档位 KB 未命中 → 「未在知识库内」披露路径，web 工具不进规划清单，全程零 requests 外呼。"""
        kb_dir = self.tmp / "kb"
        kb_dir.mkdir()
        (kb_dir / "rag.md").write_text(KB_DOC, encoding="utf-8")
        raw = {**E2E_RAW, "kb_path": str(kb_dir)}
        cfg = Config(raw)
        llm = ScriptedLLM(
            [PLAN_EMPTY_QUERY_JSON, REFLECT_NONE_JSON, "未在知识库内找到相关内容。需要我联网搜索吗？"], cfg
        )
        agent = Agent(cfg, llm=llm, lazy_index=True)
        agent.build_index(persist=False)

        post = mock.MagicMock()
        get = mock.MagicMock()
        with mock.patch("requests.post", post), mock.patch("requests.get", get):
            answer = agent.ask("对比 RAG 与混合检索的优劣，分别说明适用场景")

        self.assertTrue(answer.ok, f"ask 不应失败: {answer.error}")
        self.assertEqual(llm.calls, 3, "完整路径 = 规划 + 反思 + 合成")
        # web 工具不进规划/反思清单（注册表里其实有 web_search：离线钳制剔除，双保险）
        self.assertIn("web_search", agent.tools, "前提：web_search 已注册（由 web_search_enabled 控制）")
        self.assertNotIn("web_search", llm.seen[0][1]["content"], "规划清单不应含 web_search")
        self.assertNotIn("web_search", llm.seen[1][1]["content"], "反思清单不应含 web_search")
        # KB 未命中 → 未命中披露路径（合成提示注入），降级关闭
        step = answer.steps[0]
        self.assertEqual(step.output["hit_count"], 0, "空查询对真实 TF-IDF 检索应零命中")
        self.assertFalse(step.degraded, "离线档位降级门控应关闭")
        self.assertIn("未命中任何相关片段", llm.seen[2][1]["content"], "未命中应注入披露提示")
        self.assertIn("未在知识库内", answer.final_answer)
        # 全程零网络外呼
        self.assertEqual(post.call_count, 0, "不得有任何 requests.post 外呼")
        self.assertEqual(get.call_count, 0, "不得有任何 requests.get 外呼")

        print("✓ ACC-U11-02 全链路：KB 未命中走披露路径、web 剔除出规划清单、零网络外呼")

    def test_12_task_runner_offline_clamps_allow_web(self):
        """任务路径：local 档位 submit allow_web=True → resolve_allow_web 钳制为 False、降级门控关闭。"""
        with tempfile.TemporaryDirectory() as td:
            def fake_plan_only(question, session_id="", allow_web=None):
                plan = Plan(plan_summary="检索一步",
                            steps=[PlanStep(action="search_knowledge_base", input={"query": "RAG"}, step_id=1)])
                return plan, question, ""

            def fake_finish(question, plan, steps, history_text=""):
                return "任务答案[1]", ["kb://a.md"], SimpleNamespace(valid_count=1, invalid_count=0)

            stub_agent = SimpleNamespace(
                config=Config({"llm": {"mode": "local"}, "tools": {"kb_fallback_web": True, "max_retries": 0}}),
                tools={"search_knowledge_base": broken_kb_tool()},
                _ctx=ToolContext(),
                memory=SessionMemory(),
                prompt_tokens=0, completion_tokens=0, estimated_cost=0.0,
                plan_only=fake_plan_only,
                finish_task=fake_finish,
            )
            stub_agent.resolve_allow_web = MethodType(Agent.resolve_allow_web, stub_agent)
            self.assertIs(stub_agent.resolve_allow_web(True), False, "任务路径复用同一钳制单点")

            store = TaskStore(Path(td) / "tasks")
            runner = TaskRunner(store, threading.Lock(), agent_provider=lambda: stub_agent,
                                memory_provider=lambda sid: SessionMemory())
            runner._queue = queue.Queue()  # 摘除异步消费，同步直测 _execute
            tid = runner.submit("s1", "w1", "任务问题", remember=True, allow_web=True)
            record = store.get(tid)
            runner._execute(record, threading.Event(), threading.Event())

            self.assertEqual(record.status, "completed")
            step = record.steps[0]
            self.assertFalse(step["ok"], "KB 工具必抛错，步骤应失败")
            self.assertFalse(step["degraded"], "离线档位：allow_web=True 也必须钳制降级（配置 true 也不例外）")

        print("✓ 任务路径：local 档位 allow_web=True 被钳制，降级门控关闭")

    # ---------------- ACC-U11-03：离线 E2E ----------------

    def test_13_offline_e2e_hit_with_citations_zero_network(self):
        """ACC-U11-03：mode=local + ScriptedLLM + TF-IDF 后端 + 临时 KB → build_index(persist=False) + ask 成功，引用/来源正常，零网络。"""
        kb_dir = self.tmp / "kb"
        kb_dir.mkdir()
        (kb_dir / "rag.md").write_text(KB_DOC, encoding="utf-8")
        raw = {**E2E_RAW, "kb_path": str(kb_dir)}
        cfg = Config(raw)
        llm = ScriptedLLM(["RAG 是检索增强生成[1]。"], cfg)
        agent = Agent(cfg, llm=llm, lazy_index=True)
        agent.build_index(persist=False)

        post = mock.MagicMock()
        get = mock.MagicMock()
        with mock.patch("requests.post", post), mock.patch("requests.get", get):
            answer = agent.ask("什么是 RAG")

        self.assertTrue(answer.ok, f"ask 不应失败: {answer.error}")
        self.assertEqual(llm.calls, 1, "快路径：仅合成 1 次 LLM 调用")
        self.assertEqual(answer.steps[0].output["hit_count"], 1, "临时 KB 命中 1 个父块")
        self.assertIn("[1]", answer.final_answer)
        self.assertEqual(answer.citations_valid, 1, "引用 [1] 应合法")
        self.assertEqual(answer.citations_invalid, 0)
        self.assertTrue(answer.sources, "来源非空")
        self.assertIn("rag.md", str(answer.sources[0]), f"来源应指向临时 KB 文件: {answer.sources}")
        self.assertEqual(post.call_count, 0, "E2E 全程零 requests.post")
        self.assertEqual(get.call_count, 0, "E2E 全程零 requests.get")

        print("✓ ACC-U11-03：离线 E2E 全链路绿（构建索引→快路径问答→引用/来源正常→零网络）")

    # ---------------- 一键准备脚本 ----------------

    def _write_script_config(self, mode: str = "local", base_url: str = "http://127.0.0.1:11434/v1",
                             web_enabled: str = "false", backend: str = "tfidf") -> Path:
        """写一份可过校验的临时 config.yaml（--config 指向它，隔离用户配置）。"""
        path = self.tmp / "script_config.yaml"
        path.write_text(
            f'kb_path: "knowledge_base"\n'
            f'data_dir: "data"\n'
            f"embedding:\n  backend: \"{backend}\"\n  hash_dim: 256\n"
            f'llm:\n  mode: "{mode}"\n  base_url: "{base_url}"\n  chat_model: "qwen2.5:7b"\n'
            f"tools:\n  web_search_enabled: {web_enabled}\n",
            encoding="utf-8",
        )
        return path

    def test_14_prepare_check_ready_exits_zero(self):
        """--check：tempfile 配置齐全（local + 本机 base_url + 关 web 搜索 + tfidf）→ 退出 0。"""
        cfg_path = self._write_script_config()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = prepare_offline.main(["--check", "--config", str(cfg_path)])
        self.assertEqual(rc, 0, f"齐全配置应退出 0，输出:\n{buf.getvalue()}")
        out = buf.getvalue()
        self.assertIn("离线档位体检", out)
        self.assertIn("llm.mode=local", out)
        self.assertIn("全部就绪", out)

        print("✓ prepare_offline --check：配置齐全退出 0")

    def test_15_prepare_check_cloud_exits_one_with_hint(self):
        """--check：mode=cloud → 退出 1 且报告含 llm.mode 与中文修复建议。"""
        cfg_path = self._write_script_config(mode="cloud")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = prepare_offline.main(["--check", "--config", str(cfg_path)])
        self.assertEqual(rc, 1, f"mode=cloud 应退出 1，输出:\n{buf.getvalue()}")
        out = buf.getvalue()
        self.assertIn("llm.mode", out)
        self.assertIn("修复建议", out)
        self.assertIn("未就绪", out)

        print("✓ prepare_offline --check：mode=cloud 退出 1 且给修复建议")

    def test_16_prepare_embedding_check_states(self):
        """embedding 检查三态：缓存可加载→就绪；不可加载→失败+安装/预下载提示；tfidf 兜底直接就绪。"""
        cfg_local = Config({"embedding": {"backend": "local", "model": "BAAI/bge-small-zh-v1.5"}})
        with mock.patch.object(prepare_offline, "_load_sentence_transformer", return_value=object()):
            ok, msg = prepare_offline.check_embedding_offline(cfg_local)
        self.assertTrue(ok, "local_files_only 成功 → 就绪")
        self.assertIn("缓存", msg)

        with mock.patch.object(prepare_offline, "_load_sentence_transformer", side_effect=RuntimeError("无缓存")):
            ok, msg = prepare_offline.check_embedding_offline(cfg_local)
        self.assertFalse(ok, "加载失败 → 未就绪")
        self.assertIn("修复建议", msg)
        self.assertIn("tfidf", msg, "失败提示应含 tfidf 兜底建议")
        self.assertIn("sentence-transformers", msg, "失败提示应含安装提示")

        with mock.patch.object(prepare_offline, "_load_sentence_transformer",
                               side_effect=AssertionError("tfidf 不应触发模型加载")):
            ok, _msg = prepare_offline.check_embedding_offline(Config({"embedding": {"backend": "tfidf"}}))
        self.assertTrue(ok, "tfidf 兜底天然离线可用")

        print("✓ embedding 离线检查：三态正确（缓存/缺失提示/tfidf 兜底）")

    def test_17_prepare_guidance_prints_without_download(self):
        """--prepare：打印 ollama pull 指引与依赖检查，退出 0，全程零网络请求。"""
        cfg_path = self._write_script_config()
        post, get = mock.MagicMock(), mock.MagicMock()
        buf = io.StringIO()
        with mock.patch("requests.post", post), mock.patch("requests.get", get), \
                contextlib.redirect_stdout(buf):
            rc = prepare_offline.main(["--prepare", "--config", str(cfg_path)])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("ollama pull", out, "应给出 ollama pull 建议")
        self.assertIn("http://localhost:11434/v1", out, "应给出本地端点配置建议")
        self.assertIn("依赖检查", out)
        self.assertEqual(post.call_count, 0, "--prepare 不得发起任何请求")
        self.assertEqual(get.call_count, 0, "--prepare 不得发起任何请求")

        print("✓ prepare_offline --prepare：只打印指引（ollama pull + 依赖检查），零下载")


if __name__ == "__main__":
    unittest.main()
