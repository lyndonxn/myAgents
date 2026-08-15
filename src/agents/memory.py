"""会话记忆（SessionMemory）：多轮对话历史存储与格式化。

每一轮记录：问题、回答、引用来源、计划摘要。
- 按轮次截断（max_turns），防止上下文无限增长
- 格式化为 OpenAI 风格消息（user/assistant），供规划层与生成层使用
- 支持清空（/reset）
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

    def add(self, question: str, answer: str, sources: list[str], plan_summary: str = "") -> None:
        self.turns.append(Turn(question=question, answer=answer, sources=list(sources), plan_summary=plan_summary))
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]

    def clear(self) -> None:
        self.turns.clear()

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

    def as_text(self, max_chars: int = 2000) -> str:
        """紧凑文本形式（用于提示词内的『对话历史』段落）。"""
        if not self.turns:
            return ""
        parts = []
        for t in self.turns:
            ans = t.answer[:400].replace("\n", " ")
            parts.append(f"用户：{t.question}\n助手：{ans}")
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[-max_chars:]
        return text
