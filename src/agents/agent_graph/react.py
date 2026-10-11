"""ReAct 子图节点（spec/09 T-011，PH3-ACC-002/003）。

图引擎的多步路径：plan → execute → reflect 三节点，把 legacy 的
Planner/Executor/_reflect_and_extend 全套语义（@step 占位、工具 schema 校验/
重试/降级、检索预算、反思轮数与总步数硬上限）**整体复用，零语义复制**（C6）。

路由：plan_node 内做 G9 快路径判定（与 legacy 同口径：无会话历史 + 简单事实题），
快 → 条件边路由到单发分支（检索并行子图 + 澄清）；复杂 → execute/reflect 多步路径。
两路在 generate 汇合（generate 按 state 里有无 steps 区分口径）。

与 legacy 的计数对齐：llm_calls = 改写(route_entry) + 规划(plan_node) +
反思(_reflect_and_extend 内部自计) + 合成(runner 按 draft/step 计)。
长期记忆注入暂缺（Phase 0/1 同口径边界，见 spec/09 §4 边界 2）。
"""
from __future__ import annotations

from ..agent import Answer, _is_simple_question
from ..executor import Executor
from ..logger import get_logger
from ..planner import Plan, PlanStep, Planner
from ..tools import describe_tools

LOG = get_logger("agent_graph_react")


def make_react_nodes(agent, run_tools: dict, effective_allow_web: bool) -> dict:
    """构建共享 Planner/Executor 闭包的 ReAct 三节点。

    Executor/Planner 生命周期 = 单次问答（build_answer_graph 每次 ask 重建图），
    与 legacy"同一 Executor 实例跨 execute() 调用累积步骤结果"的契约一致，
    使 @step:N 占位符在反思补步间照常工作。
    """
    executor = Executor(agent.config, run_tools, agent._ctx, web_fallback=effective_allow_web)
    planner = Planner(agent.config, agent.llm)

    def plan_node(state):
        if state.get("error"):
            return {}
        question = state["question"]
        history_text = state.get("history_text", "")
        # G9 快路径判定（legacy 同口径）：无会话历史 + 简单事实题 → 单发分支
        if (
            agent.config.planner_fast_path
            and not state.get("history")
            and _is_simple_question(question, agent.config.planner_fast_path_max_len)
        ):
            plan = Plan(
                reasoning="G9 快路径：简单事实题跳过规划",
                plan_summary="快路径：单步知识库检索",
                steps=[PlanStep(action="search_knowledge_base", input={"query": question}, step_id=1)],
            )
            return {"plan": plan, "fast_path": True, "llm_call_count": 0}
        try:
            plan = planner.plan(question, describe_tools(run_tools), history_text=history_text)
        except Exception as exc:  # noqa: BLE001 - C5：规划失败显式入 state（legacy 为整体 error）
            return {"error": f"plan: {type(exc).__name__}: {exc}"}
        # 规划失败回退的查询兜底（legacy ask 同款）：退化计划用改写后的独立查询
        q_work = state.get("search_query") or question
        if plan.fallback and q_work != question and plan.steps:
            plan.steps[0].input["query"] = q_work
        return {"plan": plan, "fast_path": False, "llm_call_count": 1}

    def route_after_plan(state) -> str:
        """快 → 单发分支（retrieve 子图）；复杂 → 多步路径（execute）；错误 → error_node。"""
        if state.get("error"):
            return "error_node"
        return "retrieve" if state.get("fast_path") else "execute"

    def execute_node(state):
        if state.get("error"):
            return {}
        try:
            steps = executor.execute(state["plan"])
            return {"steps": steps}
        except Exception as exc:  # noqa: BLE001 - C5
            return {"error": f"execute: {type(exc).__name__}: {exc}"}

    def reflect_node(state):
        if state.get("error") or state.get("fast_path"):
            return {}
        try:
            # scratch Answer 承载 _reflect_and_extend 的既有契约（就地更新
            # plan/steps/llm_calls，补步由其内部执行），完成后写回图状态。
            scratch = Answer(
                question=state["question"],
                plan=state["plan"],
                steps=list(state.get("steps") or []),
            )
            agent._reflect_and_extend(
                scratch, planner, executor,
                state["question"], state.get("history_text", ""),
                tools=run_tools,
            )
            return {
                "plan": scratch.plan,
                "steps": scratch.steps,
                "reflect_llm_calls": scratch.llm_calls,
            }
        except Exception as exc:  # noqa: BLE001 - C5
            return {"error": f"reflect: {type(exc).__name__}: {exc}"}

    return {
        "plan_node": plan_node,
        "route_after_plan": route_after_plan,
        "execute_node": execute_node,
        "reflect_node": reflect_node,
    }
