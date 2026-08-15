"""Cross-Encoder 重排器（两阶段检索的"精排"阶段）。

- 默认模型 BAAI/bge-reranker-base（中文场景表现好，约 1.1GB，可配置换 small/v2-m3）
- 加载策略与 embeddings 一致：优先离线缓存，缺失时走 hf-mirror 下载
- 惰性加载：模型不可用时返回 None，由检索器优雅回退到"无重排"
"""
from __future__ import annotations

import os

# 必须在导入 sentence_transformers/huggingface_hub 之前设置：
# huggingface_hub 在 import 时读取 HF_ENDPOINT 常量，之后设置无效。
# 直连 huggingface.co 不可达的网络自动走 hf-mirror.com 镜像。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

try:
    from sentence_transformers import CrossEncoder  # type: ignore

    _HAS_CE = True
except Exception:  # noqa: BLE001
    _HAS_CE = False


class CrossEncoderReranker:
    def __init__(self, model_name: str = "BAAI/bge-reranker-base", max_length: int = 512):
        if not _HAS_CE:
            raise RuntimeError("sentence-transformers 未安装，无法使用 Cross-Encoder 重排")
        self._model = self._load(model_name)
        self._model_name = model_name
        self._max_length = max_length

    @staticmethod
    def _load(model_name: str):
        """加载策略：
        1. model_name 是本地目录（含权重文件）→ 直接加载
        2. HF 缓存已有 → 离线加载
        3. 都没有 → 走镜像下载
        """
        import os

        if os.path.isdir(model_name):
            return CrossEncoder(model_name, max_length=512)
        try:
            return CrossEncoder(model_name, max_length=512, local_files_only=True)
        except Exception:
            # 未缓存：走镜像下载（HF_ENDPOINT 已在模块导入前设置）
            return CrossEncoder(model_name, max_length=512)

    def rerank(self, query: str, texts: list[str], top_k: int) -> list[tuple[int, float]]:
        """返回 [(idx, score)]，按相关度降序。texts 为候选文本列表。"""
        if not texts:
            return []
        pairs = [[query, t] for t in texts]
        scores = self._model.predict(pairs, show_progress_bar=False)
        ranked = sorted(range(len(scores)), key=lambda i: float(scores[i]), reverse=True)
        return [(i, float(scores[i])) for i in ranked[:top_k]]

    @property
    def model_name(self) -> str:
        return self._model_name
