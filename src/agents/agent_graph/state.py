"""LangGraph 图状态（spec/08 T-006 Phase 0）：单发问答的最小状态契约。

字段是对现有引擎产物的薄封装（不重定义检索结构，硬约束 C6 冻结引擎）：
- 检索中间产物沿用 Retriever 的既有类型（SearchHit / RetrievedChunk / 候选 dict）；
- Phase 0 为内存运行（无 checkpointer），Phase 1 引入 SqliteSaver 时再评估序列化。

node_metrics 使用 operator.add 归并：向量/BM25 两路召回在图中并行执行，
并行分支对同一键的写入必须有 reducer（否则 LangGraph 抛 InvalidUpdateError）。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from ..retriever import RetrievedChunk
from ..vector_store import SearchHit


class GraphState(TypedDict, total=False):
    """单发问答图的状态（total=False：全部字段可选，节点按需读写）。

    硬约束 C5（spec/08）：任何节点失败必须写入 error 并停止推进——
    下游节点见 error 直接透传（return {}），主图条件边统一路由 error_node 优雅终止，
    不产出半截结果。
    """

    # ---- 输入 ----
    question: str
    top_k: int | None

    # ---- 检索子图中间产物 ----
    vec_hits: list[SearchHit]      # 向量召回（与 BM25 并行分支）
    kw_hits: list[SearchHit]       # BM25 召回
    fused: dict[int, float]        # 融合（RRF/加权）+ 来源质量调整后的叶子分数
    candidates: list[dict]         # 叶子→父块聚合候选（含精排分）
    chunks: list[RetrievedChunk]   # 最终检索结果（父块，供生成）

    # ---- 生成与校验 ----
    plan: Any                      # 展示用 Plan（单步检索，与 legacy 答案结构对齐）
    step_result: Any               # 合成 StepResult（output 与 legacy 工具输出同构）
    sources: list[str]
    sources_detail: list[dict]
    draft: str                     # 合成原文（引用校验前/后共用此键流转）
    citations_valid: int
    citations_invalid: int

    # ---- 错误与指标 ----
    error: str
    node_metrics: Annotated[list[dict], operator.add]  # 各节点耗时（Phase 2 接 benchmark）
