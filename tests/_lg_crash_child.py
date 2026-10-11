"""崩溃恢复演练子进程（spec/08 LG-ACC-006）：在合成 LLM 调用点 os._exit(9) 模拟 kill -9。

用法：python tests/_lg_crash_child.py <checkpoint_db 路径>
预期：图执行到 generate（LLM 合成）时进程以码 9 退出；此前 retrieve/clarify_check
两个 superstep 的 checkpoint 已落库，供父进程以 invoke(None) 续跑。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent import Agent
from agents.chunking import Chunk
from agents.config import Config
from agents.llm import ChatResult, LLMClient


class ExitLLM(LLMClient):
    """合成调用点直接退出进程（绕过一切异常处理，模拟 kill -9）。"""

    def __init__(self, config: Config):
        self.config = config

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(9)


def main() -> None:
    db = sys.argv[1]
    raw = {
        "data_dir": tempfile.mkdtemp(prefix="lg-crash-child-"),
        "embedding": {"backend": "tfidf", "hash_dim": 256},
        "retrieval": {"rerank": "off", "fusion_mode": "rrf"},
        "audit": {"enabled": False},
        "engine": "langgraph",
        "graph": {"checkpoint_db": db, "clarify_on_miss": False},
    }
    config = Config(raw)
    chunks = [
        Chunk(
            id=0,
            text="RAG 检索增强生成的核心是先检索再生成。叶子块用于精确召回，父块用于扩展上下文，RRF 融合向量与 BM25 两路排名。" * 3,
            file="a.md",
            heading="RAG",
            source="a.md",
        )
    ]
    agent = Agent(config, llm=ExitLLM(config), lazy_index=True)
    agent.build_index(chunks=chunks, persist=False)
    answer = agent.ask("什么是 RAG？", session_id="crash")
    print("UNREACHABLE:", answer.error)


if __name__ == "__main__":
    main()
