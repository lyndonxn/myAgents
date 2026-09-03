"""知识库未命中行为测试：答「未在知识库内」并询问是否联网（用户试运行反馈）。

全部离线：LLM 用记录调用、按脚本返回文本的假客户端；步骤用 StepResult 直接构造。
覆盖：规划器 prompt 的联网约束、_kb_miss 判定、合成层未命中提示注入。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.config import Config
from agents.llm import ChatResult, LLMClient
from agents.planner import Plan, build_planner_prompt
from agents.agent import Agent
from agents.executor import StepResult


class ScriptedLLM(LLMClient):
    """离线假 LLM：按脚本返回文本，记录每次收到的 messages。"""

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


def _step(action: str, ok: bool = True, output=None) -> StepResult:
    return StepResult(step_id=1, action=action, output=output if output is not None else {"text": ""}, ok=ok)


MISS_HINT = (
    "\n\n（系统提示：本次知识库检索未命中任何相关片段，且用户尚未同意联网搜索。"
    "请按系统规则回答：开头明确「未在知识库内找到相关内容」，"
    "并询问用户是否需要联网搜索——回复「联网搜索」即可。不要编造、不要硬凑答案。）"
)


def _agent_with_llm(texts: list[str]) -> Agent:
    return Agent(Config({}), llm=ScriptedLLM(texts, Config({})), lazy_index=True)


class KbMissTests(unittest.TestCase):
    """规划器联网约束 / _kb_miss 判定 / 合成层未命中提示注入。"""

    def test_planner_prompt_web_requires_consent(self):
        """规划器 prompt 明确：未经用户同意不自动联网。"""
        msgs = build_planner_prompt("今天天气如何", ["search_knowledge_base: 检索"], 5)
        system = msgs[0]["content"]
        self.assertIn("自行使用 web_search", system)
        self.assertIn("明确同意联网搜索", system)
        self.assertIn("未在知识库内", system)
        print("✓ 规划器 prompt：web_search 需用户明确同意")

    def test_kb_miss_detection(self):
        """_kb_miss：KB 零命中且未联网 → True；有命中/已联网 → False。"""
        zero = [_step("search_knowledge_base", output={"text": "检索到 0 条", "hit_count": 0})]
        self.assertIs(Agent._kb_miss(zero), True)
        self.assertIs(Agent._kb_miss([]), True)
        hit = [_step("search_knowledge_base", output={"text": "命中", "hit_count": 3})]
        self.assertIs(Agent._kb_miss(hit), False)
        web = zero + [_step("web_search", output={"text": "网页结果"})]
        self.assertIs(Agent._kb_miss(web), False)
        failed_kb = [_step("search_knowledge_base", ok=False, output={"text": "失败"})]
        self.assertIs(Agent._kb_miss(failed_kb), True)
        print("✓ _kb_miss 判定（零命中/有命中/已联网/失败步骤）")

    def test_synthesis_miss_hint_injected(self):
        """知识库零命中时合成 prompt 注入「未在知识库内 + 询问联网」系统提示（复现 ask 层接线）。"""
        agent = _agent_with_llm(["未在知识库内找到相关内容。需要我联网搜索吗？"])
        steps = [_step("search_knowledge_base", output={"text": "检索到 0 条相关片段", "hit_count": 0})]
        hint = MISS_HINT if Agent._kb_miss(steps) else ""
        agent._synthesize("今天北京天气怎么样", Plan(plan_summary="检索"), steps, extra_hint=hint)
        user_msg = agent.llm.seen[-1][1]["content"]
        self.assertIn("未命中任何相关片段", user_msg, "零命中应注入系统提示")
        self.assertIn("联网搜索", user_msg)

        # 有命中时不注入
        agent2 = _agent_with_llm(["正常回答"])
        hit_steps = [_step("search_knowledge_base", output={"text": "命中片段", "hit_count": 2})]
        hint2 = MISS_HINT if Agent._kb_miss(hit_steps) else ""
        agent2._synthesize("什么是 RAG", Plan(plan_summary="检索"), hit_steps, extra_hint=hint2)
        user_msg2 = agent2.llm.seen[-1][1]["content"]
        self.assertNotIn("未命中任何相关片段", user_msg2)

        # 已联网（用户同意后）不注入
        agent3 = _agent_with_llm(["联网结果回答"])
        web_steps = steps + [_step("web_search", output={"text": "网页"})]
        hint3 = MISS_HINT if Agent._kb_miss(web_steps) else ""
        agent3._synthesize("今天北京天气怎么样", Plan(plan_summary="联网"), web_steps, extra_hint=hint3)
        user_msg3 = agent3.llm.seen[-1][1]["content"]
        self.assertNotIn("未命中任何相关片段", user_msg3)
        print("✓ 合成层：零命中注入提示，有命中/已联网不注入")


if __name__ == "__main__":
    unittest.main()
