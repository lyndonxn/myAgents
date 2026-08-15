"""规划层（Planner）：把用户问题分解为可执行计划。

计划为结构化 JSON：
{
  "reasoning": "为什么这样拆",
  "steps": [
    {"action": "search_knowledge_base", "input": {"query": "...", "top_k": 5}, "purpose": "..."},
    ...
  ],
  "plan_summary": "一句话计划概述"
}

可用 action 由工具层注册表决定（传入工具描述）。
规划失败或无需规划时退化为"直接单步检索"（fallback_direct）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .llm import LLMError


@dataclass
class PlanStep:
    action: str
    input: dict = field(default_factory=dict)
    purpose: str = ""
    step_id: int = 0


@dataclass
class Plan:
    reasoning: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    plan_summary: str = ""
    fallback: bool = False  # 是否走了退化路径

    @property
    def has_tool_steps(self) -> bool:
        return any(s.action != "none" for s in self.steps)


PLANNER_SYSTEM = """你是一个任务规划器，负责把用户的提问拆解成可执行步骤，以便调用工具检索知识库并回答问题。

规则：
1. 输出必须是合法 JSON 对象，不要输出任何其他文字。
2. 结构为：
{{
  "reasoning": "对问题的分析（1-2 句）",
  "steps": [
    {{"action": "工具名", "input": {{"参数名": 值}}, "purpose": "这一步要解决什么"}}
  ],
  "plan_summary": "计划概述"
}}
3. 优先使用 search_knowledge_base 工具从知识库检索；知识库检索不到或需要最新/时效信息时用 web_search；
   问题包含多个独立子问题时拆成多步；需要精确计算时用 calculator；与知识库无关的通用问题可以用 none（无需工具）。
4. 步骤数尽量少（不超过 {max_steps} 步），每个 search 的 query 要具体、包含关键术语。
5. 只使用下面给出的工具，不要编造工具名。
6. 多轮对话中，如果当前问题指代了之前的话题（如"那它呢"、"再讲讲"），
   每个 search 的 query 必须结合对话历史改写为**独立的完整查询**，不能使用指代词。
"""


def build_planner_prompt(question: str, tool_descriptions: list[str], max_steps: int, history_text: str = "") -> list[dict]:
    tools_text = "\n".join(f"- {d}" for d in tool_descriptions)
    history_block = f"对话历史：\n{history_text}\n\n" if history_text else ""
    user = (
        f"{history_block}"
        f"可用工具：\n{tools_text}\n\n"
        f"用户问题：{question}\n\n"
        "请输出规划 JSON。"
    )
    return [
        {"role": "system", "content": PLANNER_SYSTEM.format(max_steps=max_steps)},
        {"role": "user", "content": user},
    ]


def rewrite_query(llm, question: str, history_text: str) -> str:
    """把追问改写为独立查询，供检索使用（仅当存在对话历史时调用）。"""
    if not history_text:
        return question
    messages = [
        {
            "role": "system",
            "content": "你是查询改写器。把用户在多轮对话中的最新提问改写为一条独立、完整、可单独检索的查询，"
            "保留所有关键实体与限定条件，去掉指代词（它/这个/前面提到的 等）。只输出改写后的查询文本，不要输出任何其他内容。",
        },
        {"role": "user", "content": f"对话历史：\n{history_text}\n\n最新提问：{question}\n\n改写后的独立查询："},
    ]
    try:
        out = llm.chat(messages, temperature=0.0, max_tokens=120).text.strip()
        if out:
            return out
    except LLMError:
        pass
    return question


class Planner:
    def __init__(self, config, llm):
        self.config = config
        self.llm = llm

    def plan(self, question: str, tool_descriptions: list[str], history_text: str = "") -> Plan:
        max_steps = self.config.planner_max_steps
        try:
            messages = build_planner_prompt(question, tool_descriptions, max_steps, history_text)
            obj = self.llm.chat_json(
                messages,
                model=self.config.planner_model,
                temperature=self.config.planner_temperature,
            )
            return self._parse(obj, question)
        except (LLMError, ValueError, TypeError, AttributeError) as exc:
            if self.config.planner_fallback_direct:
                return self._fallback(question, reason=f"规划解析失败（{exc}），退化为直接检索")
            raise

    def _parse(self, obj, question: str) -> Plan:
        if isinstance(obj, dict) and "steps" in obj:
            steps: list[PlanStep] = []
            raw_steps = obj["steps"] if isinstance(obj["steps"], list) else []
            for i, s in enumerate(raw_steps[: self.config.planner_max_steps]):
                if not isinstance(s, dict):
                    continue
                action = str(s.get("action", "")).strip() or "none"
                inp = s.get("input") if isinstance(s.get("input"), dict) else {}
                steps.append(
                    PlanStep(
                        action=action,
                        input=inp,
                        purpose=str(s.get("purpose", "")),
                        step_id=i + 1,
                    )
                )
            if not steps:
                return self._fallback(question, reason="计划为空")
            return Plan(
                reasoning=str(obj.get("reasoning", "")),
                steps=steps,
                plan_summary=str(obj.get("plan_summary", "")),
            )
        return self._fallback(question, reason="计划格式不正确")

    def _fallback(self, question: str, reason: str) -> Plan:
        return Plan(
            reasoning=reason,
            steps=[PlanStep(action="search_knowledge_base", input={"query": question}, purpose="直接检索知识库", step_id=1)],
            plan_summary="直接检索",
            fallback=True,
        )
