"""评测脚本：用题库跑 Agent，采集指标。

输出 benchmark/results.json 与 Markdown 表格。同一份题库可用于后续
在 Codex 上运行并对比（见 benchmark/README.md）。

指标：答案、耗时、Token 用量、估算成本、检索命中率
（期望关键词是否出现在答案中 / 期望来源文件是否被召回）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.agent import Agent  # noqa: E402
from agents.config import load_config  # noqa: E402
from agents.llm import LLMClient  # noqa: E402

ROOT = Path(__file__).resolve().parent


def keyword_hit(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    hits = sum(1 for k in keywords if k.lower() in answer.lower())
    return hits / len(keywords)


def source_hit(retrieved_sources: list[str], expected_files: list[str]) -> float:
    if not expected_files:
        return 1.0
    joined = " | ".join(retrieved_sources)
    hits = sum(1 for ef in expected_files if ef in joined)
    return hits / len(expected_files)


def section_hit(retrieved_chunks, expected_sections: list[str]) -> float:
    """章节级命中：检索到的片段标题路径是否包含期望章节（子串匹配）。"""
    if not expected_sections:
        return 1.0
    joined = " | ".join(h.chunk.heading for h in retrieved_chunks)
    hits = sum(1 for es in expected_sections if es in joined)
    return hits / len(expected_sections)


def run_retrieval_only(questions: list[dict], config, top_k: int | None = None) -> None:
    """只测检索命中率：对每题直接检索，检查期望来源文件/章节是否被召回（不调 LLM）。"""
    from agents.agent import Agent

    agent = Agent(config)
    agent.load_index()
    k = top_k or config.top_k

    print(f"检索命中检查（top_k={k}，不调用 LLM）：")
    rows = []
    for item in questions:
        qid = item["id"]
        hits = agent.retriever.retrieve(item["question"], top_k=k)
        retrieved_files = [h.file for h in hits]
        src = source_hit(retrieved_files, item.get("expected_files", []))
        sec = section_hit(hits, item.get("expected_sections", []))
        rows.append((qid, item["question"], src, sec, retrieved_files))
        print(f"  [{qid}] file={src:.0%} section={sec:.0%} | {item['question'][:36]}")
        if src < 1.0 or sec < 1.0:
            for h in hits[:3]:
                print(f"      ↳ {h.file} | {h.chunk.heading[:50]}")

    avg_file = sum(r[2] for r in rows) / len(rows)
    avg_sec = sum(r[3] for r in rows) / len(rows)
    print(f"\n平均：文件命中 {avg_file:.1%} / 章节命中 {avg_sec:.1%}（top_k={k}）")
    return rows


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="myAgents 评测")
    parser.add_argument("--retrieval-only", action="store_true",
                        help="只测检索命中率（不调用 LLM，免费快速验证召回质量）")
    parser.add_argument("--top-k", type=int, default=None, help="检索返回数（默认取配置）")
    parser.add_argument("--rerank", choices=["off", "auto", "cross_encoder", "llm"], default=None,
                        help="覆盖重排模式（默认取配置）")
    parser.add_argument("--multi-query", action="store_true", help="开启多查询扩展")
    args = parser.parse_args()

    questions = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
    config = load_config()
    if args.rerank is not None:
        config._raw.setdefault("retrieval", {})["rerank"] = args.rerank
    if args.multi_query:
        config._raw.setdefault("retrieval", {})["multi_query"] = True
    if args.retrieval_only:
        run_retrieval_only(questions, config, top_k=args.top_k)
        return

    llm = LLMClient(config)
    agent = Agent(config, llm=llm)
    agent.load_index()

    results = []
    for item in questions:
        qid = item["id"]
        t0 = time.monotonic()
        answer = agent.ask(item["question"])
        latency = time.monotonic() - t0
        record = {
            "id": qid,
            "question": item["question"],
            "answer": answer.final_answer,
            "plan": {
                "summary": answer.plan.plan_summary,
                "steps": [
                    {"action": s.action, "input": s.input, "ok": s.ok, "error": s.error}
                    for s in answer.steps
                ],
                "fallback": answer.plan.fallback,
            },
            "sources": answer.sources,
            "metrics": {
                "latency_s": round(latency, 2),
                "agent_time_s": round(answer.total_latency_s, 2),
                "prompt_tokens": answer.prompt_tokens,
                "completion_tokens": answer.completion_tokens,
                "cost_yuan": round(answer.estimated_cost, 5),
                "keyword_hit": round(keyword_hit(answer.final_answer, item.get("expected_keywords", [])), 2),
                "source_hit": round(source_hit(answer.sources, item.get("expected_files", [])), 2),
            },
            "error": answer.error,
        }
        m = record["metrics"]
        print(f"[{qid}] kw={m['keyword_hit']:.0%} src={m['source_hit']:.0%} "
              f"{m['latency_s']:.1f}s cost=¥{m['cost_yuan']:.4f} | {item['question'][:36]}")
        results.append(record)

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "engine": "myAgents", "results": results}
    out_path = ROOT / "results.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {out_path}")

    # Markdown 汇总表
    md = ["| id | 问题 | 关键词命中 | 来源命中 | 延迟s | 成本¥ | 计划步骤 | 退化 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        m = r["metrics"]
        steps = len(r["plan"]["steps"])
        md.append(f"| {r['id']} | {r['question'][:32]} | {m['keyword_hit']:.0%} | "
                  f"{m['source_hit']:.0%} | {m['latency_s']} | {m['cost_yuan']:.4f} | {steps} | "
                  f"{'是' if r['plan']['fallback'] else ''} |")
    avg_kw = sum(r["metrics"]["keyword_hit"] for r in results) / len(results)
    avg_src = sum(r["metrics"]["source_hit"] for r in results) / len(results)
    md.append(f"| **平均** | | **{avg_kw:.0%}** | **{avg_src:.0%}** | | | | |")
    (ROOT / "results.md").write_text("\n".join(md), encoding="utf-8")
    print(f"汇总表已写入 {ROOT / 'results.md'}")


if __name__ == "__main__":
    main()
