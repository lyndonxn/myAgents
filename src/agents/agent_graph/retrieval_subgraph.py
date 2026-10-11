"""检索子图（spec/08 LG-0.3）：把现有混合检索管线包装为可独立运行/评测的图组件。

节点与现有引擎阶段一一对应（薄封装，零检索逻辑复制，冻结引擎约束 C6）：
  vector_recall  → 向量召回（Retriever.store.search，查询向量由 embed_backend 生成）
  bm25_recall    → BM25 召回（Retriever.bm25.search）
  fuse           → 融合 + 来源质量调整（Retriever._fuse / _quality_adjust）
  expand_parents → 叶子→父块聚合（Retriever._leaves_to_parents）
  rerank         → 父块精排 + 结果组装（Retriever._rerank_parents + RetrievedChunk）

向量召回与 BM25 召回互不依赖，图中作为并行分支扇出、fuse 汇聚——这是图编排
对原串行管线的第一个结构性改进（两路召回可并发）。

已知限制（Phase 0，如实声明）：legacy 的 _hybrid_leaf_scores 在 multi_query=true
时先做 LLM 查询变体展开；图路径暂不含该分支（config 默认 false），一致性测试在
multi_query 关闭口径下对齐。错误处理遵循 C5：子图内节点失败写 error、下游透传、
不出子图——由主图条件边统一路由 error_node。
"""
from __future__ import annotations

from ..retriever import RetrievedChunk, Retriever
from .nodes import timed
from .state import GraphState


def _build_chunks(retriever: Retriever, candidates: list[dict], top_k: int) -> list[RetrievedChunk]:
    """候选 → RetrievedChunk（与 Retriever.retrieve 的组装循环逐字段一致）。"""
    results: list[RetrievedChunk] = []
    for rank, c in enumerate(candidates, start=1):
        parent = retriever.parents[c["parent_idx"]]
        results.append(
            RetrievedChunk(
                chunk=parent,
                final_score=float(c.get("final_score", c["score"])),
                rank=rank,
                leaf_text=c["leaf_text"],
                leaf_rank=int(c["leaf_rank"]),
            )
        )
        if len(results) >= top_k:
            break
    return results


def build_retrieval_subgraph(retriever: Retriever, metrics: list | None = None):
    """构建检索子图（编译后作为主图节点使用，随主图 checkpointer 管理——Phase 1）。

    metrics：build 级耗时持有者（不进图状态，见 nodes.timed 说明）。
    """
    from langgraph.graph import StateGraph, START, END

    def vector_recall(state: GraphState):
        if state.get("error"):
            return {}
        try:
            top_k = state.get("top_k") or retriever.config.top_k
            candidates = retriever._candidate_count(top_k)
            query = state.get("search_query") or state["question"]
            # 与 _hybrid_leaf_scores 相同的查询向量化口径
            qvec = retriever.embed_backend.embed_query(query) if retriever.embed_backend is not None else query
            return {"vec_hits": retriever.store.search(qvec, candidates)}
        except Exception as exc:  # noqa: BLE001 - C5
            return {"error": f"vector_recall: {type(exc).__name__}: {exc}"}

    def bm25_recall(state: GraphState):
        if state.get("error"):
            return {}
        try:
            top_k = state.get("top_k") or retriever.config.top_k
            candidates = retriever._candidate_count(top_k)
            query = state.get("search_query") or state["question"]
            return {"kw_hits": retriever.bm25.search(query, candidates)}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"bm25_recall: {type(exc).__name__}: {exc}"}

    def fuse(state: GraphState):
        if state.get("error"):
            return {}
        try:
            # 复用现有融合策略（RRF 默认 / weighted 可配）+ 来源质量调整
            fused = {
                idx: retriever._quality_adjust(idx, score)
                for idx, score in retriever._fuse(state.get("vec_hits") or [], state.get("kw_hits") or []).items()
            }
            return {"fused": fused}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"fuse: {type(exc).__name__}: {exc}"}

    def expand_parents(state: GraphState):
        if state.get("error"):
            return {}
        try:
            return {"candidates": retriever._leaves_to_parents(state.get("fused") or {})}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"expand_parents: {type(exc).__name__}: {exc}"}

    def rerank(state: GraphState):
        if state.get("error"):
            return {}
        try:
            top_k = state.get("top_k") or retriever.config.top_k
            query = state.get("search_query") or state["question"]
            candidates = retriever._rerank_parents(query, state.get("candidates") or [], top_k)
            return {"chunks": _build_chunks(retriever, candidates, top_k)}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"rerank: {type(exc).__name__}: {exc}"}

    subgraph = StateGraph(GraphState)
    subgraph.add_node("vector_recall", timed("vector_recall", vector_recall, metrics))
    subgraph.add_node("bm25_recall", timed("bm25_recall", bm25_recall, metrics))
    subgraph.add_node("fuse", timed("fuse", fuse, metrics))
    subgraph.add_node("expand_parents", timed("expand_parents", expand_parents, metrics))
    subgraph.add_node("rerank", timed("rerank", rerank, metrics))
    # 两路召回并行扇出（图编排的结构性收益），fuse 汇聚
    subgraph.add_edge(START, "vector_recall")
    subgraph.add_edge(START, "bm25_recall")
    subgraph.add_edge("vector_recall", "fuse")
    subgraph.add_edge("bm25_recall", "fuse")
    subgraph.add_edge("fuse", "expand_parents")
    subgraph.add_edge("expand_parents", "rerank")
    subgraph.add_edge("rerank", END)
    return subgraph.compile()
