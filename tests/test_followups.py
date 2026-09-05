"""W3 切片测试：追问建议 Agent.suggest_followups（LLM 生成，失败静默回空）。

全部离线：LLM 用 ScriptedFollowupsLLM（子类化 LLMClient 覆写 _chat，不经网络）；
通过 monkeypatch agent.LLMClient 替换 suggest_followups 内部创建的独立客户端，
验证成功截断/失败回空/坏结构回空/主客户端计数零污染。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import agent as agent_mod
from agents.config import Config
from agents.llm import ChatResult, LLMClient, LLMError


class ScriptedFollowupsLLM(LLMClient):
    """离线假 LLM：返回预定 JSON 或抛错，记录调用次数。"""

    def __init__(self, config: Config, obj: dict | None = None, error: bool = False):
        self.config = config
        self._obj = obj
        self._error = error
        self.calls = 0

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        if self._error:
            raise LLMError("模拟模型故障")
        return ChatResult(text=json.dumps(self._obj, ensure_ascii=False))


class FollowupsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_client = agent_mod.LLMClient
        self.cfg = Config({"llm": {"mode": "local", "base_url": "http://127.0.0.1:11434/v1", "chat_model": "qwen2.5:7b"}})

    def tearDown(self) -> None:
        agent_mod.LLMClient = self._orig_client

    def _agent(self) -> agent_mod.Agent:
        return agent_mod.Agent(self.cfg, llm=ScriptedFollowupsLLM(self.cfg), lazy_index=True)

    def test_suggest_ok_sanitizes_and_truncates(self):
        """成功路径：空白项剔除、截断到 count；且必须用独立客户端（主客户端计数零污染）。"""
        scripted = ScriptedFollowupsLLM(self.cfg, obj={"questions": ["RAG 和混合检索有什么区别？", "   ", "重排在什么场景必要？", "第三条应被截断"]})
        agent_mod.LLMClient = lambda config, base_url=None, api_key=None: scripted
        agent = self._agent()
        out = agent.suggest_followups("什么是 RAG？", "RAG 是先检索后生成的架构。", 2)
        self.assertEqual(out, ["RAG 和混合检索有什么区别？", "重排在什么场景必要？"], "剔除空白并截断到 count")
        self.assertEqual(scripted.calls, 1, "追问恰好一次独立调用")
        self.assertEqual(agent.llm.calls, 0, "不得复用主 LLM 客户端（避免 ask 计数污染）")

    def test_suggest_llm_error_returns_empty(self):
        """模型故障：静默返回空表，绝不向流式端点抛异常。"""
        scripted = ScriptedFollowupsLLM(self.cfg, error=True)
        agent_mod.LLMClient = lambda config, base_url=None, api_key=None: scripted
        agent = self._agent()
        self.assertEqual(agent.suggest_followups("q", "a"), [])

    def test_suggest_bad_structure_returns_empty(self):
        """JSON 结构缺失 questions / 非法 JSON：回空（坏 JSON 经修复轮后仍失败）。"""
        scripted = ScriptedFollowupsLLM(self.cfg, obj={"nope": 1})
        agent_mod.LLMClient = lambda config, base_url=None, api_key=None: scripted
        agent = self._agent()
        self.assertEqual(agent.suggest_followups("q", "a"), [])

        broken = ScriptedFollowupsLLM(self.cfg, obj={"questions": "不是列表"})
        agent_mod.LLMClient = lambda config, base_url=None, api_key=None: broken
        self.assertEqual(agent.suggest_followups("q", "a"), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
