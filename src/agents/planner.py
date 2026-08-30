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

反思重规划（S2 ReAct 迭代）：reflect() 在首轮执行后审视步骤轨迹
（每步 ok/error/输出摘要），判断是否需要补步；输出 JSON
{need_more, reasoning, steps:[...]}，解析失败抛 LLMError 由调用方静默吞掉。
"""
from __future__ import annotations

import json
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
    reflections: list[str] = field(default_factory=list)  # 各轮反思的 reasoning（S2 反思重规划）
    rounds: int = 1  # 执行轮数（首轮规划为 1，每追加一轮补步 +1）

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
3. 优先使用 search_knowledge_base 工具从知识库检索；默认**不要**自行使用 web_search——
   只有当用户在对话中明确同意联网搜索（如回复"联网搜索"、"搜一下"、"好的，搜吧"），
   或问题本身明确要求查询互联网内容（如"帮我搜网上"）时才使用 web_search；
   知识库检索不到时不要自行联网，由回答层说明"未在知识库内"并询问用户。
   问题包含多个独立子问题时拆成多步；需要精确计算时用 calculator；与知识库无关的通用问题可以用 none（无需工具）。
4. 步骤数尽量少（不超过 {max_steps} 步），每个 search 的 query 要具体、包含关键术语。
5. 只使用下面给出的工具，不要编造工具名。
6. 多轮对话中，如果当前问题指代了之前的话题（如"那它呢"、"再讲讲"），
   每个 search 的 query 必须结合对话历史改写为**独立的完整查询**，不能使用指代词。
7. 后续步骤可在 input 的字符串值里引用之前步骤的输出（步骤数据传递）：
   @step:N 代表步骤 N 输出的全文（text），@step:N.field 代表步骤 N 输出 JSON 的 field 字段
   （如 @step:1.query 表示取步骤 1 输出里的 query）。步骤编号从 1 开始；
   引用不可用时该占位符会替换为空字符串。
"""

REFLECT_SYSTEM = """你是一个反思协调器（ReAct）：给定用户问题、可用工具和已执行步骤的轨迹，\
请判断现有信息是否足以回答问题；不足时规划少量补充步骤。

规则：
1. 输出必须是合法 JSON 对象，不要输出任何其他文字，结构为：
{{"need_more": true 或 false, "reasoning": "判断理由（1-2 句）", "steps": []}}
2. steps 元素结构与规划相同：{{"action": "工具名", "input": {{"参数名": 值}}, "purpose": "这一步要解决什么"}}；
   现有信息已足够时 need_more 为 false 且 steps 为空数组，不要为了补步而补步。
3. 只使用下面给出的工具，不要编造工具名；补步不超过 {max_steps} 步。
4. 补步的 input 字符串值可用 @step:N / @step:N.field 引用已有步骤（含补步）的输出，
   语法与规划器相同；引用失败时占位符会替换为空字符串。
"""


def build_planner_prompt(
    question: str,
    tool_descriptions: list[str],
    max_steps: int,
    history_text: str = "",
    longterm_text: str = "",
) -> list[dict]:
    tools_text = "\n".join(f"- {d}" for d in tool_descriptions)
    history_block = f"对话历史：\n{history_text}\n\n" if history_text else ""
    # 跨会话相关历史经验块（S4 长期记忆召回），仅在有内容时注入
    longterm_block = f"{longterm_text}\n\n" if longterm_text else ""
    user = (
        f"{history_block}"
        f"{longterm_block}"
        f"可用工具：\n{tools_text}\n\n"
        f"用户问题：{question}\n\n"
        "请输出规划 JSON。"
    )
    return [
        {"role": "system", "content": PLANNER_SYSTEM.format(max_steps=max_steps)},
        {"role": "user", "content": user},
    ]


def build_reflect_prompt(
    question: str,
    tool_descriptions: list[str],
    history_text: str,
    trajectory: list[dict],
    remaining_budget: int,
) -> list[dict]:
    """构建反思重规划 prompt：轨迹为每步 {step_id, action, ok, error, output 摘要} 列表。"""
    tools_text = "\n".join(f"- {d}" for d in tool_descriptions)
    history_block = f"对话历史：\n{history_text}\n\n" if history_text else ""
    traj_text = json.dumps(trajectory, ensure_ascii=False, default=str)
    user = (
        f"{history_block}"
        f"可用工具：\n{tools_text}\n\n"
        f"用户问题：{question}\n\n"
        f"已执行步骤轨迹（step_id / action / ok / error / 输出摘要）：\n{traj_text}\n\n"
        f"补步数量上限：{max(0, remaining_budget)}。\n\n"
        "请输出反思 JSON（need_more / reasoning / steps）。"
    )
    return [
        {"role": "system", "content": REFLECT_SYSTEM.format(max_steps=max(0, remaining_budget))},
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

    def plan(self, question: str, tool_descriptions: list[str], history_text: str = "", longterm_text: str = "") -> Plan:
        max_steps = self.config.planner_max_steps
        try:
            messages = build_planner_prompt(question, tool_descriptions, max_steps, history_text, longterm_text)
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

    def reflect(
        self,
        question: str,
        tool_descriptions: list[str],
        history_text: str,
        trajectory: list[dict],
        remaining_budget: int,
    ) -> Plan:
        """反思已执行轨迹，判断是否需要补步（S2 ReAct 迭代）。

        返回的 Plan 只承载补步步骤（step_id 从 1 临时编号，由调用方续号）；
        need_more=false 或没有有效补步时返回空步骤计划。
        解析失败（坏 JSON 或结构不合法）抛 LLMError，由调用方静默吞掉。
        """
        try:
            messages = build_reflect_prompt(
                question, tool_descriptions, history_text, trajectory, remaining_budget
            )
            obj = self.llm.chat_json(
                messages,
                model=self.config.planner_model,
                temperature=self.config.planner_temperature,
            )
            return self._parse_reflection(obj, remaining_budget)
        except (LLMError, ValueError, TypeError, AttributeError) as exc:
            raise LLMError(f"反思解析失败（{exc}）") from exc

    def _parse_reflection(self, obj, remaining_budget: int) -> Plan:
        """解析反思输出：步骤校验复用 _parse 的构建逻辑，结构不合法视为解析失败。"""
        if not (isinstance(obj, dict) and isinstance(obj.get("steps"), list)):
            raise ValueError("反思输出缺少 need_more/steps 结构")
        reasoning = str(obj.get("reasoning", ""))
        if not obj.get("need_more"):
            return Plan(reasoning=reasoning, steps=[], plan_summary="反思：无需补步")
        steps = self._build_steps(obj["steps"], remaining_budget)
        return Plan(reasoning=reasoning, steps=steps, plan_summary="反思补步")

    def _build_steps(self, raw_steps: list, limit: int) -> list[PlanStep]:
        """从原始 JSON 步骤列表构建 PlanStep（规划与反思解析共用），超出 limit 截断。"""
        steps: list[PlanStep] = []
        for i, s in enumerate(raw_steps[: max(0, limit)]):
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
        return steps

    def _parse(self, obj, question: str) -> Plan:
        if isinstance(obj, dict) and "steps" in obj:
            raw_steps = obj["steps"] if isinstance(obj["steps"], list) else []
            steps = self._build_steps(raw_steps, self.config.planner_max_steps)
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
