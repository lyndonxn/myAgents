"""会话记忆（SessionMemory）：多轮对话历史存储与格式化。

每一轮记录：问题、回答、引用来源、计划摘要。
- 按轮次截断（max_turns），防止上下文无限增长
- 格式化为 OpenAI 风格消息（user/assistant），供规划层与生成层使用
- 支持清空（/reset）
- 摘要压缩（S4）：滑出窗口的轮次经 maybe_compress 合并为 summary，
  as_text() 在历史前注入摘要块，长会话上下文不丢失
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    question: str
    answer: str
    sources: list[str] = field(default_factory=list)
    plan_summary: str = ""


class SessionMemory:
    def __init__(self, max_turns: int = 6):
        self.max_turns = max_turns
        self.turns: list[Turn] = []
        self.summary: str = ""                    # 滑出窗口轮次的压缩摘要（S4）
        self._pending_evicted: list[Turn] = []    # add 截断时滑出的轮次，待 maybe_compress 消费

    def add(self, question: str, answer: str, sources: list[str], plan_summary: str = "") -> None:
        self.turns.append(Turn(question=question, answer=answer, sources=list(sources), plan_summary=plan_summary))
        if len(self.turns) > self.max_turns:
            # 滑出窗口的轮次先停放，供 maybe_compress 压缩为摘要（S4）
            self._pending_evicted.extend(self.turns[: len(self.turns) - self.max_turns])
            self.turns = self.turns[-self.max_turns:]

    def clear(self) -> None:
        self.turns.clear()
        self.summary = ""
        self._pending_evicted.clear()

    @property
    def is_empty(self) -> bool:
        return not self.turns

    @property
    def count(self) -> int:
        return len(self.turns)

    def as_messages(self) -> list[dict]:
        """转为 [{role, content}] 消息序列，供 LLM 使用。"""
        messages: list[dict] = []
        for t in self.turns:
            messages.append({"role": "user", "content": t.question})
            messages.append({"role": "assistant", "content": t.answer})
        return messages

    def maybe_compress(self, llm=None, compressor=None) -> str:
        """把滑出窗口的轮次压缩为摘要（S4）；返回当前 summary。

        压缩来源：优先用传入的 compressor；否则用 llm 构造 MemoryCompressor；
        两者皆无或压缩异常时降级为"旧摘要 + 各轮首句"本地拼接（≤600 字），不抛异常。
        """
        evicted: list[Turn] = list(self._pending_evicted)
        self._pending_evicted = []
        if len(self.turns) > self.max_turns:
            evicted.extend(self.turns[: len(self.turns) - self.max_turns])
            self.turns = self.turns[-self.max_turns:]
        if not evicted:
            return self.summary
        if compressor is not None:
            try:
                self.summary = compressor.compress(self.summary, evicted)
                return self.summary
            except Exception:  # noqa: BLE001 - 压缩失败必须降级而非中断
                pass
        elif llm is not None:
            try:
                from .long_memory import MemoryCompressor

                self.summary = MemoryCompressor(llm).compress(self.summary, evicted)
                return self.summary
            except Exception:  # noqa: BLE001
                pass
        from .long_memory import fallback_summary

        self.summary = fallback_summary(self.summary, evicted)
        return self.summary

    def as_text(self, max_chars: int = 2000) -> str:
        """紧凑文本形式（用于提示词内的『对话历史』段落）；有摘要时在历史前加摘要块。"""
        parts: list[str] = []
        if self.summary:
            parts.append(f"此前对话摘要：{self.summary}")
        if self.turns:
            history = []
            for t in self.turns:
                ans = t.answer[:400].replace("\n", " ")
                history.append(f"用户：{t.question}\n助手：{ans}")
            parts.append("\n\n".join(history))
        if not parts:
            return ""
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[-max_chars:]
        return text
