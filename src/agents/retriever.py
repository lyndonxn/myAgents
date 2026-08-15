"""混合检索器（生产级策略）：叶子索引 → 混合召回 → 精排 → 父块扩展。

流程（大厂两阶段检索架构）：
1. 召回：向量（自研库）+ BM25 在【叶子】上混合召回，融合方式可选：
   - RRF（Reciprocal Rank Fusion，默认，业界标准，对异构分数鲁棒）
   - 加权归一化和（旧方案）
   可选 Multi-Query：LLM 生成查询变体，各自召回后按 RRF 合并。
2. 精排：Cross-Encoder（bge-reranker）对候选叶子重排（两阶段"召回 Top-N → 精排 Top-K"）。
3. 父块扩展：命中叶子映射回父章节，去重、按最优叶子分排序，供生成使用完整上下文。
4. 近重复去重：滑窗重叠产生的近似重复父块合并，缓解 Lost-in-the-Middle。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .bm25 import BM25Index
from .chunking import Chunk, Leaf
from .vector_store import SearchHit, VectorStore

RRF_K = 60.0


def _minmax(scores: list[float]) -> list[float]:
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [0.5] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


@dataclass
class RetrievedChunk:
    """检索结果：父块 + 命中的叶子信息。"""

    chunk: Chunk
    vector_score: float = 0.0
    keyword_score: float = 0.0
    final_score: float = 0.0
    rank: int = 0
    leaf_text: str = ""       # 命中的叶子片段（精确匹配点）
    leaf_rank: int = 0        # 叶子在精排后的名次

    @property
    def file(self) -> str:
        return self.chunk.file

    @property
    def source(self) -> str:
        return self.chunk.source or self.chunk.file

    @property
    def text(self) -> str:
        return self.chunk.text


def _rrf_merge(*rankings: list[SearchHit]) -> dict[int, float]:
    """RRF 融合多路排名：score(d) = Σ 1/(k + rank)。"""
    merged: dict[int, float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            merged[hit.index] = merged.get(hit.index, 0.0) + 1.0 / (RRF_K + rank)
    return merged


class Retriever:
    def __init__(self, vector_store: VectorStore, bm25: BM25Index, parents: list[Chunk], leaves: list[Leaf], config, embed_backend=None, contexts: dict[int, str] | None = None):
        self.store = vector_store
        self.bm25 = bm25
        self.parents = parents
        self.leaves = leaves
        self.config = config
        self.embed_backend = embed_backend
        self.contexts = contexts or {}
        self.reranker = None
        self._llm = None

    # ---------------- 召回（叶子级） ----------------

    def _candidate_count(self, top_k: int) -> int:
        """召回候选池宽度：始终宽于最终 top_k（召回优先，选择后置）。

        无论是否精排，都先召回 rerank_candidates 个叶子再收窄，
        避免窄候选池让噪声片段挤掉真相关章节。
        """
        return max(top_k * 2, int(self.config.get("retrieval.rerank_candidates", 24)))

    def _fuse(self, vec_hits: list, kw_hits: list) -> dict[int, float]:
        """向量/BM25 两路排名融合：RRF（默认）或加权归一化。"""
        if self.config.fusion_mode == "weighted":
            vw = float(self.config.get("retrieval.vector_weight", 0.7))
            kw = float(self.config.get("retrieval.keyword_weight", 0.3))
            merged: dict[int, float] = {}
            for h in vec_hits:
                merged.setdefault(h.index, 0.0)
            for h in kw_hits:
                merged.setdefault(h.index, 0.0)
            idxs = list(merged.keys())
            v_scores = _minmax([next((h.score for h in vec_hits if h.index == i), 0.0) for i in idxs])
            k_scores = _minmax([next((h.score for h in kw_hits if h.index == i), 0.0) for i in idxs])
            return {i: vw * v + kw * k for i, v, k in zip(idxs, v_scores, k_scores)}
        return _rrf_merge(vec_hits, kw_hits)

    def _hybrid_leaf_scores(self, query: str, candidates: int) -> dict[int, float]:
        """多查询展开（可选）+ 向量/BM25 混合 → 叶子 RRF 分数。"""
        qvec = query
        if self.embed_backend is not None:
            qvec = self.embed_backend.embed_query(query)

        queries = [query]
        if self.config.get("retrieval.multi_query", False) and self._llm is not None:
            queries = self._expand_queries(query)

        merged: dict[int, float] = {}
        for q in queries:
            qv = qvec if q == query else self.embed_backend.embed_query(q)
            vec_hits = self.store.search(qv, candidates)
            kw_hits = self.bm25.search(q, candidates)
            for idx, score in self._fuse(vec_hits, kw_hits).items():
                score = self._quality_adjust(idx, score)
                merged[idx] = merged.get(idx, 0.0) + score
        return merged

    # 噪声来源标记：转录/字幕/视频信息等低质量章节（生产 RAG 的来源质量过滤）
    NOISE_HEADING_MARKERS = ("原始字幕", "字幕", "视频信息", "图片来源")

    def _quality_adjust(self, leaf_idx: int, score: float) -> float:
        """按来源质量调整分数：噪声章节降权，高质量章节轻微加分。"""
        penalty = float(self.config.get("retrieval.noise_penalty", 0.0))
        if penalty > 0:
            heading = self.leaves[leaf_idx].heading
            if any(m in heading for m in self.NOISE_HEADING_MARKERS):
                score *= penalty
        boost = float(self.config.get("retrieval.structured_boost", 0.0))
        if boost > 0 and self.leaves[leaf_idx].heading:
            # 有明确标题路径的结构化章节轻微加分
            score += boost
        return score

    def _expand_queries(self, query: str) -> list[str]:
        """LLM 生成 2 个查询变体（覆盖同义改写与视角补充）。"""
        prompt = (
            "你是查询扩展器。给定检索查询，生成 2 个语义等价但措辞不同的变体查询，"
            "帮助召回更多相关内容。直接输出 JSON 数组，如 [\"变体1\", \"变体2\"]，不要输出其他内容。\n"
            f"查询：{query}"
        )
        try:
            obj = self._llm.chat_json([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=200)
            if isinstance(obj, list):
                variants = [str(v).strip() for v in obj if isinstance(v, str) and v.strip()]
                return [query, *variants[:2]]
        except Exception:  # noqa: BLE001
            pass
        return [query]

    # ---------------- 父块聚合与精排 ----------------

    def _leaves_to_parents(self, leaf_scores: dict[int, float]) -> list[dict]:
        """叶子 → 父块候选：同一父块取最优叶子，近重复父块合并。"""
        best: dict[int, dict] = {}  # parent_idx -> {score, leaf_rank, leaf_text, leaf_search}
        ordered_leaves = sorted(leaf_scores, key=leaf_scores.get, reverse=True)
        for leaf_rank, leaf_idx in enumerate(ordered_leaves, start=1):
            score = leaf_scores[leaf_idx]
            leaf = self.leaves[leaf_idx]
            if leaf.parent_idx not in best:
                best[leaf.parent_idx] = {
                    "score": score,
                    "leaf_rank": leaf_rank,
                    "leaf_text": leaf.text,
                    "leaf_search": self._leaf_search_text(leaf)[:400],
                }

        ordered = sorted(best.items(), key=lambda kv: kv[1]["score"], reverse=True)
        candidates: list[dict] = []
        seen_texts: list[str] = []
        for parent_idx, info in ordered:
            pt = self.parents[parent_idx].text.strip()
            if any(pt in st or st in pt for st in seen_texts):  # 近重复去重
                continue
            seen_texts.append(pt)
            candidates.append({"parent_idx": parent_idx, **info})
        return candidates

    def _rerank_parents(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        """父块级精排：用代表性叶子的短文本做 Cross-Encoder/LLM 精排。

        - 短文本避免长父块截断稀释信号；聚合仍按父块
        - 最终分数 = blend * 精排分 + (1-blend) * 召回分（min-max 归一化），
          避免精排器关键词重叠噪声完全推翻召回排序
        """
        if not candidates:
            return candidates
        pool = candidates[: self._candidate_count(top_k)]
        texts = [c["leaf_search"] for c in pool]
        blend = float(self.config.get("retrieval.rerank_blend", 0.7))

        rerank_scores: list[float] | None = None
        try:
            if self.reranker is not None:
                rerank_scores = [s for _, s in self.reranker.rerank(query, texts, len(pool))]
        except Exception:  # noqa: BLE001
            pass
        mode = str(self.config.get("retrieval.rerank", "off")).lower()
        if rerank_scores is None and mode == "llm" and self._llm is not None:
            order = self._llm_rerank(query, texts, len(pool))
            if order is not None:
                n = len(pool)
                rerank_scores = [float(n - order.index(i)) for i in range(n)]

        if rerank_scores is None:
            for c in pool[:top_k]:
                c["final_score"] = float(c["score"])
            return pool[:top_k]

        # 归一化融合
        recall_norm = _minmax([c["score"] for c in pool])
        rerank_norm = _minmax(rerank_scores)
        for c, rn, cn in zip(pool, recall_norm, rerank_norm):
            c["final_score"] = blend * cn + (1.0 - blend) * rn
        pool.sort(key=lambda c: c["final_score"], reverse=True)
        return pool[:top_k]

    def _llm_rerank(self, query: str, texts: list[str], top_k: int) -> list[int] | None:
        """LLM 精排候选父块（config rerank=llm 时）。"""
        candidates = [f"[{i}] {t[:300]}" for i, t in enumerate(texts)]
        prompt = (
            "你是检索重排器。给定用户问题与候选片段，选出最相关的 "
            f"{top_k} 个，按相关度从高到低输出片段编号（JSON 数组）。\n"
            f"问题：{query}\n候选：\n" + "\n".join(candidates)
        )
        try:
            out = self._llm.chat_json([{"role": "user", "content": prompt}])
            order = [int(x) for x in out] if isinstance(out, list) else json.loads(str(out))
        except Exception:  # noqa: BLE001
            return None
        valid = [i for i in order if 0 <= i < len(texts)]
        rest = [i for i in range(len(texts)) if i not in valid]
        return [i for i in valid + rest][:top_k]

    # ---------------- 对外接口 ----------------

    def retrieve(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
        top_k = top_k or self.config.top_k
        leaf_scores = self._hybrid_leaf_scores(query, self._candidate_count(top_k))
        if not leaf_scores:
            return []
        candidates = self._leaves_to_parents(leaf_scores)
        candidates = self._rerank_parents(query, candidates, top_k)
        results: list[RetrievedChunk] = []
        for rank, c in enumerate(candidates, start=1):
            parent = self.parents[c["parent_idx"]]
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

    # ---------------- 辅助 ----------------

    def _leaf_search_text(self, leaf: Leaf) -> str:
        ctx = self.contexts.get(leaf.parent_idx, "")
        parts = [p for p in (ctx, leaf.heading, leaf.text) if p]
        return "\n".join(parts)

    def attach_llm(self, llm) -> "Retriever":
        self._llm = llm
        return self

    def attach_reranker(self, reranker) -> "Retriever":
        self.reranker = reranker
        return self
