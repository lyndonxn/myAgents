"""配置加载：config.yaml + .env 环境变量 + 运行时覆盖（data/runtime.json）。

优先级（高→低）：运行时覆盖 runtime.json > 环境变量 > config.yaml 默认值。
runtime.json 由 Web 前端「设置」面板写入，持久化跨重启生效。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
RUNTIME_PATH = PROJECT_ROOT / "data" / "runtime.json"


def _load_dotenv(path: Path = ENV_PATH) -> None:
    """极简 .env 加载：KEY=VALUE 每行，忽略注释与空行，不覆盖已有环境变量。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_runtime(raw: dict) -> dict:
    """读取运行时覆盖并合并进配置。"""
    if RUNTIME_PATH.exists():
        try:
            rt = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
            if isinstance(rt, dict):
                _deep_merge(raw, rt)
        except (json.JSONDecodeError, OSError):
            pass
    return raw


def save_runtime(overrides: dict) -> None:
    """保存运行时配置覆盖（持久化，跨重启生效）。"""
    current: dict = {}
    if RUNTIME_PATH.exists():
        try:
            current = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            current = {}
    _deep_merge(current, overrides)
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


class Config:
    """类型安全的配置门面。"""

    def __init__(self, raw: dict[str, Any]):
        self._raw = raw

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # ---- 便捷属性 ----
    @property
    def kb_path(self) -> str:
        return os.path.expanduser(self.get("kb_path", ""))

    @property
    def data_dir(self) -> Path:
        raw = self.get("data_dir", "data")
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def chunk_min_chars(self) -> int:
        return int(self.get("chunking.min_chars", 80))

    @property
    def chunk_max_chars(self) -> int:
        return int(self.get("chunking.max_chars", 1200))

    @property
    def chunk_overlap(self) -> int:
        return int(self.get("chunking.overlap", 100))

    @property
    def chunk_exclude_dirs(self) -> list[str]:
        return list(self.get("chunking.exclude_dirs", []))

    @property
    def chunk_exclude_files(self) -> list[str]:
        return list(self.get("chunking.exclude_files", []))

    @property
    def embedding_backend(self) -> str:
        return str(self.get("embedding.backend", "auto"))

    @property
    def embedding_model(self) -> str:
        return str(self.get("embedding.model", "BAAI/bge-small-zh-v1.5"))

    @property
    def hash_dim(self) -> int:
        return int(self.get("embedding.hash_dim", 1024))

    @property
    def top_k(self) -> int:
        return int(self.get("retrieval.top_k", 6))

    @property
    def vector_weight(self) -> float:
        return float(self.get("retrieval.vector_weight", 0.7))

    @property
    def keyword_weight(self) -> float:
        return float(self.get("retrieval.keyword_weight", 0.3))

    @property
    def rerank_mode(self) -> str:
        """off | auto | cross_encoder | llm。auto 优先 cross_encoder。"""
        raw = self.get("retrieval.rerank", "off")
        if isinstance(raw, bool):
            return "cross_encoder" if raw else "off"
        return str(raw).lower()

    @property
    def rerank(self) -> bool:
        """向后兼容别名。"""
        return self.rerank_mode != "off"

    @property
    def rerank_candidates(self) -> int:
        return int(self.get("retrieval.rerank_candidates", 12))

    @property
    def reranker_model(self) -> str:
        return str(self.get("retrieval.reranker_model", "BAAI/bge-reranker-base"))

    @property
    def multi_query(self) -> bool:
        return bool(self.get("retrieval.multi_query", False))

    @property
    def fusion_mode(self) -> str:
        """rrf | weighted"""
        return str(self.get("retrieval.fusion_mode", "rrf")).lower()

    @property
    def leaf_max_chars(self) -> int:
        return int(self.get("chunking.leaf_max_chars", 320))

    @property
    def leaf_min_chars(self) -> int:
        return int(self.get("chunking.leaf_min_chars", 24))

    @property
    def contextual_augment(self) -> bool:
        return bool(self.get("chunking.contextual_augment", False))

    @property
    def llm_base_url(self) -> str:
        return os.environ.get("DEEPSEEK_BASE_URL", str(self.get("llm.base_url", "https://api.deepseek.com")))

    @property
    def llm_api_key(self) -> str:
        env = os.environ.get("DEEPSEEK_API_KEY", "")
        if env:
            return env
        return str(self.get("llm.api_key", ""))

    @property
    def llm_chat_model(self) -> str:
        return str(self.get("llm.chat_model", "deepseek-chat"))

    @property
    def llm_temperature(self) -> float:
        return float(self.get("llm.temperature", 0.3))

    @property
    def llm_max_tokens(self) -> int:
        return int(self.get("llm.max_tokens", 2048))

    @property
    def llm_timeout(self) -> int:
        return int(self.get("llm.timeout", 60))

    @property
    def llm_max_retries(self) -> int:
        return int(self.get("llm.max_retries", 3))

    @property
    def llm_json_repair_rounds(self) -> int:
        """chat_json 解析失败后的修复轮数（0=关闭）。"""
        return int(self.get("llm.json_repair_rounds", 1))

    # ---- 视觉模型（图片搜索，OpenAI 兼容，如 GLM-4V / Qwen-VL / GPT-4o） ----
    @property
    def vision_model(self) -> str:
        return str(self.get("vision.model", ""))

    @property
    def vision_base_url(self) -> str:
        return str(self.get("vision.base_url", self.llm_base_url))

    @property
    def vision_api_key(self) -> str:
        return str(self.get("vision.api_key", "")).strip() or self.llm_api_key

    @property
    def vision_configured(self) -> bool:
        return bool(self.vision_model)

    @property
    def planner_model(self) -> str:
        return str(self.get("planner.model", self.llm_chat_model))

    @property
    def planner_temperature(self) -> float:
        return float(self.get("planner.temperature", 0.2))

    @property
    def planner_max_steps(self) -> int:
        return int(self.get("planner.max_steps", 5))

    @property
    def planner_fallback_direct(self) -> bool:
        return bool(self.get("planner.fallback_direct", True))

    @property
    def planner_reflect(self) -> bool:
        """首轮执行后是否进行反思重规划（S2 ReAct 迭代总开关）。"""
        return bool(self.get("planner.reflect", True))

    @property
    def planner_max_reflections(self) -> int:
        """反思重规划的最多轮数（0=关闭反思重规划）。"""
        return int(self.get("planner.max_reflections", 1))

    @property
    def search_default_top_k(self) -> int:
        return int(self.get("tools.search_default_top_k", 5))

    @property
    def tools_max_retries(self) -> int:
        """工具步骤失败后的重试次数（总尝试次数 = 1 + 该值）。"""
        return int(self.get("tools.max_retries", 1))

    @property
    def kb_fallback_web(self) -> bool:
        """知识库检索重试耗尽仍失败时，是否降级用 Web 搜索。"""
        return bool(self.get("tools.kb_fallback_web", True))

    @property
    def tools_enabled(self) -> dict[str, bool]:
        return {
            "calculator": bool(self.get("tools.calculator_enabled", True)),
            "time": bool(self.get("tools.time_enabled", True)),
            "topics": bool(self.get("tools.topics_enabled", True)),
            "web_search": bool(self.get("tools.web_search_enabled", True)),
        }

    @property
    def web_search_timeout(self) -> float:
        return float(self.get("tools.web_search.timeout", 15.0))

    @property
    def web_search_cache_ttl(self) -> float:
        return float(self.get("tools.web_search.cache_ttl", 300.0))


def load_config() -> Config:
    _load_dotenv()
    raw: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            raw = _deep_merge(raw, loaded)
    # 运行时覆盖（Web 设置面板写入）
    raw = _load_runtime(raw)
    # 环境变量覆盖
    if os.environ.get("DEEPSEEK_API_KEY"):
        raw.setdefault("llm", {})["api_key_env"] = "DEEPSEEK_API_KEY"
    return Config(raw)
