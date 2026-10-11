"""LangGraph 引擎运行器（spec/08 LG-0.5/0.6 + LG-1.1~1.4 + LG-2.1/2.2）。

主图：
  START → route_entry →(条件边)→ retrieve(检索子图) →(条件边)→ clarify_check
        →(澄清路由)→ retrieve(重查) | generate →(条件边)→ validate_citations
        →(条件边)→ commit_turn → END
  任意条件边见 error → error_node → END（C5 统一错误出口）

入口：
- run_graph_ask：同步一次性（invoke），Answer 兼容 legacy；
- stream_graph_ask：同步生成器（graph.stream(stream_mode=["custom","values"])），
  generate 节点经 get_stream_writer 产出 {"event":"delta"} 真 token 事件（LG-2.1），
  最后 yield {"event":"_result","answer":Answer} 供调用方持久化收尾。

会话与恢复语义（LG-1.1~1.4）：
- 提供 session_id 时启用 Checkpointer（SQLite），thread_id = session_id；
- 检索零命中且 graph.clarify_on_miss 开启时 interrupt() 暂停；
- resume=True：对已崩溃/中断的线程以 invoke(None)/stream(None) 从最近 checkpoint 续跑。

Phase 2 边界（如实声明）：
- 无 session_id 的调用不落 checkpoint（保持无状态，避免 checkpoint 无界增长）；
- 澄清中断的 Web 端点集成属后续切片；
- remember/allow_web 参数仅为与 Agent.ask 签名对齐而保留，当前不生效。
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


def build_answer_graph(agent, on_stage=None, checkpointer=None, streaming: bool = False):
    """组装并编译主图（LG-0.5 显式条件边错误路由；streaming 仅 stream_graph_ask 置真）。"""
    from langgraph.graph import StateGraph, START, END

    from .retrieval_subgraph import build_retrieval_subgraph

    if agent.retriever is None:
        raise RuntimeError("检索器未初始化：请先加载/构建索引（agent.load_index()）")

    graph = StateGraph(GraphState)
    graph.add_node("route_entry", timed("route_entry", make_route_entry(agent, on_stage)))
    graph.add_node("retrieve", build_retrieval_subgraph(agent.retriever))
    graph.add_node("clarify_check", timed("clarify_check", make_clarify_node(agent)))
    graph.add_node("generate", timed("generate", make_generate_node(agent, on_stage, streaming=streaming)))
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


def _prepare_run(agent, question: str, session_id: str, resume: bool, on_stage, streaming: bool = False):
    """两次入口共用的运行准备：checkpointer 决策、thread 映射、输入构造（含恢复语义）。

    返回 (app, config, graph_input, error)；error 非空时其余为 None。
    """
    if agent.config.get("retrieval.multi_query", False):
        LOG.warning("retrieval.multi_query=true 在 LangGraph 图路径暂不支持（Phase 0），本次按单查询执行")
    use_checkpoint = bool(session_id) or resume
    checkpointer = _get_checkpointer(agent) if use_checkpoint else None
    app = build_answer_graph(agent, on_stage=on_stage, checkpointer=checkpointer, streaming=streaming)
    config = {"configurable": {"thread_id": session_id or f"adhoc-{uuid.uuid4()}"}}

    if resume:
        # LG-1.4：从最近 checkpoint 续跑，已完成节点不重跑；线程不存在时给出可读错误
        snapshot = app.get_state(config)
        if not snapshot or not snapshot.values:
            return None, None, None, f"未找到可恢复的会话线程: {session_id or '(未提供 session_id)'}"
        return app, config, None, ""
    if use_checkpoint:
        pending = _extract_interrupt(app.get_state(config))
        if pending:
            LOG.info("会话存在挂起澄清，将本次提问作为澄清内容注入")
            from langgraph.types import Command

            return app, config, Command(resume={"clarified_query": question}), ""
    return app, config, {"question": question, "node_metrics": []}, ""


def _finalize_answer(answer: Answer, final: dict, agent, use_checkpoint: bool, app=None, config: dict | None = None) -> Answer:
    """两次入口共用的收尾：final state → Answer（LG-2.2 节点指标随 Answer 暴露）。"""
    answer.plan = final.get("plan") or Plan()
    step = final.get("step_result")
    answer.steps = [step] if step is not None else []
    answer.sources = final.get("sources") or []
    answer.sources_detail = final.get("sources_detail") or []
    answer.final_answer = final.get("draft", "")
    answer.citations_valid = final.get("citations_valid", 0)
    answer.citations_invalid = final.get("citations_invalid", 0)
    answer.node_metrics = final.get("node_metrics") or []
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
    elif use_checkpoint and app is not None and config is not None and not answer.error:
        pending = _extract_interrupt(app.get_state(config))
        if pending:
            answer.pending_clarification = pending

    # 用量口径与 legacy ask 对齐：Answer 暴露会话级累计值
    answer.prompt_tokens = agent.prompt_tokens
    answer.completion_tokens = agent.completion_tokens
    answer.estimated_cost = agent.estimated_cost
    return answer


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


def _finish_meta(answer: Answer, agent, question: str, session_id: str, t0: float) -> Answer:
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
        app, config, graph_input, err = _prepare_run(agent, question, session_id, resume, on_stage)
        if err:
            answer.error = err
            return _finish_meta(answer, agent, question, session_id, t0)

        final = app.invoke(graph_input, config)
        if verbose and final.get("node_metrics"):
            for m in final["node_metrics"]:
                print(f"  · [graph] {m['node']} {m['duration_ms']}ms")
        use_checkpoint = bool(session_id) or resume
        _finalize_answer(answer, final, agent, use_checkpoint, app=app, config=config)
    except ImportError as exc:
        answer.error = f"LangGraph 未安装：请执行 .venv/bin/pip install -r requirements-langgraph.txt（或保持 engine: legacy）。{exc}"
    except Exception as exc:  # noqa: BLE001 - 兜底：图外异常（组装/调用层）同样不让进程崩溃
        answer.error = f"{type(exc).__name__}: {exc}"
    return _finish_meta(answer, agent, question, session_id, t0)


def stream_graph_ask(
    agent,
    *,
    question: str = "",
    verbose: bool = False,
    session_id: str = "",
    remember=None,
    allow_web=None,
    on_stage=None,
    resume: bool = False,
):
    """同步流式入口（LG-2.1）：yield 与 /api/ask_stream 协议对齐的事件 dict。

    - {"event": "delta", "text": …}：generate 节点经 get_stream_writer 写出的
      真 token 事件（流式合成，非 legacy 的整段切 24 字符）；
    - {"event": "_result", "answer": Answer}：终止事件，携带完整 Answer 供调用方
      持久化（_persist_answer）与收尾（meta/done/followups），不应直接下发前端。
    事件顺序与 legacy 不同（delta 先于 meta）：meta 依赖完成后才有的 sources，
    前端按事件类型渲染，属运行时验证项。
    """
    audit_mod.set_current_session(session_id)
    answer = Answer(question=question)
    t0 = time.monotonic()
    try:
        app, config, graph_input, err = _prepare_run(agent, question, session_id, resume, on_stage, streaming=True)
        if err:
            answer.error = err
            yield {"event": "_result", "answer": answer}
            return
        final: dict = {}
        for mode, chunk in app.stream(graph_input, config, stream_mode=["custom", "values"]):
            if mode == "custom":
                yield chunk
            elif mode == "values":
                final = chunk
        if verbose and final.get("node_metrics"):
            for m in final["node_metrics"]:
                print(f"  · [graph] {m['node']} {m['duration_ms']}ms")
        use_checkpoint = bool(session_id) or resume
        _finalize_answer(answer, final, agent, use_checkpoint, app=app, config=config)
    except ImportError as exc:
        answer.error = f"LangGraph 未安装：请执行 .venv/bin/pip install -r requirements-langgraph.txt（或保持 engine: legacy）。{exc}"
    except Exception as exc:  # noqa: BLE001
        answer.error = f"{type(exc).__name__}: {exc}"
    _finish_meta(answer, agent, question, session_id, t0)
    yield {"event": "_result", "answer": answer}
