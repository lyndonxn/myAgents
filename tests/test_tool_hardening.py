"""S1 工具层强化测试：参数校验、步骤重试、KB→Web 降级、chat_json 修复轮。

全部离线：LLM 用记录调用次数、按脚本返回文本的假客户端（子类化 LLMClient
覆写 _chat，不发真实请求）；工具用闭包实现，覆盖 spec ACC-S1-01..04。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.config import Config
from agents.executor import Executor
from agents.llm import ChatResult, LLMClient, LLMError
from agents.planner import PlanStep
from agents.tools import Tool, ToolContext, ToolValidationError, validate_tool_input


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


def _make_tool(name: str, func, properties: dict, required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=f"测试工具 {name}",
        parameters={"type": "object", "properties": properties, "required": required},
        func=func,
    )


def _make_executor(tools: dict[str, Tool], raw: dict | None = None) -> Executor:
    return Executor(Config(raw or {}), tools, ToolContext())


class ToolHardeningTests(unittest.TestCase):
    """spec ACC-S1-01..04：参数校验 / 重试 / KB→Web 降级 / chat_json 修复轮。"""

    def test_validate_required_missing(self):
        """ACC-S1-01：缺 required 参数 → 步骤失败、error 含"参数校验失败"、attempts==1 不重试。"""
        calls: list[dict] = []

        def fake_kb(ctx, **kwargs):
            calls.append(kwargs)
            return {"text": "不应被调用"}

        kb = _make_tool(
            "search_knowledge_base", fake_kb,
            {"query": {"type": "string"}}, ["query"],
        )
        executor = _make_executor({"search_knowledge_base": kb})
        step = PlanStep(action="search_knowledge_base", input={}, step_id=1)
        result = executor._run_step(1, step)  # 不崩溃即通过

        self.assertFalse(result.ok)
        self.assertIn("参数校验失败", result.error)
        self.assertEqual(result.attempts, 1, f"校验失败不应重试，实际 attempts={result.attempts}")
        self.assertEqual(calls, [], "校验失败的步骤不应真正调用工具")

        # tools 层直接校验：缺 required 抛 ToolValidationError（ValueError 子类）
        with self.assertRaises(ToolValidationError) as cm:
            validate_tool_input(kb, {})
        self.assertIn("query", str(cm.exception))

        print("✓ required 缺失校验（不重试，attempts==1）")

    def test_retry_then_success(self):
        """ACC-S1-02：首次抛 RuntimeError、第二次成功，max_retries=1 → 成功且 attempts==2。"""
        attempts: list[int] = []

        def flaky(ctx, **kwargs):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise RuntimeError("第一次失败")
            return {"text": "重试成功"}

        tool = _make_tool("flaky", flaky, {"query": {"type": "string"}}, ["query"])
        executor = _make_executor({"flaky": tool}, {"tools": {"max_retries": 1}})
        step = PlanStep(action="flaky", input={"query": "q"}, step_id=1)
        result = executor._run_step(1, step)

        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.output, {"text": "重试成功"})
        self.assertEqual(len(attempts), 2)

        # 两次都失败：ok=False，attempts == max_retries + 1
        calls: list[int] = []

        def always_fail(ctx, **kwargs):
            calls.append(1)
            raise RuntimeError("一直失败")

        tool2 = _make_tool("bad", always_fail, {}, [])
        executor2 = _make_executor({"bad": tool2}, {"tools": {"max_retries": 2}})
        result2 = executor2._run_step(1, PlanStep(action="bad", input={}, step_id=1))
        self.assertFalse(result2.ok)
        self.assertEqual(result2.attempts, 3)
        self.assertIn("RuntimeError", result2.error)
        self.assertIn("一直失败", result2.error)

        print("✓ 失败重试（首次失败第二次成功 → attempts==2）")

    def test_kb_fallback_to_web(self):
        """ACC-S1-03：KB 工具必抛错、web_search 可用 → 走 Web 降级、degraded==True、text 含"[降级]"。"""
        def broken_kb(ctx, **kwargs):
            raise RuntimeError("知识库索引损坏")

        def fake_web(ctx, query="", max_results=5):
            return {"text": f"Web 搜索 {query} 的结果", "sources": ["https://example.com/a"]}

        tools = {
            "search_knowledge_base": _make_tool(
                "search_knowledge_base", broken_kb,
                {"query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "maximum": 10}},
                ["query"],
            ),
            "web_search": _make_tool(
                "web_search", fake_web,
                {"query": {"type": "string"}}, ["query"],
            ),
        }
        executor = _make_executor(tools)  # kb_fallback_web 默认 True
        step = PlanStep(action="search_knowledge_base", input={"query": "RAG 是什么"}, step_id=1)
        result = executor._run_step(1, step)

        self.assertTrue(result.ok, f"降级拿到可用结果后应 ok=True，实际 error={result.error}")
        self.assertTrue(result.degraded)
        self.assertTrue(result.output["text"].startswith("[降级] 知识库检索失败，已改用 Web 搜索\n"))
        self.assertIn("Web 搜索 RAG 是什么 的结果", result.output["text"])  # 同一 query 传给了 web_search
        self.assertEqual(result.output["sources"], ["https://example.com/a"])  # sources 正常收集

        print("✓ KB→Web 降级（degraded==True，text 含[降级]前缀）")

    def test_kb_fallback_disabled_and_failed(self):
        """降级开关关闭 / 降级也失败 → 按普通失败处理，error 汇总两次错误。"""
        def broken_kb(ctx, **kwargs):
            raise RuntimeError("KB 挂了")

        def broken_web(ctx, **kwargs):
            raise RuntimeError("外网不通")

        kb = _make_tool("search_knowledge_base", broken_kb, {"query": {"type": "string"}}, ["query"])

        # 1) kb_fallback_web=False：不降级，直接普通失败
        executor = _make_executor(
            {"search_knowledge_base": kb}, {"tools": {"kb_fallback_web": False}},
        )
        result = executor._run_step(1, PlanStep(action="search_knowledge_base", input={"query": "q"}, step_id=1))
        self.assertFalse(result.ok)
        self.assertFalse(result.degraded)
        self.assertIn("RuntimeError: KB 挂了", result.error)
        self.assertNotIn("[降级]", str(result.output))

        # 2) 降级也失败：error 同时含 KB 错误与 Web 降级错误
        web = _make_tool("web_search", broken_web, {"query": {"type": "string"}}, ["query"])
        executor2 = _make_executor({"search_knowledge_base": kb, "web_search": web})
        result2 = executor2._run_step(1, PlanStep(action="search_knowledge_base", input={"query": "q"}, step_id=1))
        self.assertFalse(result2.ok)
        self.assertFalse(result2.degraded)
        self.assertIn("KB 挂了", result2.error)
        self.assertIn("Web 降级失败", result2.error)

        print("✓ 降级开关关闭 / 降级失败回退为普通失败")

    def test_chat_json_repair_round(self):
        """ACC-S1-04（前半）：坏 JSON 后修复轮返回合法 JSON → 解析成功且共 2 次调用。"""
        llm = ScriptedLLM(["这不是 JSON", '{"ok": true}'], Config({}))
        obj = llm.chat_json([{"role": "user", "content": "给我 JSON"}])
        self.assertEqual(obj, {"ok": True})
        self.assertEqual(llm.calls, 2, f"应共 2 次模型调用，实际 {llm.calls}")

        # 修复轮消息 = 原 messages + 坏输出(assistant) + 修复指令(user)
        repair = llm.seen[1]
        self.assertEqual([m["role"] for m in repair], ["user", "assistant", "user"])
        self.assertEqual(repair[1]["content"], "这不是 JSON")
        self.assertIn("无法解析为 JSON", repair[2]["content"])
        self.assertIn("请只输出修正后的合法 JSON", repair[2]["content"])

        print("✓ chat_json 修复轮（坏 JSON 一次修复成功，共 2 次调用）")

    def test_chat_json_repair_exhausted(self):
        """ACC-S1-04（后半）：修复轮耗尽仍坏 → 抛 LLMError；rounds=0 时直接抛。"""
        llm = ScriptedLLM(["坏输出一", "坏输出二"], Config({}))  # 默认 1 轮修复
        with self.assertRaises(LLMError) as cm:
            llm.chat_json([{"role": "user", "content": "给我 JSON"}])
        self.assertEqual(llm.calls, 2, f"1 轮修复应共 2 次调用，实际 {llm.calls}")
        self.assertIn("无法从 LLM 输出解析 JSON", str(cm.exception))  # 保留原错误信息

        llm0 = ScriptedLLM(["坏输出"], Config({"llm": {"json_repair_rounds": 0}}))
        with self.assertRaises(LLMError):
            llm0.chat_json([{"role": "user", "content": "给我 JSON"}])
        self.assertEqual(llm0.calls, 1)

        print("✓ chat_json 修复轮耗尽抛 LLMError（rounds=0 直接抛）")

    def test_validate_unknown_keys(self):
        """未知键剔除：校验后返回的参数不含 schema 外的键（并 LOG.warning）。"""
        kb = _make_tool(
            "kb", lambda ctx, **kw: kw,
            {"query": {"type": "string"}}, ["query"],
        )
        cleaned = validate_tool_input(kb, {"query": "q", "rogue": 1, "extra": "x"})
        self.assertEqual(cleaned, {"query": "q"})

        # 执行器层面：未知键被剔除后工具不会收到，也不崩溃
        received: list[dict] = []

        def record(ctx, **kwargs):
            received.append(kwargs)
            return {"text": "ok"}

        tool = _make_tool("rec", record, {"query": {"type": "string"}}, ["query"])
        executor = _make_executor({"rec": tool})
        result = executor._run_step(1, PlanStep(action="rec", input={"query": "q", "bogus": True}, step_id=1))
        self.assertTrue(result.ok)
        self.assertEqual(received, [{"query": "q"}])

        print("✓ 未知键剔除")

    def test_validate_string_coercion_and_clamp(self):
        """字符串数字纠偏（"5"→5、"true"→True）与整数按 min/max clamp。"""
        kb = _make_tool(
            "kb", lambda ctx, **kw: kw,
            {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                "flag": {"type": "boolean"},
            },
            ["query"],
        )

        # "5" → 5（int）；"true" → True；"false" → False
        out = validate_tool_input(kb, {"query": "q", "top_k": "5", "flag": "true"})
        self.assertEqual(out["top_k"], 5)
        self.assertIsInstance(out["top_k"], int)
        self.assertIs(out["flag"], True)
        self.assertIs(validate_tool_input(kb, {"query": "q", "flag": "false"})["flag"], False)

        # clamp：超出 min/max 的整数与数字字符串都收敛到边界
        self.assertEqual(validate_tool_input(kb, {"query": "q", "top_k": 99})["top_k"], 10)
        self.assertEqual(validate_tool_input(kb, {"query": "q", "top_k": "0"})["top_k"], 1)

        # 无法纠偏的字符串 → ToolValidationError
        with self.assertRaises(ToolValidationError):
            validate_tool_input(kb, {"query": "q", "top_k": "abc"})

        # 执行器层面：纠偏与 clamp 后的值才传给工具函数
        received: list[dict] = []

        def record(ctx, **kwargs):
            received.append(kwargs)
            return {"text": "ok"}

        recorder = _make_tool("kb", record, kb.parameters["properties"], kb.parameters["required"])
        executor = _make_executor({"kb": recorder})
        result = executor._run_step(
            1, PlanStep(action="kb", input={"query": "q", "top_k": "99", "flag": "false"}, step_id=1)
        )
        self.assertTrue(result.ok)
        self.assertEqual(received, [{"query": "q", "top_k": 10, "flag": False}])

        print("✓ 字符串数字纠偏与整数 clamp")


if __name__ == "__main__":
    unittest.main()
