"""主图节点（spec/08 LG-0.4/0.5 + LG-1.2/1.3/1.5）：入口路由、澄清中断、合成、引用校验、轮次提交。

复用边界（薄封装，零提示词复制）：
- 追问改写复用 planner.rewrite_query（其自带 LLMError fail-soft，返回原查询）；
- 合成走 Agent._synthesize（证据压缩/Prefix Caching 友好 prompt 全部继承）；
- 检索步骤输出构造复用 tools._fmt_hit / _hit_detail，与 legacy 工具输出同构，
  使 _collect_source_info / 前端证据卡无需任何改动；
- 引用校验复用 citations.validate_citations（S3）。

错误处理遵循 C5：节点失败写 error 字段并停止推进（下游见 error 透传），
主图条件边将其路由到 error_node —— 不复刻参考项目"置 error 后继续走"的反模式。
澄清中断遵循 LG-1.3：检索零命中（可配）时 interrupt() 暂停等待用户补充，
经 Command(resume={"clarified_query": …}) 恢复后重跑检索；澄清轮数上限 1，防循环。
"""
from __future__ import annotations

import functools
import time

from ..agent import _emit_stage
from ..citations import validate_citations
from ..executor import StepResult
from ..logger import get_logger
from ..planner import Plan, PlanStep, rewrite_query
from ..tools import _fmt_hit, _hit_detail

LOG = get_logger("agent_graph")

# 与 Agent.ask 的 miss_hint 保持一致（Phase 1 抽公共常量）：
# 知识库零命中且未联网时，约束合成按「未在知识库内」作答并询问是否联网。
_KB_MISS_HINT = (
    "\n\n（系统提示：本次知识库检索未命中任何相关片段，且用户尚未同意联网搜索。"
    "请按系统规则回答：开头明确「未在知识库内找到相关内容」，"
    "并询问用户是否需要联网搜索——回复「联网搜索」即可。不要编造、不要硬凑答案。）"
)

_CLARIFY_MESSAGE = "知识库中没有检索到与问题直接相关的内容。请补充背景或换个问法（也可以直接跳过，按「未在知识库内」作答）。"


def format_history(history: list[dict]) -> str:
    """把会话历史拼装为文本（改写与合成共用同一口径）。"""
    return "\n".join(f"用户：{t.get('q', '')}\n助手：{t.get('a', '')}" for t in history)


def timed(name: str, fn):
    """节点耗时包装（LG-0.2）：把执行时长记入 state["node_metrics"]。

    state 中该键带 operator.add reducer，节点返回 {"node_metrics": [单条]} 即追加；
    正常与失败路径都记录（失败时长同样有归因价值）。
    """

    @functools.wraps(fn)
    def wrapper(state):
        t0 = time.monotonic()
        update = fn(state) or {}
        return {**update, "node_metrics": [{"node": name, "duration_ms": round((time.monotonic() - t0) * 1000, 2)}]}

    return wrapper


# ---------------- 主图节点 ----------------

def make_route_entry(agent, on_stage=None):
    """入口节点：问题规范化 + 会话历史追问改写（LG-1.5，复用 planner.rewrite_query）。"""

    def route_entry(state):
        question = (state.get("question") or "").strip()
        if not question:
            return {"error": "问题为空，无法执行问答"}
        _emit_stage(on_stage, "检索知识库")
        history = state.get("history") or []
        history_text = format_history(history)
        search_query = question
        llm_calls = 0
        if history_text:
            rewritten = rewrite_query(agent.llm, question, history_text)
            if rewritten and rewritten != question:
                search_query = rewritten
                llm_calls = 1
        return {"question": question, "search_query": search_query, "history_text": history_text, "llm_call_count": llm_calls}

    return route_entry


def make_clarify_node(agent):
    """澄清中断节点（LG-1.3）：检索零命中（graph.clarify_on_miss）时 interrupt 等待用户补充。

    恢复语义：Command(resume={"clarified_query": …})（或纯字符串）→ 以澄清文本重跑检索；
    用户跳过（空恢复值）或澄清一轮仍未命中 → 按未命中口径进入合成（miss_hint）。
    """

    def clarify_check(state):
        if state.get("error"):
            return {"pending_requery": False}
        if state.get("chunks"):
            return {"pending_requery": False}
        if not agent.config.graph_clarify_on_miss:
            return {"pending_requery": False}
        if state.get("clarify_round", 0) >= 1:
            LOG.info("澄清一轮后仍未命中，按未命中口径继续合成")
            return {"pending_requery": False}
        from langgraph.types import interrupt

        user = interrupt({
            "type": "clarify",
            "question": state.get("question", ""),
            "message": _CLARIFY_MESSAGE,
        })
        clarified = ""
        if isinstance(user, dict):
            clarified = str(user.get("clarified_query", "") or "").strip()
        elif isinstance(user, str):
            clarified = user.strip()
        if not clarified:
            LOG.info("用户跳过澄清，按未命中口径继续合成")
            return {"pending_requery": False}
        LOG.info("澄清恢复，改用新查询重跑检索: %s", clarified[:50])
        return {
            "pending_requery": True,
            "clarify_round": 1,
            # 澄清文本同时接管 question：合成与 history 记录以澄清后的真实意图为准
            "question": clarified,
            "search_query": clarified,
            "chunks": [],
            "candidates": [],
            "fused": {},
            "vec_hits": [],
            "kw_hits": [],
        }

    return clarify_check


def route_clarify(state) -> str:
    """澄清路由：待重查 → 回检索子图；否则进入合成。"""
    return "retrieve" if state.get("pending_requery") else "generate"


def make_generate_node(agent, on_stage=None, streaming: bool = False, web_allowed: bool = False):
    """合成节点：两路汇合——单发分支用检索子图结果组装单步输出；
    ReAct 分支直接复用 state["steps"]（真实工具输出）与 legacy 的
    来源收集/未命中判定。均复用 _synthesize / synthesize_stream。

    streaming=True（仅 stream_graph_ask 构建）时经 get_stream_writer 把 token
    以 {"event": "delta"} 自定义事件写出；非流式上下文该调用抛 RuntimeError，
    安全回退 on_token=None → 非流式 chat（invoke 路径零行为变化）。
    """

    def generate(state):
        if state.get("error"):
            return {}
        _emit_stage(on_stage, "生成答案")
        try:
            question = state.get("question", "")
            search_query = state.get("search_query") or question
            react_steps = state.get("steps") or []
            if react_steps:
                # ReAct 多步路径：真实工具步骤 → legacy 同款来源收集与未命中判定
                info = agent._collect_source_info(react_steps)
                sources = [src for src, _ in info]
                sources_detail = [
                    detail or {"title": src, "path": src, "heading": "", "snippet": ""}
                    for src, detail in info
                ]
                plan = state.get("plan") or Plan(reasoning="LangGraph ReAct 多步编排", steps=[])
                steps_for_synth = react_steps
                hint = _KB_MISS_HINT if agent._kb_miss(react_steps, web_allowed=web_allowed) else ""
            else:
                chunks = state.get("chunks") or []
                lines = [f"针对「{search_query}」检索到 {len(chunks)} 条相关片段："]
                sources: list[str] = []
                sources_detail: list[dict] = []
                for rank, hit in enumerate(chunks, start=1):
                    lines.append(_fmt_hit(rank, hit))
                    src = hit.source or hit.file
                    if src not in sources:
                        sources.append(src)
                        sources_detail.append(_hit_detail(hit))
                output = {
                    "text": "\n".join(lines),
                    "sources": sources,
                    "hit_count": len(chunks),
                    "sources_detail": sources_detail,
                }
                plan = state.get("plan") or Plan(
                    reasoning="LangGraph 图编排：单步检索→合成→引用校验",
                    plan_summary="LangGraph：检索→（澄清）→生成→引用校验",
                    steps=[PlanStep(action="search_knowledge_base", input={"query": search_query}, step_id=1)],
                )
                steps_for_synth = [StepResult(
                    step_id=1,
                    action="search_knowledge_base",
                    input={"query": search_query},
                    output=output,
                    ok=True,
                )]
                # 知识库零命中提示与 legacy ask 口径一致；澄清被跳过/未命中时同样生效
                hint = _KB_MISS_HINT if not chunks else ""

            on_token = None
            if streaming:
                try:
                    from langgraph.config import get_stream_writer

                    writer = get_stream_writer()
                except Exception:  # noqa: BLE001 - 非 stream 上下文回退非流式
                    writer = None
                if callable(writer):
                    on_token = lambda t: writer({"event": "delta", "text": t})  # noqa: E731
            draft = agent.synthesize_stream(
                question, plan, steps_for_synth,
                history_text=state.get("history_text", ""),
                extra_hint=hint,
                on_token=on_token,
            )
            return {
                "draft": draft,
                "sources": sources,
                "sources_detail": sources_detail,
                "step_result": steps_for_synth[-1] if steps_for_synth else None,
                "plan": plan,
            }
        except Exception as exc:  # noqa: BLE001 - C5：失败显式入 state，由条件边路由 error_node
            return {"error": f"generate: {type(exc).__name__}: {exc}"}

    return generate


def make_validate_node(agent):
    """引用校验节点（S3 复用）：按实际来源数校验 [n]，剔除幻觉引用。"""

    def validate_citations_node(state):
        if state.get("error"):
            return {}
        try:
            sources = state.get("sources") or []
            report = validate_citations(state.get("draft", ""), len(sources))
            if report.invalid_count:
                LOG.warning(
                    "引用校验：剔除 %d 个非法引用编号 %s（实际来源数=%d）",
                    report.invalid_count, report.invalid_numbers, len(sources),
                )
            return {
                "draft": report.cleaned_text,
                "citations_valid": report.valid_count,
                "citations_invalid": report.invalid_count,
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": f"validate_citations: {type(exc).__name__}: {exc}"}

    return validate_citations_node


def commit_turn(state):
    """轮次提交节点（LG-1.2）：成功轮次写入 state["history"]，随 Checkpointer 持久化。

    不双写 legacy SessionMemory——图引擎的会话状态以 checkpoint 为唯一载体。
    """
    if state.get("error"):
        return {}
    answer_text = state.get("draft", "")
    if not answer_text:
        return {}
    history = list(state.get("history") or [])
    history.append({"q": state.get("question", ""), "a": answer_text})
    return {"history": history}


def error_node(state):
    """统一错误出口（C5 终点）：draft 置空，避免半截产物流向调用方。"""
    err = state.get("error") or "未知错误"
    LOG.warning("LangGraph 图编排失败: %s", err)
    return {"error": err, "draft": ""}


# ---------------- 条件边路由 ----------------

def route_ok_or_error(next_node: str):
    """通用路由工厂：无 error → 下一节点；有 error → error_node。"""

    def router(state) -> str:
        return "error_node" if state.get("error") else next_node

    return router


def route_finish(state) -> str:
    """末节点路由：校验通过 → 提交轮次；有 error → error_node。"""
    return "commit_turn" if not state.get("error") else "error_node"
