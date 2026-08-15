"""自研 BM25 关键词检索（jieba 分词）。

标准 BM25：score(q,d) = Σ_t IDF(t) * f(t,d)*(k1+1) / (f(t,d) + k1*(1-b+b*|d|/avgdl))

分词：jieba 精确模式 + 字符 bigram 补充（覆盖词典外中文词）。
检索结果与 VectorStore 统一为 SearchHit 结构，便于融合。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .vector_store import SearchHit


@dataclass
class BM25Index:
    docs: list[str] = field(default_factory=list)
    postings: dict[str, list[tuple[int, int]]] = field(default_factory=dict)  # term -> [(doc_idx, tf)]
    doc_len: list[int] = field(default_factory=list)
    avgdl: float = 0.0
    idf: dict[str, float] = field(default_factory=dict)
    k1: float = 1.5
    b: float = 0.75
    _jieba = None

    # ---- 分词 ----
    @classmethod
    def _get_jieba(cls):
        if cls._jieba is None:
            import jieba

            cls._jieba = jieba
        return cls._jieba

    @classmethod
    def tokenize(cls, text: str) -> list[str]:
        jb = cls._get_jieba()
        toks = [t.strip() for t in jb.cut(text) if t.strip() and not t.isspace()]
        toks += [text[i : i + 2] for i in range(len(text) - 1) if not text[i].isspace()]
        return toks

    # ---- 构建 ----
    def build(self, docs: list[str]) -> "BM25Index":
        self.docs = list(docs)
        self.doc_len = []
        term_doc_count: dict[str, int] = {}
        postings: dict[str, list[tuple[int, int]]] = {}
        for doc_idx, doc in enumerate(self.docs):
            tf_map: dict[str, int] = {}
            for tok in self.tokenize(doc):
                tf_map[tok] = tf_map.get(tok, 0) + 1
            self.doc_len.append(len(doc))
            for tok, tf in tf_map.items():
                postings.setdefault(tok, []).append((doc_idx, tf))
                term_doc_count[tok] = term_doc_count.get(tok, 0) + 1
        self.postings = postings
        n = len(self.docs)
        self.avgdl = sum(self.doc_len) / n if n else 0.0
        # BM25+ 风格的 IDF（加平滑，避免负值）
        self.idf = {tok: math.log(1 + (n - df + 0.5) / (df + 0.5)) for tok, df in term_doc_count.items()}
        return self

    # ---- 检索 ----
    def search(self, query: str, top_k: int) -> list[SearchHit]:
        if not self.docs or not self.avgdl:
            return []
        scores = [0.0] * len(self.docs)
        for tok in set(self.tokenize(query)):
            idf = self.idf.get(tok, 0.0)
            if idf <= 0:
                continue
            for doc_idx, tf in self.postings.get(tok, []):
                dl = self.doc_len[doc_idx]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                scores[doc_idx] += idf * (tf * (self.k1 + 1)) / denom
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        hits: list[SearchHit] = []
        for i in ranked:
            if scores[i] <= 0:
                continue
            hits.append(SearchHit(index=int(i), score=float(scores[i]), text=self.docs[i]))
        return hits
