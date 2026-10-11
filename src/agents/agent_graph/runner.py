"""LangGraph 引擎运行器（spec/08 LG-0.5/0.6 + LG-1.1~1.4）：主图组装 + ask 分派入口。

主图：
  START → route_entry →(条件边)→ retrieve(检索子图) →(条件边)→ clarify_check
        →(澄清路由)→ retrieve(重查) | generate →(条件边)→ validate_citations
        →(条件边)→ commit_turn → END
  任意条件边见 error → error_node → END（C5 统一错误出口）

会话与恢复语义（LG-1.1~1.4）：
- 提供 session_id 时启用 Checkpointer（SQLite），thread_id = session_id：
  history 随 checkpoint 持久化，跨进程/跨实例续接（LG-ACC-004）；
- 检索零命中且 graph.clarify_on_miss 开启时 interrupt() 暂停：
  Answer.pending_clarification 携带追问载荷；同会话的下一问会被当作澄清内容
  经 Command(resume) 注回图继续（LG-ACC-005）；
- resume=True：对已崩溃/中断的线程以 invoke(None) 从最近 checkpoint 续跑
  （LG-ACC-006），已完成的节点不重跑。

Phase 1 边界（如实声明）：
- 不含规划器/反思/工具集（规划编排属 Phase 3 评审范围）；
- 无 session_id 的调用不落 checkpoint（保持无状态，避免 checkpoint 无界增长）；
- 澄清中断的 Web 端点集成属后续切片，当前经 Answer.pending_clarification 暴露；
- remember/allow_web 参数仅为与 Agent.ask 签名对齐而保留，当前不生效。
- 同步执行：Agent.ask 为同步契约（web 端点在线程池中调用），故用 graph.invoke +
  同步版 SqliteSaver（spec 原文为 AsyncSqliteSaver——引擎是同步契约，按实际接缝
  调整为同步 saver，已在 spec/langgraph-phase1.md 登记偏差）。
"""
from __future__ import annotations

import sqlite3
import time
import uuid

from .. import audit as audit_mod
from ..agent import Answer
from ..logger import get_logger
from ..planner import Plan
from .nodes import (
    commit_turn,
    error_node,
    make_clarify_node,
    make_generate_node,
    make_route_entry,
    make_validate_node,
    route_clarify,
    route_finish,
    route_ok_or_error,
    timed,
)
from .state import GraphState

LOG = get_logger("agent_graph")


def _get_checkpointer(agent):
    """按 agent 惰性创建/复用同步 SqliteSaver（LG-1.1）。

    连接 check_same_thread=False：web 端点在线程池中调用 ask，单用户本地场景
    由 sqlite 自身写锁串行化。库路径来自 graph.checkpoint_db（默认 data_dir 下）。
    """
    cached = getattr(agent, "_lg_checkpointer", None)
    if cached is not None:
        return cached
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = agent.config.graph_checkpoint_db
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    agent._lg_checkpointer = saver
    LOG.info("LangGraph Checkpointer 就绪: %s", path)
    return saver


def build_answer_graph(agent, on_stage=None, checkpointer=None):
    """组装并编译主图（LG-0.5：显式条件边错误路由；checkpointer 由调用方按会话决定）。"""
    from langgraph.graph import StateGraph, START, END

    from .retrieval_subgraph import build_retrieval_subgraph

    if agent.retriever is None:
        raise RuntimeError("检索器未初始化：请先加载/构建索引（agent.load_index()）")

    graph = StateGraph(GraphState)
    graph.add_node("route_entry", timed("route_entry", make_route_entry(agent, on_stage)))
    graph.add_node("retrieve", build_retrieval_subgraph(agent.retriever))
    graph.add_node("clarify_check", timed("clarify_check", make_clarify_node(agent)))
    graph.add_node("generate", timed("generate", make_generate_node(agent, on_stage)))
    graph.add_node("validate_citations", timed("validate_citations", make_validate_node(agent)))
    graph.add_node("commit_turn", timed("commit_turn", commit_turn))
    graph.add_node("error_node", timed("error_node", error_node))

    graph.add_edge(START, "route_entry")
    graph.add_conditional_edges(
        "route_entry", route_ok_or_error("retrieve"), {"retrieve": "retrieve", "error_node": "error_node"}
    )
    graph.add_conditional_edges(
        "retrieve", route_ok_or_error("clarify_check"), {"clarify_check": "clarify_check", "error_node": "error_node"}
    )
    graph.add_conditional_edges(
        "clarify_check", route_clarify, {"retrieve": "retrieve", "generate": "generate"}
    )
    graph.add_conditional_edges(
        "generate", route_ok_or_error("validate_citations"),
        {"validate_citations": "validate_citations", "error_node": "error_node"},
    )
    graph.add_conditional_edges(
        "validate_citations", route_finish, {"commit_turn": "commit_turn", "error_node": "error_node"}
    )
    graph.add_edge("commit_turn", END)
    graph.add_edge("error_node", END)
    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()


def _extract_interrupt(snapshot) -> dict:
    """从状态快照提取挂起的 interrupt 载荷（无则返回空 dict）。"""
    try:
        for task in getattr(snapshot, "tasks", None) or ():
            for intr in getattr(task, "interrupts", None) or ():
                value = getattr(intr, "value", None)
                if isinstance(value, dict):
                    return value
    except Exception:  # noqa: BLE001 - 检测失败按"无挂起"处理
        pass
    return {}


def run_graph_ask(
    agent,
    *,
    question: str = "",
    verbose: bool = False,
    session_id: str = "",
    remember=None,
    allow_web=None,
    on_stage=None,
    resume: bool = False,
) -> Answer:
    """LangGraph 引擎的 ask 入口（LG-0.6 单点分派目标），返回与 legacy Answer 兼容。

    - 常规调用：question 为新问题；同会话存在挂起澄清时，question 会被当作澄清
      内容经 Command(resume) 注回图（LG-ACC-005）。
    - resume=True：忽略 question，对最近 checkpoint 以 invoke(None) 续跑（崩溃恢复）。
    """
    audit_mod.set_current_session(session_id)
    answer = Answer(question=question)
    t0 = time.monotonic()
    try:
        # Phase 0 限制显式告警：multi_query 的 LLM 变体展开不在图路径内
        if agent.config.get("retrieval.multi_query", False):
            LOG.warning("retrieval.multi_query=true 在 LangGraph 图路径暂不支持（Phase 0），本次按单查询执行")

        use_checkpoint = bool(session_id) or resume
        checkpointer = _get_checkpointer(agent) if use_checkpoint else None
        app = build_answer_graph(agent, on_stage=on_stage, checkpointer=checkpointer)
        config = {"configurable": {"thread_id": session_id or f"adhoc-{uuid.uuid4()}"}}

        graph_input: object
        if resume:
            # LG-1.4：从最近 checkpoint 续跑，已完成节点不重跑；线程不存在时给出可读错误
            snapshot = app.get_state(config)
            if not snapshot or not snapshot.values:
                answer.error = f"未找到可恢复的会话线程: {session_id or '(未提供 session_id)'}"
                answer.total_latency_s = time.monotonic() - t0
                agent._audit_ask(question, answer, session_id)
                LOG.info("ask[langgraph] | resume 无可恢复线程 | session=%s", session_id)
                return answer
            graph_input = None
        elif use_checkpoint:
            pending = _extract_interrupt(app.get_state(config))
            if pending:
                LOG.info("会话存在挂起澄清，将本次提问作为澄清内容注入")
                from langgraph.types import Command

                graph_input = Command(resume={"clarified_query": question})
            else:
                graph_input = {"question": question, "node_metrics": []}
        else:
            graph_input = {"question": question, "node_metrics": []}

        final = app.invoke(graph_input, config)
        if verbose and final.get("node_metrics"):
            for m in final["node_metrics"]:
                print(f"  · [graph] {m['node']} {m['duration_ms']}ms")

        answer.plan = final.get("plan") or Plan()
        step = final.get("step_result")
        answer.steps = [step] if step is not None else []
        answer.sources = final.get("sources") or []
        answer.sources_detail = final.get("sources_detail") or []
        answer.final_answer = final.get("draft", "")
        answer.citations_valid = final.get("citations_valid", 0)
        answer.citations_invalid = final.get("citations_invalid", 0)
        # 计数语义与 legacy 对齐：改写/合成等图内 LLM 调用数 + 合成一次
        answer.llm_calls = final.get("llm_call_count", 0) + (1 if step is not None else 0)
        if final.get("error"):
            answer.error = final["error"]

        # LG-1.3：interrupt 挂起 → Answer.pending_clarification 暴露（answer 尚未生成）
        interrupts = final.get("__interrupt__")
        if interrupts:
            value = getattr(interrupts[0], "value", None)
            if isinstance(value, dict):
                answer.pending_clarification = value
                LOG.info("图执行挂起等待澄清: %s", str(value.get("message", ""))[:60])
        elif use_checkpoint and not answer.error:
            pending = _extract_interrupt(app.get_state(config))
            if pending:
                answer.pending_clarification = pending

        # 用量口径与 legacy ask 对齐：Answer 暴露会话级累计值
        answer.prompt_tokens = agent.prompt_tokens
        answer.completion_tokens = agent.completion_tokens
        answer.estimated_cost = agent.estimated_cost
    except ImportError as exc:
        answer.error = f"LangGraph 未安装：请执行 .venv/bin/pip install -r requirements-langgraph.txt（或保持 engine: legacy）。{exc}"
    except Exception as exc:  # noqa: BLE001 - 兜底：图外异常（组装/调用层）同样不让进程崩溃
        answer.error = f"{type(exc).__name__}: {exc}"
    answer.total_latency_s = time.monotonic() - t0
    # 会话记忆不写 legacy SessionMemory（不双写，LG-1.2）；审计轨迹保持与 legacy 一致
    agent._audit_ask(question, answer, session_id)
    LOG.info(
        "ask[langgraph] | q=%s | %.1fs | calls=%d | in=%d out=%d | ¥%.4f | %s",
        question[:50].replace("\n", " "), answer.total_latency_s, answer.llm_calls,
        answer.prompt_tokens, answer.completion_tokens, answer.estimated_cost,
        answer.error or ("pending_clarification" if answer.pending_clarification else "ok"),
    )
    return answer
