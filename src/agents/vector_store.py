"""自研向量库：numpy 余弦相似度 + 磁盘持久化。

存储：向量矩阵 (n, dim) float32 + 元数据 JSON。检索为整库点积
（向量已归一化时余弦 = 点积），对当前量级（数千片段）足够快；
后续可平滑替换为 ANN（如 hnswlib/faiss），接口不变。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class SearchHit:
    index: int
    score: float
    text: str = ""
    file: str = ""
    heading: str = ""
    source: str = ""


@dataclass
class VectorStore:
    dim: int = 0
    backend_name: str = ""
    model_name: str = ""
    vectors: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), dtype=np.float32))
    metas: list[dict] = field(default_factory=list)

    # ---- 构建 ----
    def build(self, embeddings, metas: list[dict], backend_name: str, model_name: str = "") -> "VectorStore":
        embeddings = np.asarray(embeddings, dtype=np.float32)
        assert embeddings.ndim == 2
        self.dim = embeddings.shape[1]
        self.backend_name = backend_name
        self.model_name = model_name
        self.vectors = np.asarray(embeddings, dtype=np.float32)
        self.metas = list(metas)
        return self

    def add(self, embeddings: np.ndarray, metas: list[dict]) -> None:
        if self.dim == 0:
            self.dim = embeddings.shape[1]
        assert embeddings.shape[1] == self.dim
        self.vectors = np.vstack([self.vectors, np.asarray(embeddings, dtype=np.float32)]) if self.vectors.size else np.asarray(embeddings, dtype=np.float32)
        self.metas.extend(metas)

    @property
    def size(self) -> int:
        return len(self.metas)

    # ---- 检索 ----
    def search(self, query_vec: np.ndarray, top_k: int) -> list[SearchHit]:
        if self.size == 0 or self.dim == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32).reshape(1, -1)
        qn = np.linalg.norm(q)
        if qn == 0:
            return []
        q = q / qn
        # 向量已 L2 归一化时点积即余弦；再做一次防护性归一化
        norms = np.linalg.norm(self.vectors, axis=1)
        safe = np.where(norms > 0, norms, 1.0)
        scores = (self.vectors / safe[:, None]) @ q.T
        scores = scores.flatten()
        k = min(top_k, self.size)
        top_idx = np.argpartition(-scores, k - 1)[:k]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        hits: list[SearchHit] = []
        for i in top_idx.tolist():
            meta = self.metas[i]
            hits.append(
                SearchHit(
                    index=int(i),
                    score=float(scores[i]),
                    text=meta.get("text", ""),
                    file=meta.get("file", ""),
                    heading=meta.get("heading", ""),
                    source=meta.get("source", ""),
                )
            )
        return hits

    # ---- 持久化 ----
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path.with_suffix(".npy"), self.vectors)
        payload = {
            "dim": self.dim,
            "backend": self.backend_name,
            "model": self.model_name,
            "metas": self.metas,
        }
        (path.with_suffix(".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "VectorStore":
        vectors = np.load(path.with_suffix(".npy"))
        payload = json.loads((path.with_suffix(".json")).read_text(encoding="utf-8"))
        return cls(
            dim=payload["dim"],
            backend_name=payload.get("backend", ""),
            model_name=payload.get("model", ""),
            vectors=np.asarray(vectors, dtype=np.float32),
            metas=payload["metas"],
        )
