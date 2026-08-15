"""Agent 门面：编排「规划 → 执行 → 生成」完整问答流程。

Agent 持有：
- 知识库索引（chunks + vector store + bm25）
- 检索器（混合召回）
- 规划层、工具层、执行器
- LLM 客户端

ask(question) 返回结构化 Answer：计划、步骤结果、最终答案、引用来源、延迟与 Token 用量。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .bm25 import BM25Index
from .chunking import Chunk, Leaf, summarize_corpus
from .embeddings import build_backend
from .executor import Executor, StepResult
from .llm import LLMClient, LLMError
from .logger import get_logger

LOG = get_logger("agent")
from .memory import SessionMemory
from .planner import Plan, Planner, rewrite_query
from .retriever import Retriever
from .tools import ToolContext, build_tools, describe_tools
from .vector_store import VectorStore

SYNTHESIS_SYSTEM = """你是知识库问答助手。基于检索到的片段回答用户问题。

要求：
1. 只依据提供的检索片段回答；片段中没有的内容要明确说"知识库中未找到相关信息"，不要编造。
2. 用中文回答，条理清晰。
3. 引用来源：正文中用 [n] 标注依据（n 对应片段编号），回答末尾列出"参考来源"清单。
4. 如果规划中包含了工具执行结果（检索片段/计算结果），请充分利用。
"""


@dataclass
class Answer:
    question: str = ""
    plan: Plan = field(default_factory=Plan)
    steps: list[StepResult] = field(default_factory=list)
    final_answer: str = ""
    sources: list[str] = field(default_factory=list)
    total_latency_s: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class Agent:
    def __init__(self, config, llm: LLMClient | None = None, lazy_index: bool = True, memory: SessionMemory | None = None):
        self.config = config
        self.llm = llm
        self.memory = memory or SessionMemory()
        self.chunks: list[Chunk] = []        # 父块（供生成）
        self.leaves: list = []               # 叶子（供索引召回）
        self.contexts: dict[int, str] = {}   # 父块上下文增强（parent_idx -> context）
        self.vector_store = VectorStore()
        self.bm25 = BM25Index()
        self.retriever: Retriever | None = None
        self.tools: dict = {}
        self._backend = None
        self.reranker = None
        self._index_loaded = False
        # 会话级累计统计（跨多次 ask 累积）
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_cost = 0.0
        if not lazy_index:
            self.load_index()

    # ================= 索引 =================

    def build_index(self, chunks: list[Chunk] | None = None, persist: bool = True, augment: bool | None = None) -> "Agent":
        """离线构建索引（父子分块）：父块 → 叶子 → Embedding → 向量库 + BM25。

        augment=True 时用 LLM 为父块生成上下文摘要（Anthropic Contextual Retrieval）。
        """
        from pathlib import Path

        from .chunking import load_chunks_with_leaves
        from .contextual import augment_contexts

        t0 = time.monotonic()
        if chunks is not None:
            # 外部传入父块时，叶子按父块生成
            from .chunking import split_leaves

            self.chunks = chunks
            self.leaves = []
            for pid, p in enumerate(chunks):
                for lt in split_leaves(p.text):
                    self.leaves.append(Leaf(id=len(self.leaves), text=lt, file=p.file, heading=p.heading, source="", parent_idx=pid))
        else:
            self.chunks, self.leaves = load_chunks_with_leaves(Path(self.config.kb_path), self.config)
        if not self.chunks:
            raise RuntimeError(f"知识库 {self.config.kb_path} 中没有可索引的内容")
        # 上下文增强（可选，索引期一次性成本）
        if augment is None:
            augment = self.config.contextual_augment
        if augment:
            if self.llm is None:
                self.ensure_llm()
            self.contexts = augment_contexts(self.llm, self.chunks, self.config.data_dir, verbose=True)
        else:
            self.contexts = {}

        backend = build_backend(self.config)
        self._backend = backend
        texts = [self._leaf_search_text(l) for l in self.leaves]
        if backend.name == "tfidf":
            backend.fit(texts)
        vectors = backend.embed_texts(texts)

        metas = [
            {"text": l.text, "file": l.file, "heading": l.heading, "source": l.source, "parent_idx": l.parent_idx}
            for l in self.leaves
        ]
        self.vector_store = VectorStore().build(vectors, metas, backend.name, getattr(backend, "_model_name", ""))
        self.bm25 = BM25Index().build(texts)
        self._init_runtime()
        if persist:
            self.save_index()
        print(f"[index] 父块 {len(self.chunks)} / 叶子 {len(self.leaves)} | backend={backend.name} dim={backend.dim} | "
              f"上下文增强={len(self.contexts)} | {time.monotonic()-t0:.1f}s")
        return self

    def _leaf_search_text(self, leaf) -> str:
        """叶子的检索文本：上下文摘要 + 标题路径 + 正文。"""
        ctx = self.contexts.get(leaf.parent_idx, "")
        parts = [p for p in (ctx, leaf.heading, leaf.text) if p]
        return "\n".join(parts)

    def save_index(self) -> None:
        path = self.config.data_dir / "index"
        self.vector_store.save(path)
        import json

        payload = {
            "format": 2,
            "parents": [
                {"text": c.text, "file": c.file, "heading": c.heading, "source": c.source}
                for c in self.chunks
            ],
            "leaves": [
                {"text": l.text, "file": l.file, "heading": l.heading, "source": l.source, "parent_idx": l.parent_idx}
                for l in self.leaves
            ],
            "contexts": {str(k): v for k, v in self.contexts.items()},
        }
        (self.config.data_dir / "chunks.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load_index(self) -> "Agent":
        """从磁盘加载索引（v2）；旧格式自动重建。"""
        path = self.config.data_dir / "index"
        if not (path.with_suffix(".npy")).exists():
            print("[index] 未找到本地索引，自动构建中…")
            return self.build_index()
        import json

        self.vector_store = VectorStore.load(path)
        payload = json.loads((self.config.data_dir / "chunks.json").read_text(encoding="utf-8"))
        if payload.get("format") != 2:
            print("[index] 索引格式已升级，重建中…")
            return self.build_index()
        self.chunks = [
            Chunk(id=i, text=c["text"], file=c["file"], heading=c["heading"], source=c.get("source", ""))
            for i, c in enumerate(payload["parents"])
        ]
        self.leaves = [
            Leaf(id=i, text=l["text"], file=l["file"], heading=l["heading"], source=l.get("source", ""), parent_idx=l["parent_idx"])
            for i, l in enumerate(payload["leaves"])
        ]
        self.contexts = {int(k): v for k, v in payload.get("contexts", {}).items()}
        self.bm25 = BM25Index().build([self._leaf_search_text(l) for l in self.leaves])
        self._backend = build_backend(self.config)
        if self.vector_store.backend_name == "tfidf" and hasattr(self._backend, "fit"):
            self._backend.fit([self._leaf_search_text(l) for l in self.leaves])
        elif self.vector_store.backend_name == "local" and self.vector_store.model_name:
            from .embeddings import LocalEmbeddingBackend

            self._backend = LocalEmbeddingBackend(self.vector_store.model_name)
        self._init_runtime()
        print(f"[index] 已加载 父块 {len(self.chunks)} / 叶子 {len(self.leaves)}（{self.vector_store.backend_name}，dim={self.vector_store.dim}）")
        return self

    def _init_runtime(self) -> None:
        self.retriever = Retriever(
            self.vector_store, self.bm25, self.chunks, self.leaves, self.config,
            embed_backend=self._backend, contexts=self.contexts,
        )
        if self.llm:
            self.retriever.attach_llm(self.llm)
        self._load_reranker()
        if self.reranker is not None:
            self.retriever.attach_reranker(self.reranker)
        from .web_search import WebSearch

        self.web_search = WebSearch(timeout=self.config.web_search_timeout, cache_ttl=self.config.web_search_cache_ttl)
        ctx = ToolContext(
            retriever=self.retriever,
            config=self.config,
            llm=self.llm,
            chunks=self.chunks,
            web_search=self.web_search,
        )
        self.tools = build_tools(
            self.config, retriever=self.retriever, chunks=self.chunks, llm=self.llm, web_search=self.web_search
        )
        self._ctx = ctx
        self._index_loaded = True

    def _load_reranker(self) -> None:
        """按配置加载精排器（cross_encoder / llm / off）。模型不可用时优雅降级。"""
        mode = self.config.rerank_mode
        self.reranker = None
        if mode in ("auto", "cross_encoder"):
            try:
                from .reranker import CrossEncoderReranker

                self.reranker = CrossEncoderReranker(self.config.reranker_model)
                print(f"[rerank] 已加载 Cross-Encoder: {self.config.reranker_model}")
            except Exception as exc:  # noqa: BLE001
                print(f"[rerank] Cross-Encoder 不可用（{type(exc).__name__}: {exc}），检索将无精排")
        elif mode == "llm":
            print("[rerank] 使用 LLM 重排模式")
        elif mode != "off":
            print(f"[rerank] 未知模式 {mode!r}，按 off 处理")

    def ensure_llm(self) -> None:
        if self.llm is None:
            self.llm = LLMClient(self.config)
            if self.retriever:
                self.retriever.attach_llm(self.llm)
            if hasattr(self, "_ctx"):
                self._ctx.llm = self.llm

    def reconfigure(self) -> None:
        """Web 设置面板改配置后调用：重建 LLM 客户端 + 按新模式挂载精排器。"""
        self.llm = LLMClient(self.config)
        if self.retriever:
            self.retriever.attach_llm(self.llm)
        if hasattr(self, "_ctx") and self._ctx:
            self._ctx.llm = self.llm
        # 重排模式可能变化：重载/摘除
        mode = self.config.rerank_mode
        if mode in ("auto", "cross_encoder"):
            if self.reranker is None:
                self._load_reranker()
        else:
            self.reranker = None
        if self.retriever:
            self.retriever.attach_reranker(self.reranker)
            self.retriever.reranker = self.reranker

    # ================= 问答 =================

    def ask(self, question: str, verbose: bool = False) -> Answer:
        if not self._index_loaded:
            self.load_index()
        self.ensure_llm()

        answer = Answer(question=question)
        t0 = time.monotonic()
        try:
            # 0. 会话上下文：历史文本 + 追问改写（指代消解，保证检索查询独立）
            history_text = self.memory.as_text()
            q_work = question
            if not self.memory.is_empty:
                q_work = rewrite_query(self.llm, question, history_text)
                answer.llm_calls += 1

            # 1. 规划（规划器能看到历史，从而理解追问；规则要求 search query 独立）
            planner = Planner(self.config, self.llm)
            answer.plan = planner.plan(question, describe_tools(self.tools), history_text=history_text)
            answer.llm_calls += 1
            # 退化路径用改写后的独立查询兜底，避免指代词检索失败
            if answer.plan.fallback and q_work != question and answer.plan.steps:
                answer.plan.steps[0].input["query"] = q_work
            if verbose:
                self._log_plan(answer.plan, history=history_text)

            # 2. 执行
            executor = Executor(self.config, self.tools, self._ctx)
            answer.steps = executor.execute(answer.plan)
            if verbose:
                for step in answer.steps:
                    print(f"  · {step.display()}")

            # 3. 生成（结合对话历史，保持连贯）
            answer.final_answer = self._synthesize(question, answer.plan, answer.steps, history_text)
            answer.llm_calls += 1
            answer.sources = self._collect_sources(answer.steps)
            answer.prompt_tokens = self.prompt_tokens
            answer.completion_tokens = self.completion_tokens
            answer.estimated_cost = self.estimated_cost
        except LLMError as exc:
            answer.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            answer.error = f"{type(exc).__name__}: {exc}"
        answer.total_latency_s = time.monotonic() - t0
        # 4. 写入记忆（失败的回答不记录）
        if answer.ok and answer.final_answer:
            self.memory.add(
                question, answer.final_answer, answer.sources, answer.plan.plan_summary
            )
        LOG.info(
            "ask | q=%s | %.1fs | calls=%d | in=%d out=%d | ¥%.4f | %s",
            question[:50].replace("\n", " "), answer.total_latency_s, answer.llm_calls,
            answer.prompt_tokens, answer.completion_tokens, answer.estimated_cost,
            answer.error or "ok",
        )
        return answer

    def _synthesize(self, question: str, plan: Plan, steps: list[StepResult], history_text: str = "") -> str:
        context_parts: list[str] = []
        citation = 1
        source_map: dict[str, list[int]] = {}
        for step in steps:
            if not step.ok or step.action == "none":
                continue
            out = step.output
            if isinstance(out, dict) and "text" in out:
                text = str(out["text"])
                srcs = out.get("sources") or []
                # 把工具输出里的 [n] 片段重编为全局引用号
                for src in srcs:
                    source_map.setdefault(src, []).append(citation)
                    citation += 1
                context_parts.append(f"【工具: {step.action}】\n{text}")

        history_block = f"对话历史：\n{history_text}\n\n" if history_text else ""
        user = (
            f"{history_block}"
            f"用户问题：{question}\n\n"
            f"规划：{plan.plan_summary or plan.reasoning or '(无)'}\n\n"
            "以下是工具执行结果（检索片段/计算等）：\n\n"
            + "\n\n".join(context_parts)
            + "\n\n请基于以上信息回答，正文标注 [n] 引用，并在末尾列出参考来源。"
            "如问题是对之前话题的追问或对比，请结合对话历史保持回答连贯。"
        )
        result = self.llm.chat(
            [
                {"role": "system", "content": SYNTHESIS_SYSTEM},
                {"role": "user", "content": user},
            ]
        )
        self._accumulate_usage(result)
        return result.text

    def _collect_sources(self, steps: list[StepResult]) -> list[str]:
        seen: list[str] = []
        for step in steps:
            out = step.output
            if isinstance(out, dict):
                for src in out.get("sources") or []:
                    if src not in seen:
                        seen.append(src)
        return seen

    def _accumulate_usage(self, result) -> None:
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.estimated_cost += LLMClient.estimate_cost(result)

    def _log_plan(self, plan: Plan, history: str = "") -> None:
        if history:
            print(f"  [mem] 对话历史 {self.memory.count} 轮")
        print(f"  [plan] {'⚠ 退化路径: ' if plan.fallback else ''}{plan.plan_summary or plan.reasoning}")
        for step in plan.steps:
            print(f"    · step{step.step_id}: {step.action} {step.input} — {step.purpose}")

    def reset_memory(self) -> None:
        """清空会话记忆，开始新一轮对话。"""
        self.memory.clear()
