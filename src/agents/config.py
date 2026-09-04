"""配置加载：config.yaml + .env 环境变量 + 运行时覆盖（data/runtime.json）。

优先级（高→低）：运行时覆盖 runtime.json > 环境变量 > config.yaml 默认值。
runtime.json 由 Web 前端「设置」面板写入，持久化跨重启生效。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import yaml

from .logger import get_logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
RUNTIME_PATH = PROJECT_ROOT / "data" / "runtime.json"

LOG = get_logger("config")

# G3/P0-3 数据外发默认关闭：这三个开关控制"数据是否外发（联网检索）或持久化（长期/实体记忆）"，
# 默认全部 false，用户在 UI 或 runtime.json 显式开启后才启用。
_EGRESS_DEFAULT_KEYS: tuple[str, ...] = (
    "tools.kb_fallback_web",
    "memory.long_term_enabled",
    "memory.entities_enabled",
)
# 迁移提示只提示一次/进程（load_config 可能被多次调用）
_migration_hint_shown = False


def _load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载：KEY=VALUE 每行，忽略注释与空行，不覆盖已有环境变量。

    path 缺省时取模块级 ENV_PATH（调用时解析而非定义时绑定，便于测试替换到临时目录）。
    """
    if path is None:
        path = ENV_PATH
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
    """保存运行时配置覆盖（持久化，跨重启生效）。

    P0-5：runtime.json 可能包含 API Key，写后收紧为 0600（owner 读写），
    防止以默认 0644 落盘导致组/其他用户可读。
    """
    current: dict = {}
    if RUNTIME_PATH.exists():
        try:
            current = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            current = {}
    _deep_merge(current, overrides)
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    _tighten_key_file_permissions(RUNTIME_PATH)


# ================= G5/P0-5 密钥文件权限 =================
#
# 约定：.env 与 data/runtime.json 含 API Key，最小权限 = owner 读写（0600），
# 组/其他用户任何权限位都视为过宽。启动时告警；Web 保存新密钥前检查，
# 过宽时拒绝保存（不静默收紧——避免掩盖用户环境的共享目录配置）。
# 后续扩展点：可替换为系统钥匙串（macOS Keychain / Secret Service）读取密钥，
# 本期不引入新依赖，接口以 ENV/RUNTIME 文件为准。

def key_file_permissions_ok(path: Path) -> bool:
    """检查密钥文件权限是否满足最小权限；文件不存在视为满足（由存在性逻辑另行处理）。"""
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return True
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def _tighten_key_file_permissions(path: Path) -> bool:
    """尽力收紧密钥文件权限为 0600；失败仅告警不抛错（如跨平台文件系统不支持）。"""
    try:
        os.chmod(path, 0o600)
        return True
    except OSError as exc:
        LOG.warning("收紧密钥文件权限失败（%s）: %s", path, exc)
        return False


def warn_key_file_permissions() -> None:
    """启动时检查 .env / runtime.json 权限，过宽则告警（提示会影响保存新密钥）。"""
    for path, label in ((ENV_PATH, ".env"), (RUNTIME_PATH, "data/runtime.json")):
        if not path.exists() or key_file_permissions_ok(path):
            continue
        LOG.warning(
            "%s 权限过宽（组/其他用户可读），存在密钥泄露风险；将拒绝通过设置面板保存新密钥。"
            "建议执行 chmod 600 %s 收紧权限。",
            label, path,
        )


# ================= G2 配置校验（P0-4 + spec/upgrade-2026-09「校验规则表」） =================
#
# 约定：只校验显式提供的值（缺失键用默认值，不算错误）；收集全部错误不 fail-fast；
# 每条消息含完整 dotted 路径与中文期望；路径含 key/token/secret 语义的键绝不回显
# 实际值（只回显类型）。


class ConfigError(RuntimeError):
    """配置校验失败异常。errors 为全部错误消息列表，异常消息按行拼接。"""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


# 整数规则（bool 不算 int，闭区间）：dotted 路径 → (最小值, 最大值)。
# 注：chunking.max_chars 在规则表中只出现于跨字段条目，此处按 min_chars 同口径补上
# 类型/范围校验（解释性决定），否则跨字段比较对非整数值无从下手。
_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "retrieval.top_k": (1, 100),
    "retrieval.rerank_candidates": (1, 200),
    "planner.max_steps": (1, 50),
    "planner.max_reflections": (0, 10),
    "tools.max_retries": (0, 5),
    "tools.search_default_top_k": (1, 50),
    "llm.max_retries": (0, 10),
    "llm.json_repair_rounds": (0, 5),
    "llm.max_tokens": (64, 32768),
    "memory.max_episodes": (1, 100000),
    "embedding.hash_dim": (64, 65536),
    "chunking.min_chars": (0, 100000),
    "chunking.max_chars": (0, 100000),
    "audit.retention_days": (1, 3650),
    "tasks.step_timeout_s": (1, 86400),
    "tasks.total_timeout_s": (1, 86400),
    "tasks.watchdog_interval_s": (1, 3600),
}
_POSITIVE_INT_KEYS: tuple[str, ...] = ("llm.timeout",)  # 整数且 >0
# 浮点规则（int 或 float 均可，bool 不算，闭区间）
_FLOAT_RANGES: dict[str, tuple[float, float]] = {
    "llm.temperature": (0.0, 2.0),
    "planner.temperature": (0.0, 2.0),
    "retrieval.vector_weight": (0.0, 1.0),
    "retrieval.keyword_weight": (0.0, 1.0),
    "retrieval.rerank_blend": (0.0, 1.0),
}
_POSITIVE_FLOAT_KEYS: tuple[str, ...] = ("tools.web_search.timeout",)  # 数值且 >0
_NONNEGATIVE_FLOAT_KEYS: tuple[str, ...] = ("tools.web_search.cache_ttl",)  # 数值且 ≥0
# 枚举规则（大小写不敏感）；retrieval.rerank 另接受 bool true/false（旧配置向后兼容）
_ENUM_CHOICES: dict[str, tuple[str, ...]] = {
    "retrieval.rerank": ("off", "auto", "cross_encoder", "llm"),
    "retrieval.fusion_mode": ("rrf", "weighted"),
    "embedding.backend": ("auto", "local", "tfidf"),
    "memory.embedding_backend": ("auto", "local", "tfidf"),
}
_BOOL_STR_TRUE = frozenset({"true", "1", "yes"})
_BOOL_STR_FALSE = frozenset({"false", "0", "no"})
# 布尔键集：全部 *_enabled + contextual_augment / multi_query / fallback_direct / reflect / kb_fallback_web
_BOOL_KEYS: frozenset[str] = frozenset({
    "chunking.contextual_augment",
    "retrieval.multi_query",
    "planner.fallback_direct",
    "planner.reflect",
    "tools.kb_fallback_web",
    "tools.calculator_enabled",
    "tools.time_enabled",
    "tools.topics_enabled",
    "tools.web_search_enabled",
    "memory.long_term_enabled",
    "memory.entities_enabled",
    "audit.enabled",
    "audit.log_content",
})
_NONEMPTY_NO_NUL_KEYS: tuple[str, ...] = ("kb_path", "data_dir")  # 非空且无 NUL
_URL_KEYS: tuple[str, ...] = ("llm.base_url", "vision.base_url")  # 非空时须 http(s):// 开头
_NONEMPTY_NO_WS_KEYS: tuple[str, ...] = (  # 非空且无空白
    "llm.chat_model",
    "planner.model",
    "embedding.model",
    "retrieval.reranker_model",
)
_NO_WS_IF_NONEMPTY_KEYS: tuple[str, ...] = ("vision.model",)  # 非空时无空白（允许空）
_TYPE_ONLY_STR_KEYS: tuple[str, ...] = ("llm.api_key", "vision.api_key")  # 只查类型为 str

_MISSING = object()  # _get_path 的「键不存在」哨兵（区别于值恰好为 None）


def _get_path(raw: dict, dotted: str) -> Any:
    """按 dotted 路径取值；任一层缺失或不是 dict 时返回哨兵 _MISSING。"""
    node: Any = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set_path(raw: dict, dotted: str, value: Any) -> None:
    """按 dotted 路径就地覆写已存在的键；任一层缺失或不是 dict 时静默跳过。"""
    parts = dotted.split(".")
    node: Any = raw
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    if isinstance(node, dict) and parts[-1] in node:
        node[parts[-1]] = value


def _is_sensitive(dotted: str) -> bool:
    """键路径是否敏感（key/token/secret 语义）。

    按片段全等或后缀判断（如 api_key），而非子串——避免 keyword_weight 因含
    "key" 子串被误判为敏感。
    """
    return any(
        segment in ("key", "token", "secret") or segment.endswith(("_key", "_token", "_secret"))
        for segment in dotted.lower().split(".")
    )


def _render_actual(dotted: str, value: Any) -> str:
    """渲染错误消息中的「实际值」片段。敏感键只回显类型，绝不回显实际值。"""
    if _is_sensitive(dotted):
        return f"实际类型: {type(value).__name__}"
    if isinstance(value, str):
        if "\x00" in value:
            return "实际含 NUL 字符"
        shown = value if len(value) <= 60 else value[:57] + "…"
        return f"实际: {shown!r}"
    if isinstance(value, (int, float)):
        return f"实际: {value!r}"
    return f"实际类型: {type(value).__name__}"


def _is_number(value: Any) -> bool:
    """数值判定：int/float 均可，bool 不算（bool 是 int 子类，需显式排除）。"""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def validate_config(raw: dict) -> list[str]:
    """按 G2 规则表校验显式提供的配置值，返回全部错误消息（空列表 = 通过）。

    - 缺失键不算错误（使用默认值）；
    - 收集全部错误，不 fail-fast；
    - 每条消息含完整 dotted 路径与中文期望（修复建议）；
    - 敏感键（api_key 等）绝不回显实际值，只回显类型。
    """
    errors: list[str] = []

    def add(dotted: str, expect: str, value: Any, type_only: bool = False) -> None:
        # 类型错误只报类型（对齐规格示例）；取值错误回显实际值（敏感键由 _render_actual 兜底只报类型）
        actual = f"实际类型: {type(value).__name__}" if type_only else _render_actual(dotted, value)
        errors.append(f"{dotted}: {expect}（{actual}）")

    # ---- 整数（bool 不算 int，闭区间）----
    for dotted, (lo, hi) in _INTEGER_RANGES.items():
        value = _get_path(raw, dotted)
        if value is _MISSING:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            add(dotted, f"应为 {lo}–{hi} 的整数", value, type_only=True)
        elif not lo <= value <= hi:
            add(dotted, f"应为 {lo}–{hi} 的整数", value)
    # ---- 正整数（整数且 >0）----
    for dotted in _POSITIVE_INT_KEYS:
        value = _get_path(raw, dotted)
        if value is _MISSING:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            add(dotted, "应为大于 0 的整数", value, type_only=True)
        elif value <= 0:
            add(dotted, "应为大于 0 的整数", value)
    # ---- 浮点（int 或 float 均可，闭区间）----
    for dotted, (lo, hi) in _FLOAT_RANGES.items():
        value = _get_path(raw, dotted)
        if value is _MISSING:
            continue
        if not _is_number(value):
            add(dotted, f"应为 {lo:g}–{hi:g} 的数值", value, type_only=True)
        elif not lo <= value <= hi:
            add(dotted, f"应为 {lo:g}–{hi:g} 的数值", value)
    # ---- 正浮点（数值且 >0）/ 非负浮点（数值且 ≥0）----
    for dotted in _POSITIVE_FLOAT_KEYS:
        value = _get_path(raw, dotted)
        if value is _MISSING:
            continue
        if not _is_number(value):
            add(dotted, "应为大于 0 的数值", value, type_only=True)
        elif value <= 0:
            add(dotted, "应为大于 0 的数值", value)
    for dotted in _NONNEGATIVE_FLOAT_KEYS:
        value = _get_path(raw, dotted)
        if value is _MISSING:
            continue
        if not _is_number(value):
            add(dotted, "应为不小于 0 的数值", value, type_only=True)
        elif value < 0:
            add(dotted, "应为不小于 0 的数值", value)
    # ---- 枚举（大小写不敏感；retrieval.rerank 的 bool true/false 向后兼容）----
    for dotted, choices in _ENUM_CHOICES.items():
        value = _get_path(raw, dotted)
        if value is _MISSING:
            continue
        if dotted == "retrieval.rerank" and isinstance(value, bool):
            continue
        if not isinstance(value, str):
            add(dotted, f"应为 {'/'.join(choices)} 之一", value, type_only=True)
        elif value.lower() not in choices:
            add(dotted, f"应为 {'/'.join(choices)} 之一", value)
    # ---- 布尔（bool 或字符串 true/false/1/0/yes/no，大小写不敏感）----
    for dotted in sorted(_BOOL_KEYS):
        value = _get_path(raw, dotted)
        if value is _MISSING or isinstance(value, bool):
            continue
        if not isinstance(value, str):
            add(dotted, "应为布尔值 true/false/1/0/yes/no", value, type_only=True)
        elif value.strip().lower() not in _BOOL_STR_TRUE | _BOOL_STR_FALSE:
            add(dotted, "应为布尔值 true/false/1/0/yes/no", value)
    # ---- 字符串/格式 ----
    for dotted in _NONEMPTY_NO_NUL_KEYS:
        value = _get_path(raw, dotted)
        if value is _MISSING:
            continue
        if not isinstance(value, str):
            add(dotted, "应为非空且不含 NUL 字符的字符串", value, type_only=True)
        elif not value or "\x00" in value:
            add(dotted, "应为非空且不含 NUL 字符的字符串", value)
    for dotted in _URL_KEYS:
        value = _get_path(raw, dotted)
        if value is _MISSING or (isinstance(value, str) and not value):
            continue  # 空串表示走环境变量/默认值，放行
        if not isinstance(value, str):
            add(dotted, "应为以 http:// 或 https:// 开头的字符串", value, type_only=True)
        elif not value.startswith(("http://", "https://")):
            add(dotted, "应为以 http:// 或 https:// 开头的字符串", value)
    for dotted in _NONEMPTY_NO_WS_KEYS:
        value = _get_path(raw, dotted)
        if value is _MISSING:
            continue
        if not isinstance(value, str):
            add(dotted, "应为非空且不含空白字符的字符串", value, type_only=True)
        elif not value.strip() or any(ch.isspace() for ch in value):
            add(dotted, "应为非空且不含空白字符的字符串", value)
    for dotted in _NO_WS_IF_NONEMPTY_KEYS:
        value = _get_path(raw, dotted)
        if value is _MISSING or (isinstance(value, str) and not value):
            continue  # 允许为空（视觉模型未配置）
        if not isinstance(value, str):
            add(dotted, "应为不含空白字符的字符串", value, type_only=True)
        elif any(ch.isspace() for ch in value):
            add(dotted, "应为不含空白字符的字符串", value)
    for dotted in _TYPE_ONLY_STR_KEYS:
        value = _get_path(raw, dotted)
        if value is not _MISSING and not isinstance(value, str):
            add(dotted, "应为字符串", value, type_only=True)  # 只查类型，不查内容
    # ---- 跨字段：chunking.max_chars 必须大于 chunking.min_chars（两侧均显式提供才比较）----
    max_chars = _get_path(raw, "chunking.max_chars")
    min_chars = _get_path(raw, "chunking.min_chars")
    if _is_number(max_chars) and _is_number(min_chars) and max_chars <= min_chars:
        errors.append(
            "chunking.max_chars: 必须大于 chunking.min_chars"
            f"（实际: max_chars={max_chars}, min_chars={min_chars}）"
        )
    return errors


def normalize_config(raw: dict) -> dict:
    """就地归一化规则表中的布尔字符串键与枚举键，返回 raw 本身。

    仅处理规则表列出的键，应在 validate_config 通过后调用（假定值已合法）：
    - 布尔键："true"/"1"/"yes"（大小写不敏感）→ True，"false"/"0"/"no" → False；
    - 枚举键：字符串归一化小写（retrieval.rerank 的 bool 值保持不变）。
    """
    for dotted in _BOOL_KEYS:
        value = _get_path(raw, dotted)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _BOOL_STR_TRUE:
                _set_path(raw, dotted, True)
            elif lowered in _BOOL_STR_FALSE:
                _set_path(raw, dotted, False)
    for dotted in _ENUM_CHOICES:
        value = _get_path(raw, dotted)
        if isinstance(value, str):
            _set_path(raw, dotted, value.strip().lower())
    return raw


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
        """知识库检索重试耗尽仍失败时，是否降级用 Web 搜索（P0-3：默认关闭）。"""
        return bool(self.get("tools.kb_fallback_web", False))

    # ---- 记忆体系（S4：长期记忆 + 实体记忆 + 会话摘要压缩） ----
    @property
    def memory_long_term_enabled(self) -> bool:
        """是否启用跨会话长期记忆（成功问答写入经验，规划时召回相关历史）。P0-3：默认关闭。"""
        return bool(self.get("memory.long_term_enabled", False))

    @property
    def memory_entities_enabled(self) -> bool:
        """是否启用实体记忆（从问答中抽取关键实体及其事实）。P0-3：默认关闭。"""
        return bool(self.get("memory.entities_enabled", False))

    @property
    def memory_max_episodes(self) -> int:
        """长期记忆最多保留的经验条数，超出按命中率与新旧淘汰。"""
        return int(self.get("memory.max_episodes", 200))

    @property
    def memory_embedding_backend(self) -> str:
        """长期记忆向量后端：auto | local | tfidf（auto 优先本地模型，失败回退 TF-IDF）。"""
        return str(self.get("memory.embedding_backend", "auto"))

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

    # ---- 审计轨迹（G6） ----
    @property
    def audit_enabled(self) -> bool:
        """审计总开关：关闭后不写审计事件。"""
        return bool(self.get("audit.enabled", True))

    @property
    def audit_retention_days(self) -> int:
        """审计文件保留天数。"""
        return int(self.get("audit.retention_days", 30))

    @property
    def audit_log_content(self) -> bool:
        """是否记录问题与答案正文（P0-5 语义：默认关闭，显式开启才写）。"""
        return bool(self.get("audit.log_content", False))

    # ---- 任务看门狗（G7/P1-2） ----
    @property
    def tasks_step_timeout_s(self) -> float:
        """步骤级超时（秒）：心跳停滞超过该值的 running 任务被看门狗转 paused。"""
        return float(self.get("tasks.step_timeout_s", 600.0))

    @property
    def tasks_total_timeout_s(self) -> float:
        """任务级总超时（秒）：执行超过该值后不再推进剩余步骤。"""
        return float(self.get("tasks.total_timeout_s", 3600.0))

    @property
    def tasks_watchdog_interval_s(self) -> float:
        """看门狗扫描间隔（秒）。"""
        return float(self.get("tasks.watchdog_interval_s", 30.0))


def _maybe_log_egress_migration_hint() -> None:
    """G3/P0-3 迁移提示：runtime.json 存在但未显式设置外发开关时，提示默认行为已变更。

    兼容策略（P0-3）：runtime.json 显式配置优先、不强制覆盖；但存量用户的 runtime.json
    由设置面板写入、通常不含这三个键——默认值翻转会实际改变其行为，故提示一次/进程。
    """
    global _migration_hint_shown
    if _migration_hint_shown or not RUNTIME_PATH.exists():
        return
    try:
        rt = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(rt, dict):
        return
    missing = [key for key in _EGRESS_DEFAULT_KEYS if _get_path(rt, key) is _MISSING]
    if not missing:
        return
    _migration_hint_shown = True
    LOG.info(
        "默认行为已变更：联网降级与长期记忆默认关闭（%s 未显式设置，本次起按默认关闭执行）。"
        "如需保持原行为，请在设置面板或 data/runtime.json 中显式开启对应开关。",
        "、".join(missing),
    )


def load_config() -> Config:
    _load_dotenv()
    # G5/P0-5：启动时检查密钥文件权限，过宽即告警（一次/进程，不影响启动）
    warn_key_file_permissions()
    raw: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            raw = _deep_merge(raw, loaded)
    # 运行时覆盖（Web 设置面板写入）
    raw = _load_runtime(raw)
    # G3：存量 runtime.json 未显式设置外发开关时，提示默认行为已变更（只提示一次/进程）
    _maybe_log_egress_migration_hint()
    # 环境变量覆盖
    if os.environ.get("DEEPSEEK_API_KEY"):
        raw.setdefault("llm", {})["api_key_env"] = "DEEPSEEK_API_KEY"
    # G2：返回前对合并后的 raw 做校验，失败抛 ConfigError（进程不静默启动）
    errors = validate_config(raw)
    if errors:
        raise ConfigError(errors)
    # 校验通过后归一化布尔字符串与枚举大小写（仅规则表列出的键）
    normalize_config(raw)
    return Config(raw)
