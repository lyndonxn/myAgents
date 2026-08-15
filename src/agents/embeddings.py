"""Embedding 后端。

两种实现，统一接口：
- LocalEmbeddingBackend：sentence-transformers 加载本地中文向量模型
  （默认 BAAI/bge-small-zh-v1.5，512 维）。质量最好、离线可用。
- TfidfHashEmbeddingBackend：内置兜底。jieba 分词 + 词频/逆文档频 +
  特征哈希（hash trick）映射到固定维度。零外部依赖，无需下载模型。

backend="auto" 时：装好 sentence-transformers 用 local，否则 tfidf。
"""
from __future__ import annotations

import os

# 必须在导入 sentence_transformers/huggingface_hub 之前设置（hub 在 import 时读取该常量）。
# 直连 huggingface.co 不可达的网络自动走 hf-mirror.com 镜像。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from dataclasses import dataclass  # noqa: E402

import numpy as np  # noqa: E402

try:  # 可选依赖
    from sentence_transformers import SentenceTransformer  # type: ignore

    _HAS_ST = True
except Exception:  # noqa: BLE001 - 任何导入失败都视为不可用
    _HAS_ST = False


@dataclass
class EmbeddingResult:
    vectors: np.ndarray          # (n, dim) float32
    backend_name: str
    dim: int
    model_name: str = ""


class EmbeddingBackend:
    name = "base"

    def embed_texts(self, texts: list[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def embed_query(self, query: str) -> np.ndarray:  # pragma: no cover
        return self.embed_texts([query])[0]

    @property
    def dim(self) -> int:  # pragma: no cover
        raise NotImplementedError


class LocalEmbeddingBackend(EmbeddingBackend):
    name = "local"

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        if not _HAS_ST:
            raise RuntimeError(
                "sentence-transformers 未安装。请执行: "
                "pip install 'sentence-transformers>=3.0'，"
                "或设置 embedding.backend=tfidf 使用内置向量。"
            )
        self._model = self._load_model(model_name)
        self._model_name = model_name

    @staticmethod
    def _load_model(model_name: str) -> SentenceTransformer:
        """优先离线加载缓存；无缓存时用国内镜像下载（hf-mirror.com）。

        模型已缓存时绝不联网校验（huggingface.co 在部分网络不可达会卡死）。
        """
        try:
            return SentenceTransformer(model_name, local_files_only=True)
        except Exception:
            # 未缓存：走镜像下载（HF_ENDPOINT 已在模块导入前设置）
            return SentenceTransformer(model_name)

    @property
    def dim(self) -> int:
        getter = getattr(self._model, "get_embedding_dimension", None) or getattr(
            self._model, "get_sentence_embedding_dimension"
        )
        return int(getter())

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # bge 系列建议查询侧加指令前缀；检索我们统一不加（双编码器已足够）
        vecs = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return np.asarray(vecs, dtype=np.float32)


class TfidfHashEmbeddingBackend(EmbeddingBackend):
    """jieba 分词 + TF-IDF 权重 + 特征哈希 → 固定维度稀疏向量（L2 归一化）。"""

    name = "tfidf"

    def __init__(self, hash_dim: int = 1024, min_df: int = 1):
        import jieba  # 延迟导入

        self._jieba = jieba
        self._dim = int(hash_dim)
        self._min_df = min_df
        self._doc_freq: dict[int, int] = {}   # 特征 -> 出现文档数
        self._n_docs = 0
        self._fitted = False

    @property
    def dim(self) -> int:
        return self._dim

    def _tokenize(self, text: str) -> list[str]:
        toks = [t.strip() for t in self._jieba.cut(text) if t.strip()]
        # 补充字符 bigram，缓解 jieba 词典覆盖不足
        toks += [text[i : i + 2] for i in range(len(text) - 1) if not text[i].isspace()]
        return toks

    def _hash_token(self, token: str) -> int:
        # 与语言无关的稳定哈希（避免 Python hash() 的随机化）
        h = 0
        for ch in token:
            h = (h * 31 + ord(ch)) & 0xFFFFFFFF
        return h % self._dim

    def fit(self, docs: list[str]) -> "TfidfHashEmbeddingBackend":
        """在语料上统计文档频率，用于 IDF 加权（查询向量与文档同分布）。"""
        doc_freq: dict[int, set[int]] = {}
        for doc in docs:
            seen: set[int] = set()
            for tok in self._tokenize(doc):
                seen.add(self._hash_token(tok))
            for f in seen:
                doc_freq.setdefault(f, set()).add(1)
        self._doc_freq = {f: len(s) for f, s in doc_freq.items()}
        self._n_docs = len(docs)
        self._fitted = True
        return self

    def _vector(self, text: str) -> np.ndarray:
        vec = np.zeros(self._dim, dtype=np.float32)
        counts: dict[int, int] = {}
        for tok in self._tokenize(text):
            f = self._hash_token(tok)
            counts[f] = counts.get(f, 0) + 1
        n = sum(counts.values()) or 1
        for f, c in counts.items():
            tf = c / n
            if self._fitted:
                df = self._doc_freq.get(f, 0)
                idf = np.log((self._n_docs + 1) / (df + 1)) + 1.0 if df >= self._min_df else 0.0
            else:
                idf = 1.0
            vec[f] += tf * idf
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self._vector(t) for t in texts]).astype(np.float32)


def build_backend(config) -> EmbeddingBackend:
    """按配置构建后端。backend=auto 时优先 local。"""
    mode = config.embedding_backend
    if mode in ("local", "auto"):
        try:
            backend = LocalEmbeddingBackend(config.embedding_model)
            backend._source = "local"  # type: ignore[attr-defined]
            return backend
        except RuntimeError:
            if mode == "local":
                raise
    if not _HAS_ST:
        import warnings

        warnings.warn("未安装 sentence-transformers，使用内置 TF-IDF 哈希向量（质量较低）。", stacklevel=2)
    return TfidfHashEmbeddingBackend(hash_dim=config.hash_dim)
