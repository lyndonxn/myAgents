"""命令行入口：构建知识库索引。

用法:
  python -m scripts.build_index
  python -m scripts.build_index --augment   # 启用上下文增强（Anthropic Contextual Retrieval，需 API Key）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.agent import Agent  # noqa: E402
from agents.config import load_config  # noqa: E402
from agents.llm import LLMClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="构建知识库索引")
    parser.add_argument("--augment", action="store_true",
                        help="用 LLM 为每个父块生成上下文摘要（Contextual Retrieval，一次性成本）")
    args = parser.parse_args()

    config = load_config()
    print(f"[config] 知识库: {config.kb_path}")
    llm = LLMClient(config) if args.augment else None
    agent = Agent(config, llm=llm)
    agent.build_index(augment=args.augment)
    print("[done] 索引构建完成。")


if __name__ == "__main__":
    main()
