"""LangGraph 图状态（spec/08 T-006/T-007）：单发与会话式问答的最小状态契约。

字段是对现有引擎产物的薄封装（不重定义检索结构，硬约束 C6 冻结引擎）：
- 检索中间产物沿用 Retriever 的既有类型（SearchHit / RetrievedChunk / 候选 dict）；
- 会话历史存 state["history"]（随 Checkpointer 持久化），不双写 legacy SessionMemory；
- node_metrics 使用 operator.add 归并：向量/BM25 两路召回并行执行，
  并行分支对同一键的写入必须有 reducer（否则 LangGraph 抛 InvalidUpdateError）。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from ..retriever import RetrievedChunk
from ..vector_store import SearchHit


def _first_error(current: str, new: str) -> str:
    """error 键归并（C5）：向量/BM25 并行分支同时失败时取首个错误，
    避免 InvalidUpdateError 让错误处理自身崩溃（T-008 回归实测抓出）。"""
    return current or new


class GraphState(TypedDict, total=False):
    """问答图的状态（total=False：全部字段可选，节点按需读写）。

    硬约束 C5（spec/08）：任何节点失败必须写入 error 并停止推进——
    下游节点见 error 直接透传（return {}），主图条件边统一路由 error_node 优雅终止，
    不产出半截结果。
    """

    # ---- 输入 ----
    question: str                 # 用户原始问题（合成/历史记录用它）
    search_query: str             # 实际检索查询（有历史时为改写后的独立查询）
    top_k: int | None

    # ---- 会话（LG-1.2/1.5：随 Checkpointer 持久化） ----
    history: list[dict]           # 已完成轮次 [{"q":…, "a":…}]，commit_turn 追加
    history_text: str             # 由 history 拼装的文本（供改写与合成）

    # ---- 检索子图中间产物 ----
    vec_hits: list[SearchHit]     # 向量召回（与 BM25 并行分支）
    kw_hits: list[SearchHit]      # BM25 召回
    fused: dict[int, float]       # 融合（RRF/加权）+ 来源质量调整后的叶子分数
    candidates: list[dict]        # 叶子→父块聚合候选（含精排分）
    chunks: list[RetrievedChunk]  # 最终检索结果（父块，供生成）

    # ---- 澄清中断（LG-1.3） ----
    pending_requery: bool         # 澄清后需要用新查询重跑检索子图
    clarify_round: int            # 已发生的澄清轮数（上限 1，防循环）

    # ---- 生成与校验 ----
    plan: Any                     # 展示用 Plan（单步检索，与 legacy 答案结构对齐）
    step_result: Any              # 合成 StepResult（output 与 legacy 工具输出同构）
    sources: list[str]
    sources_detail: list[dict]
    draft: str                    # 合成原文（引用校验前后共用此键流转）
    citations_valid: int
    citations_invalid: int

    # ---- 计数与指标 ----
    llm_call_count: int           # 检索/改写阶段的 LLM 调用数（合成由 runner 按 step 计 1）
    error: Annotated[str, _first_error]
    node_metrics: Annotated[list[dict], operator.add]  # 各节点耗时（Phase 2 接 benchmark）
