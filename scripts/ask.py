"""CLI 问答入口。

用法：
  python -m scripts.ask --question "RAG 的完整流程是什么？"
  python -m scripts.ask                          # 交互模式
  python -m scripts.ask --question "..." --verbose   # 显示规划与工具调用
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.agent import Agent  # noqa: E402
from agents.config import load_config  # noqa: E402
from agents.llm import LLMClient  # noqa: E402


def render(answer) -> str:
    lines = ["=" * 60, f"问题：{answer.question}", "-" * 60, answer.final_answer or "(无答案)", "-" * 60]
    if answer.sources:
        lines.append("参考来源：")
        for i, src in enumerate(answer.sources, 1):
            lines.append(f"  [{i}] {src}")
    lines.append("-" * 60)
    lines.append(
        f"耗时 {answer.total_latency_s:.1f}s | LLM 调用 {answer.llm_calls} 次 | "
        f"tokens in={answer.prompt_tokens} out={answer.completion_tokens} | 估算成本 ¥{answer.estimated_cost:.4f}"
    )
    if answer.error:
        lines.append(f"⚠ 错误：{answer.error}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="myAgents 知识库问答")
    parser.add_argument("--question", "-q", help="单次提问；不填则进入交互模式")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示规划与工具调用细节")
    parser.add_argument("--rerank", action="store_true", help="开启 LLM 重排")
    args = parser.parse_args()

    config = load_config()
    if args.rerank:
        config._raw.setdefault("retrieval", {})["rerank"] = "cross_encoder"

    llm = LLMClient(config)
    agent = Agent(config, llm=llm)
    agent.load_index()

    if args.question:
        print(render(agent.ask(args.question, verbose=args.verbose)))
        return

    print("myAgents 交互问答（多轮对话；/reset 清空记忆，/mem 查看记忆，exit 退出）")
    while True:
        try:
            q = input("\n❯ ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit"):
            break
        if q in ("/reset", "/clear"):
            agent.reset_memory()
            print("（已清空会话记忆）")
            continue
        if q == "/mem":
            if agent.memory.is_empty:
                print("（暂无记忆）")
            else:
                for i, t in enumerate(agent.memory.turns, 1):
                    print(f"  {i}. 问: {t.question} | 答: {t.answer[:60]}…")
            continue
        print(render(agent.ask(q, verbose=args.verbose)))


if __name__ == "__main__":
    main()
