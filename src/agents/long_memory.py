"""长期记忆（S4）：跨会话经验库 + 会话摘要压缩器 + 实体记忆。

三个组件：
- LongTermMemory：MemoryEpisode 向量库（复用项目内 embedding 后端），余弦 top-k 检索、
  容量淘汰（prune）与磁盘持久化（episodes.json + vectors.npy + meta.json，tmp 写 +
  os.replace 原子替换）。agent.ask 召回相关历史经验注入规划提示词，成功问答后写入经验。
- MemoryCompressor：把会话记忆滑出窗口的旧轮次与旧摘要合并为 ≤300 字中文摘要；
  LLM 任何异常都回退为本地拼接（各轮首句截断，总长 ≤600）。
- EntityMemory：从问答中抽取关键实体及一句事实（LLM JSON，异常/坏结构 → 空），
  维护 dict[name, {facts, last_seen}]，随长期记忆同目录持久化 entities.json。

线程安全：LongTermMemory 内部持 threading.Lock（agent.ask 与任务线程可能并发访问）。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .embeddings import LocalEmbeddingBackend, TfidfHashEmbeddingBackend
from .logger import get_logger

LOG = get_logger("long_memory")

EPISODE_SUMMARY_MAX = 400   # episode.answer_summary 截断长度（字符）
SUMMARY_MAX = 300           # 压缩摘要目标长度（字符）
FALLBACK_SUMMARY_MAX = 600  # 降级拼接摘要总长上限（字符）
ENTITY_NAME_MAX = 30        # 实体名最大长度，超长丢弃
ENTITY_MAX = 5              # 单次抽取实体数上限
ENTITY_PROMPT_LIMIT = 8     # as_prompt 默认渲染实体数


def now_iso() -> str:
    """当前本地时间 ISO 字符串（含微秒，可按字典序比较先后）。"""
    return datetime.now().isoformat(timespec="microseconds")


@dataclass
class MemoryEpisode:
    """一条跨会话经验：成功问答的紧凑记录。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = ""
    ts: str = field(default_factory=now_iso)
    question: str = ""
    answer_summary: str = ""
    sources: list[str] = field(default_factory=list)
    entities: dict[str, str] = field(default_factory=dict)   # 实体名 -> 一句事实
    hits: int = 0                                            # 检索命中计数（供淘汰排序）


def _first_sentence(text: str, limit: int = 80) -> str:
    """取文本首句（按中文句读切断）并截断到 limit 字符。"""
    text = " ".join((text or "").split())
    for sep in ("。", "！", "？", "；", "."):
        idx = text.find(sep)
        if idx > 0:
            text = text[:idx]
            break
    return text[:limit]


def fallback_summary(old_summary: str, evicted_turns) -> str:
    """降级摘要：旧摘要 + 各轮问答首句（各截 80 字），总长 ≤600。无 LLM 时也可用。"""
    parts: list[str] = []
    if (old_summary or "").strip():
        parts.append(old_summary.strip())
    for t in evicted_turns or []:
        parts.append(f"问：{_first_sentence(getattr(t, 'question', ''))}／答：{_first_sentence(getattr(t, 'answer', ''))}")
    return "；".join(parts)[:FALLBACK_SUMMARY_MAX]


COMPRESSOR_SYSTEM = (
    "你是对话摘要压缩器。把『旧摘要』与若干轮新滑出的对话合并为一段不超过 300 字的中文摘要。"
    "保留关键事实、实体、结论与未完成的意图，去掉寒暄与重复；直接输出摘要正文，不要任何解释或前缀。"
)


class MemoryCompressor:
    """会话摘要压缩器：一次 LLM 调用把旧摘要与滑出轮次合并为 ≤300 字摘要。"""

    def __init__(self, llm):
        self.llm = llm

    def compress(self, old_summary: str, evicted_turns) -> str:
        """合并摘要；LLM 任何异常（LLMError 等）→ 回退本地拼接，绝不抛异常。"""
        turns_text = "\n\n".join(
            f"用户：{getattr(t, 'question', '')}\n助手：{str(getattr(t, 'answer', ''))[:500]}"
            for t in evicted_turns or []
        )
        messages = [
            {"role": "system", "content": COMPRESSOR_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"【旧摘要】\n{(old_summary or '').strip() or '（无）'}\n\n"
                    f"【新滑出的对话轮】\n{turns_text or '（无）'}\n\n"
                    "请输出合并后的中文摘要（≤300 字）。"
                ),
            },
        ]
        try:
            text = self.llm.chat(messages).text.strip()
            if text:
                return text[:SUMMARY_MAX]
        except Exception as exc:  # noqa: BLE001 - 摘要失败必须降级而非中断问答
            LOG.warning("摘要压缩失败，回退本地拼接: %s", exc)
        return fallback_summary(old_summary, evicted_turns)


ENTITY_EXTRACT_SYSTEM = (
    "你是实体抽取器。从给出的问答对中抽取 0~5 个关键实体（人名、产品、概念等），"
    "并为每个实体写一句与该问答相关的事实。只输出 JSON，结构为："
    '{"entities": {"实体名": "一句事实"}}，不要输出任何其他文字。'
)


class EntityMemory:
    """实体记忆：跨轮维护关键实体及其事实，支持 as_prompt 渲染与 entities.json 持久化。"""

    def __init__(self, store_dir: Path | None = None):
        self.store_dir = Path(store_dir) if store_dir else None
        self.entities: dict[str, dict] = {}   # name -> {"facts": [str], "last_seen": iso}

    # ---- 抽取与合并 ----
    def extract(self, llm, question: str, answer: str) -> dict[str, str]:
        """从问答对抽取实体（一次 chat_json）；任何异常/坏结构 → {} 不崩溃。"""
        try:
            obj = llm.chat_json(
                [
                    {"role": "system", "content": ENTITY_EXTRACT_SYSTEM},
                    {"role": "user", "content": f"问题：{question}\n回答：{(answer or '')[:800]}\n\n请输出实体 JSON。"},
                ]
            )
        except Exception as exc:  # noqa: BLE001 - 实体抽取失败不影响问答
            LOG.warning("实体抽取失败，返回空: %s", exc)
            return {}
        raw = obj.get("entities") if isinstance(obj, dict) else None
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for name, fact in raw.items():
            name, fact = str(name).strip(), str(fact).strip()
            if not name or not fact or len(name) > ENTITY_NAME_MAX:
                continue
            out[name] = fact
            if len(out) >= ENTITY_MAX:
                break
        return out

    def merge(self, entity_dict: dict[str, str]) -> None:
        """合并抽取结果：事实去重追加，last_seen 刷新为当前时间。"""
        now = now_iso()
        for name, fact in (entity_dict or {}).items():
            entry = self.entities.setdefault(name, {"facts": [], "last_seen": ""})
            if fact and fact not in entry["facts"]:
                entry["facts"].append(fact)
            entry["last_seen"] = now

    # ---- 渲染 ----
    def as_prompt(self, limit: int = ENTITY_PROMPT_LIMIT) -> str:
        """渲染"已知实体"提示词块（按 last_seen 倒序取 limit）；为空返回 ""。"""
        if not self.entities:
            return ""
        ranked = sorted(self.entities.items(), key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
        lines = ["已知实体："]
        for name, entry in ranked[: max(0, limit)]:
            facts = entry.get("facts") or []
            fact = facts[-1] if facts else ""
            lines.append(f"- {name}：{fact}（最近提及 {str(entry.get('last_seen', ''))[:19]}）")
        return "\n".join(lines)

    # ---- 持久化 ----
    def save(self) -> None:
        """entities.json 原子写（tmp + os.replace）；未配置存储目录时跳过。"""
        if self.store_dir is None:
            return
        self.store_dir.mkdir(parents=True, exist_ok=True)
        path = self.store_dir / "entities.json"
        tmp = self.store_dir / "entities.json.tmp"
        tmp.write_text(json.dumps(self.entities, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def load(self) -> None:
        """加载 entities.json；文件缺失或损坏 → 空库。"""
        self.entities = {}
        if self.store_dir is None:
            return
        path = self.store_dir / "entities.json"
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.entities = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError) as exc:
            LOG.warning("实体记忆读取失败，按空库处理: %s", exc)


class LongTermMemory:
    """跨会话长期记忆库：MemoryEpisode 向量化存储 + 余弦检索 + 容量淘汰 + 持久化。

    - add：episode 文本（question + " " + answer_summary）向量化追加；TF-IDF 后端
      语料变化后整体 re-fit 重建（规模小），local 后端只增量嵌入新条目。
    - search：余弦 top-k，命中者 hits+1 并持久化；支持按会话排除（全部 / 最近 n 条）。
    - prune：超过 max_episodes 时按 (hits 升序, ts/id 旧优先) 淘汰。
    - 持久化：episodes.json（列表）+ vectors.npy + meta.json（backend 名），
      全部 tmp 写 + os.replace 原子替换；backend 名或维度不一致 → 向量不可比，重建空库。
    """

    def __init__(self, store_dir: Path, embed_backend, max_episodes: int = 200):
        self.store_dir = Path(store_dir)
        self.backend = embed_backend
        self.max_episodes = max(1, int(max_episodes))
        self._lock = threading.RLock()
        self.episodes: list[MemoryEpisode] = []
        self._vectors: np.ndarray = self._empty_vectors()
        self.load()

    # ---- 基础 ----
    @property
    def size(self) -> int:
        return len(self.episodes)

    def _empty_vectors(self) -> np.ndarray:
        dim = int(getattr(self.backend, "dim", 0) or 0)
        return np.zeros((0, dim), dtype=np.float32)

    @staticmethod
    def _episode_text(episode: MemoryEpisode) -> str:
        return " ".join(f"{episode.question} {episode.answer_summary}".split())

    def _reembed_all(self) -> None:
        """TF-IDF 后端：语料变化后整体 re-fit 并重建全部向量（当前量级代价可忽略）。"""
        texts = [self._episode_text(e) for e in self.episodes]
        if hasattr(self.backend, "fit"):
            self.backend.fit(texts)
        if texts:
            self._vectors = np.asarray(self.backend.embed_texts(texts), dtype=np.float32)
        else:
            self._vectors = self._empty_vectors()

    def _evict_overflow(self) -> list[MemoryEpisode]:
        """淘汰超额 episode（(hits, ts, id) 最小者先出）；非 TF-IDF 后端同步删除向量行。"""
        evicted: list[MemoryEpisode] = []
        while len(self.episodes) > self.max_episodes:
            idx = min(
                range(len(self.episodes)),
                key=lambda i: (self.episodes[i].hits, self.episodes[i].ts, self.episodes[i].id),
            )
            evicted.append(self.episodes.pop(idx))
            if self.backend.name != "tfidf" and idx < self._vectors.shape[0]:
                self._vectors = np.delete(self._vectors, idx, axis=0)
        return evicted

    # ---- 写入 ----
    def add(self, episode: MemoryEpisode) -> None:
        """写入一条经验并持久化；超出容量自动淘汰。"""
        with self._lock:
            if self.backend.name == "tfidf":
                self.episodes.append(episode)
                self._evict_overflow()
                self._reembed_all()
            else:
                vec = np.asarray(
                    self.backend.embed_texts([self._episode_text(episode)]), dtype=np.float32
                ).reshape(1, -1)
                self.episodes.append(episode)
                self._vectors = vec if self._vectors.size == 0 else np.vstack([self._vectors, vec])
                self._evict_overflow()
            self.save()

    def prune(self) -> list[MemoryEpisode]:
        """容量淘汰（公开接口）；TF-IDF 后端淘汰后整体重建向量。返回被淘汰条目。"""
        with self._lock:
            evicted = self._evict_overflow()
            if evicted and self.backend.name == "tfidf":
                self._reembed_all()
            return evicted

    # ---- 检索 ----
    def _excluded_ids(self, exclude_session: str | None, exclude_session_recent) -> set[str]:
        """计算检索时应排除的 episode id 集合。"""
        excluded: set[str] = set()
        if exclude_session:
            excluded |= {e.id for e in self.episodes if e.session_id == exclude_session}
        if exclude_session_recent:
            sid, n = exclude_session_recent
            n = max(0, int(n))
            if sid and n > 0:
                recent = sorted(
                    (e for e in self.episodes if e.session_id == sid),
                    key=lambda e: (e.ts, e.id),
                    reverse=True,
                )[:n]
                excluded |= {e.id for e in recent}
        return excluded

    def search(
        self,
        query: str,
        k: int = 3,
        exclude_session: str | None = None,
        exclude_session_recent: tuple[str, int] | None = None,
    ) -> list[MemoryEpisode]:
        """余弦 top-k 检索；命中者 hits+1 并持久化。

        exclude_session：排除该会话的全部 episode；
        exclude_session_recent：(session_id, n) 时排除该会话最近 n 条（按 ts/id 倒序）。
        """
        with self._lock:
            if not self.episodes or self._vectors.size == 0:
                return []
            if self._vectors.shape[0] != len(self.episodes):
                # 向量与元数据错位（异常状态）：TF-IDF 可整体重建，其余按空结果处理
                if self.backend.name == "tfidf":
                    self._reembed_all()
                else:
                    LOG.warning("长期记忆向量与条目数不一致，跳过本次检索")
                    return []
            excluded = self._excluded_ids(exclude_session, exclude_session_recent)
            q = np.asarray(self.backend.embed_query(query), dtype=np.float32)
            qn = np.linalg.norm(q)
            if qn == 0:
                return []
            q = q / qn
            norms = np.linalg.norm(self._vectors, axis=1)
            safe = np.where(norms > 0, norms, 1.0)
            scores = (self._vectors / safe[:, None]) @ q
            hits: list[MemoryEpisode] = []
            for idx in np.argsort(-scores).tolist():
                if len(hits) >= max(0, int(k)):
                    break
                ep = self.episodes[idx]
                if ep.id in excluded:
                    continue
                ep.hits += 1
                hits.append(ep)
            if hits:
                self.save()
            return hits

    def as_context(self, query: str, k: int = 3, **exclude) -> str:
        """渲染"相关历史经验"提示词块；无命中或检索异常返回 ""。"""
        try:
            hits = self.search(query, k=k, **exclude)
        except Exception as exc:  # noqa: BLE001 - 记忆增强失败不拖垮问答
            LOG.warning("长期记忆检索失败: %s", exc)
            return ""
        if not hits:
            return ""
        lines = ["相关历史经验："]
        for i, ep in enumerate(hits, 1):
            lines.append(f"[{i}] Q: {' '.join(ep.question.split())}")
            lines.append(f"    A: {' '.join(ep.answer_summary.split())}")
        return "\n".join(lines)

    # ---- 持久化 ----
    def save(self) -> None:
        """原子持久化：episodes.json + vectors.npy + meta.json（各文件 tmp 写 + os.replace）。"""
        with self._lock:
            self.store_dir.mkdir(parents=True, exist_ok=True)
            ep_tmp = self.store_dir / "episodes.json.tmp"
            ep_tmp.write_text(
                json.dumps([asdict(e) for e in self.episodes], ensure_ascii=False), encoding="utf-8"
            )
            os.replace(ep_tmp, self.store_dir / "episodes.json")

            vec_tmp = self.store_dir / "vectors.npy.tmp"
            with open(vec_tmp, "wb") as f:
                np.save(f, self._vectors)
            os.replace(vec_tmp, self.store_dir / "vectors.npy")

            meta_tmp = self.store_dir / "meta.json.tmp"
            meta_tmp.write_text(
                json.dumps(
                    {
                        "backend": self.backend.name,
                        "dim": int(getattr(self.backend, "dim", 0) or 0),
                        "model": str(getattr(self.backend, "_model_name", "")),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(meta_tmp, self.store_dir / "meta.json")

    def load(self) -> None:
        """从磁盘加载；文件缺失 → 空库；backend 名/维度不一致或数据损坏 → 重建空库并告警。"""
        with self._lock:
            self.episodes = []
            self._vectors = self._empty_vectors()
            ep_file = self.store_dir / "episodes.json"
            if not ep_file.exists():
                return
            try:
                raw = json.loads(ep_file.read_text(encoding="utf-8"))
                episodes = [MemoryEpisode(**item) for item in raw]
            except (OSError, ValueError, TypeError) as exc:
                LOG.warning("长期记忆 episodes.json 读取失败，按空库重建: %s", exc)
                return
            meta: dict = {}
            meta_file = self.store_dir / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    if not isinstance(meta, dict):
                        meta = {}
                except (OSError, ValueError):
                    meta = {}
            if str(meta.get("backend", "")) != self.backend.name:
                LOG.warning(
                    "长期记忆向量后端不一致（库=%s 当前=%s），向量不可比，重建为空库",
                    meta.get("backend", "未知"), self.backend.name,
                )
                return
            vectors = self._empty_vectors()
            vec_file = self.store_dir / "vectors.npy"
            if vec_file.exists():
                try:
                    with open(vec_file, "rb") as f:
                        vectors = np.asarray(np.load(f), dtype=np.float32)
                except (OSError, ValueError) as exc:
                    LOG.warning("长期记忆 vectors.npy 读取失败，按空库重建: %s", exc)
                    return
            dim = int(getattr(self.backend, "dim", 0) or 0)
            if (len(episodes) and vectors.shape != (len(episodes), dim)) or (
                not episodes and vectors.size
            ):
                LOG.warning("长期记忆向量维度/数量与条目不一致，重建为空库")
                return
            self.episodes = episodes
            self._vectors = vectors
            if self.episodes and self.backend.name == "tfidf":
                # TF-IDF 的 IDF 统计不持久化：加载后按当前语料 re-fit，保证查询与文档同分布
                self._reembed_all()


def build_memory_backend(config):
    """按配置构建长期记忆向量后端（供持久化 meta 校验 backend 名）。

    memory.embedding_backend == "local"/"auto" 时优先复用 LocalEmbeddingBackend
    （加载失败/未安装等任何异常都回退）；否则/回退时用 TF-IDF 哈希后端（dim 取
    embedding.hash_dim），保证离线可用。
    """
    mode = str(getattr(config, "memory_embedding_backend", "auto") or "auto").lower()
    if mode in ("local", "auto"):
        try:
            return LocalEmbeddingBackend(config.embedding_model)
        except Exception as exc:  # noqa: BLE001 - 模型不可用时一律回退
            if mode == "local":
                LOG.warning("本地向量模型加载失败（%s: %s），长期记忆回退 TF-IDF", type(exc).__name__, exc)
            else:
                LOG.info("本地向量模型不可用（%s），长期记忆使用 TF-IDF 哈希后端", type(exc).__name__)
    return TfidfHashEmbeddingBackend(hash_dim=config.hash_dim)
