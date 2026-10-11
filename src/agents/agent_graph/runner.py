"""LangGraph 引擎运行器（spec/08 LG-0.5/0.6）：主图组装 + ask 分派入口。

主图（Phase 0 单发问答）：
  START → route_entry →(条件边)→ retrieve(检索子图) →(条件边)→ generate
        →(条件边)→ validate_citations →(条件边)→ END
  任意条件边见 error → error_node → END（C5 统一错误出口）

Phase 0 边界（如实声明）：
- 单发问答：不写会话记忆/长期记忆/实体记忆（Phase 1 由 checkpointer 与会话编排接管，
  避免双写）；
- 不含规划器/反思/工具集（规划编排属 Phase 3 评审范围）；
- remember/allow_web 参数仅为与 Agent.ask 签名对齐而保留，当前不生效。
- 同步执行：Agent.ask 为同步契约（web 端点在线程池中调用），故用 graph.invoke。
"""
from __future__ import annotations

import time

from .. import audit as audit_mod
from ..agent import Answer
from ..logger import get_logger
from ..planner import Plan
from .nodes import (
    error_node,
    make_generate_node,
    make_route_entry,
    make_validate_node,
    route_finish,
    route_ok_or_error,
    timed,
)
from .state import GraphState

LOG = get_logger("agent_graph")


def build_answer_graph(agent, on_stage=None):
    """组装并编译主图（LG-0.5：显式条件边错误路由）。

    langgraph 为可选依赖（requirements-langgraph.txt）：仅 engine=langgraph 时需要，
    未安装时抛出的 ImportError 由 run_graph_ask 转为 Answer.error，legacy 模式零影响。
    """
    from langgraph.graph import StateGraph, START, END

    from .retrieval_subgraph import build_retrieval_subgraph

    if agent.retriever is None:
        raise RuntimeError("检索器未初始化：请先加载/构建索引（agent.load_index()）")

    graph = StateGraph(GraphState)
    graph.add_node("route_entry", timed("route_entry", make_route_entry(on_stage)))
    graph.add_node("retrieve", build_retrieval_subgraph(agent.retriever))
    graph.add_node("generate", timed("generate", make_generate_node(agent, on_stage)))
    graph.add_node("validate_citations", timed("validate_citations", make_validate_node(agent)))
    graph.add_node("error_node", timed("error_node", error_node))

    graph.add_edge(START, "route_entry")
    graph.add_conditional_edges(
        "route_entry", route_ok_or_error("retrieve"), {"retrieve": "retrieve", "error_node": "error_node"}
    )
    graph.add_conditional_edges(
        "retrieve", route_ok_or_error("generate"), {"generate": "generate", "error_node": "error_node"}
    )
    graph.add_conditional_edges(
        "generate", route_ok_or_error("validate_citations"),
        {"validate_citations": "validate_citations", "error_node": "error_node"},
    )
    graph.add_conditional_edges("validate_citations", route_finish, {"__end__": END, "error_node": "error_node"})
    graph.add_edge("error_node", END)
    return graph.compile()


def run_graph_ask(
    agent,
    *,
    question: str,
    verbose: bool = False,
    session_id: str = "",
    remember=None,
    allow_web=None,
    on_stage=None,
) -> Answer:
    """LangGraph 引擎的 ask 入口（LG-0.6 单点分派目标），返回与 legacy Answer 兼容。"""
    # G6：与 legacy ask 相同的会话审计关联口径
    audit_mod.set_current_session(session_id)
    answer = Answer(question=question)
    t0 = time.monotonic()
    try:
        # Phase 0 限制显式告警（验收 issue 3）：multi_query 的 LLM 变体展开不在图路径内
        if agent.config.get("retrieval.multi_query", False):
            LOG.warning("retrieval.multi_query=true 在 LangGraph 图路径暂不支持（Phase 0），本次按单查询执行")
        app = build_answer_graph(agent, on_stage=on_stage)
        final = app.invoke({"question": question, "node_metrics": []})
        if verbose and final.get("node_metrics"):
            for m in final["node_metrics"]:
                print(f"  · [graph] {m['node']} {m['duration_ms']}ms")

        answer.plan = final.get("plan") or Plan()
        step = final.get("step_result")
        answer.steps = [step] if step is not None else []
        # 计数语义与 legacy 对齐（验收 issue 1）：图路径当前仅合成一次 LLM 调用
        answer.llm_calls = 1 if step is not None else 0
        answer.sources = final.get("sources") or []
        answer.sources_detail = final.get("sources_detail") or []
        answer.final_answer = final.get("draft", "")
        answer.citations_valid = final.get("citations_valid", 0)
        answer.citations_invalid = final.get("citations_invalid", 0)
        if final.get("error"):
            answer.error = final["error"]
        # 用量口径与 legacy ask 对齐：Answer 暴露会话级累计值
        answer.prompt_tokens = agent.prompt_tokens
        answer.completion_tokens = agent.completion_tokens
        answer.estimated_cost = agent.estimated_cost
    except ImportError as exc:
        answer.error = f"LangGraph 未安装：请执行 .venv/bin/pip install -r requirements-langgraph.txt（或保持 engine: legacy）。{exc}"
    except Exception as exc:  # noqa: BLE001 - 兜底：图外异常（组装/调用层）同样不让进程崩溃
        answer.error = f"{type(exc).__name__}: {exc}"
    answer.total_latency_s = time.monotonic() - t0
    # Phase 0：会话记忆不写（见模块 docstring 边界），审计轨迹保持与 legacy 一致
    agent._audit_ask(question, answer, session_id)
    LOG.info(
        "ask[langgraph] | q=%s | %.1fs | in=%d out=%d | ¥%.4f | %s",
        question[:50].replace("\n", " "), answer.total_latency_s,
        answer.prompt_tokens, answer.completion_tokens, answer.estimated_cost,
        answer.error or "ok",
    )
    return answer
