"""主图节点（spec/08 LG-0.4/0.5）：入口路由、合成、引用校验、统一错误出口。

复用边界（薄封装，零提示词复制）：
- 合成走 Agent._synthesize（证据压缩/Prefix Caching 友好 prompt 全部继承）；
- 检索步骤输出构造复用 tools._fmt_hit / _hit_detail，与 legacy 工具输出同构，
  使 _collect_source_info / 前端证据卡无需任何改动；
- 引用校验复用 citations.validate_citations（S3）。

错误处理遵循 C5：节点失败写 error 字段并停止推进（下游见 error 透传），
主图条件边将其路由到 error_node —— 不复刻参考项目"置 error 后继续走"的反模式。
"""
from __future__ import annotations

import functools
import time

from ..agent import _emit_stage
from ..citations import validate_citations
from ..executor import StepResult
from ..logger import get_logger
from ..planner import Plan, PlanStep
from ..tools import _fmt_hit, _hit_detail

LOG = get_logger("agent_graph")

# 与 Agent.ask 的 miss_hint 保持一致（Phase 1 抽公共常量）：
# 知识库零命中且未联网时，约束合成按「未在知识库内」作答并询问是否联网。
_KB_MISS_HINT = (
    "\n\n（系统提示：本次知识库检索未命中任何相关片段，且用户尚未同意联网搜索。"
    "请按系统规则回答：开头明确「未在知识库内找到相关内容」，"
    "并询问用户是否需要联网搜索——回复「联网搜索」即可。不要编造、不要硬凑答案。）"
)


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

def make_route_entry(on_stage=None):
    """入口节点：问题规范化 + 空问题拦截（错误走 error_node，不进检索）。"""

    def route_entry(state):
        question = (state.get("question") or "").strip()
        if not question:
            return {"error": "问题为空，无法执行问答"}
        _emit_stage(on_stage, "检索知识库")
        return {"question": question}

    return route_entry


def make_generate_node(agent, on_stage=None):
    """合成节点：把检索结果组装为与 legacy 工具同构的单步输出，复用 _synthesize。"""

    def generate(state):
        if state.get("error"):
            return {}
        _emit_stage(on_stage, "生成答案")
        try:
            chunks = state.get("chunks") or []
            question = state.get("question", "")
            lines = [f"针对「{question}」检索到 {len(chunks)} 条相关片段："]
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
            step = StepResult(
                step_id=1,
                action="search_knowledge_base",
                input={"query": question},
                output=output,
                ok=True,
            )
            plan = Plan(
                reasoning="LangGraph 图编排（Phase 0）：单步检索→合成→引用校验",
                plan_summary="LangGraph：检索→生成→引用校验",
                steps=[PlanStep(action="search_knowledge_base", input={"query": question}, step_id=1)],
            )
            # 知识库零命中提示与 legacy ask 口径一致（联网决策属 Phase 1 会话编排）
            hint = _KB_MISS_HINT if not chunks else ""
            draft = agent._synthesize(question, plan, [step], history_text="", extra_hint=hint)
            return {
                "draft": draft,
                "sources": sources,
                "sources_detail": sources_detail,
                "step_result": step,
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
    """末节点路由：校验通过 → 结束；有 error → error_node。"""
    return "__end__" if not state.get("error") else "error_node"
