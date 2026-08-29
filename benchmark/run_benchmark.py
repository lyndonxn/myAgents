"""评测脚本：用题库跑 Agent，采集指标。

输出 benchmark/results.json 与 Markdown 表格。同一份题库可用于后续
在 Codex 上运行并对比（见 benchmark/README.md）。

指标：答案、耗时、Token 用量、估算成本、检索命中率
（期望关键词是否出现在答案中 / 期望来源文件是否被召回）、
引用幻觉率（非法 [n] 引用占比，S3 引用校验）与任务完成率
（keyword_hit 与 source_hit 同时达到阈值）。

可选 --judge（默认关，付费）：用 LLM 评审每题答案是否被检索证据
支撑（0=无证据支撑 / 1=部分支撑 / 2=有证据支撑），输出 JSON。

T4 起 --kb/--questions 支持换库换题评测：--kb 用样例/临时知识库在内存中
重建索引（build_index(persist=False)，绝不写 data/index* 与 chunks.json），
--questions 换用其他题库（如 questions_sample.json）。均只影响本次运行，
不带参数时行为与从前完全一致。
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


def summarize_results(results: list[dict], threshold: float = 0.5) -> dict:
    """纯函数汇总：按 results 记录计算各项总览指标。

    - completion_rate：task_completed 比例（keyword_hit 与 source_hit 均达 threshold）；
    - hallucination_rate：全部题目非法引用合计 /（合法+非法）合计，无引用记 0；
    - 另含平均/最大延迟、总成本、退化率（fallback 题数占比）与 judge 均分。
    """
    n = len(results)
    if n == 0:
        return {
            "questions": 0, "threshold": threshold, "completion_rate": 0.0,
            "hallucination_rate": 0.0, "avg_latency_s": 0.0, "max_latency_s": 0.0,
            "total_cost_yuan": 0.0, "fallback_rate": 0.0, "judge_avg": None,
        }
    completed = sum(
        1 for r in results
        if r["metrics"]["keyword_hit"] >= threshold and r["metrics"]["source_hit"] >= threshold
    )
    total_valid = sum(r["metrics"].get("citations_valid", 0) for r in results)
    total_invalid = sum(r["metrics"].get("citations_invalid", 0) for r in results)
    latencies = [r["metrics"]["latency_s"] for r in results]
    fallbacks = sum(1 for r in results if r.get("plan", {}).get("fallback"))
    scores = [
        r["judge"]["score"] for r in results
        if isinstance(r.get("judge"), dict) and r["judge"].get("score") is not None
    ]
    return {
        "questions": n,
        "threshold": threshold,
        "completion_rate": completed / n,
        "hallucination_rate": (total_invalid / (total_valid + total_invalid)) if (total_valid + total_invalid) else 0.0,
        "avg_latency_s": round(sum(latencies) / n, 2),
        "max_latency_s": round(max(latencies), 2),
        "total_cost_yuan": round(sum(r["metrics"]["cost_yuan"] for r in results), 5),
        "fallback_rate": fallbacks / n,
        "judge_avg": round(sum(scores) / len(scores), 2) if scores else None,
    }


JUDGE_SYSTEM = (
    "你是问答质量评审员。判断候选答案是否被检索证据支撑："
    "2=有证据支撑（关键论断在来源中找得到依据）；1=部分支撑；0=无证据支撑（疑似编造）。"
    '只输出 JSON：{"score": 0|1|2, "reason": "一句话理由"}'
)


def judge_answer(llm, question: str, answer_text: str, sources: list[str]) -> dict:
    """LLM 评审答案的证据支撑度（0/1/2 分）；解析失败记 score=None，不抛异常。"""
    src_block = "\n".join(f"- {s}" for s in sources) or "-（无来源）"
    user = (
        f"问题：{question}\n\n检索到的来源：\n{src_block}\n\n"
        f"候选答案：\n{answer_text[:2000]}\n\n请评审答案是否被上述检索证据支撑，只输出 JSON。"
    )
    try:
        obj = llm.chat_json(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001 - 评审失败不阻塞评测
        return {"score": None, "reason": f"评审失败: {exc}"}
    score = obj.get("score") if isinstance(obj, dict) else None
    if isinstance(score, bool) or score not in (0, 1, 2):
        score = None
    reason = str(obj.get("reason", ""))[:300] if isinstance(obj, dict) else ""
    return {"score": score, "reason": reason}


def build_benchmark_agent(config, kb: str | None = None, llm: LLMClient | None = None) -> Agent:
    """构造评测用 Agent（T4 --kb 支持）。

    带 kb 时：本次运行临时覆盖 kb_path，并跳过 load_index（会从 data/ 读用户旧索引，
    索引缺失时还会重建并落盘、污染用户数据），改为用 build_index(persist=False)
    在内存中重建该库索引——绝不写 data/index* 与 data/chunks.json。
    不带 kb 时：保持原行为——load_index() 从 data/ 加载既有索引。
    仅影响本次运行，不修改任何配置文件。
    """
    if kb:
        config._raw["kb_path"] = str(Path(kb).expanduser().resolve())
        # augment=True 时 build_index 会写 data/contexts.json 缓存——评测路径显式禁用，保证零落盘
        return Agent(config, llm=llm).build_index(persist=False, augment=False)
    agent = Agent(config, llm=llm)
    agent.load_index()
    return agent


def run_retrieval_only(
    questions: list[dict], config, top_k: int | None = None, agent: Agent | None = None
) -> list[tuple]:
    """只测检索命中率：对每题直接检索，检查期望来源文件/章节是否被召回（不调 LLM）。

    agent 可注入（T4 --kb 场景：传入 build_benchmark_agent 用样例库内存构建的
    Agent）；默认 None 时保持原行为：构造 Agent 并 load_index()。
    """
    if agent is None:
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
    parser.add_argument("--complete-threshold", type=float, default=0.5,
                        help="任务完成判定阈值：keyword_hit 与 source_hit 均需 >= 该值（默认 0.5）")
    parser.add_argument("--kb", type=str, default=None,
                        help="覆盖知识库路径（如 samples/kb）：内存构建索引，绝不写 data/，仅本次运行生效")
    parser.add_argument("--questions", type=str, default=None,
                        help="题库 JSON 路径（默认 benchmark/questions.json）")
    parser.add_argument("--judge", action="store_true",
                        help="用 LLM 评审每题答案的证据支撑度（付费，默认关闭）")
    args = parser.parse_args()

    questions_path = Path(args.questions).expanduser() if args.questions else ROOT / "questions.json"
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    config = load_config()
    if args.rerank is not None:
        config._raw.setdefault("retrieval", {})["rerank"] = args.rerank
    if args.multi_query:
        config._raw.setdefault("retrieval", {})["multi_query"] = True
    if args.retrieval_only:
        agent = build_benchmark_agent(config, kb=args.kb)
        run_retrieval_only(questions, config, top_k=args.top_k, agent=agent)
        return

    llm = LLMClient(config)
    agent = build_benchmark_agent(config, kb=args.kb, llm=llm)
    threshold = args.complete_threshold

    results = []
    for item in questions:
        qid = item["id"]
        t0 = time.monotonic()
        # Agent.ask 返回会话累计用量（复用同一 Agent）——按题差值记录单题真实用量
        prev_in, prev_out, prev_cost = agent.prompt_tokens, agent.completion_tokens, agent.estimated_cost
        answer = agent.ask(item["question"])
        latency = time.monotonic() - t0
        q_in = agent.prompt_tokens - prev_in
        q_out = agent.completion_tokens - prev_out
        q_cost = agent.estimated_cost - prev_cost
        kw = keyword_hit(answer.final_answer, item.get("expected_keywords", []))
        src = source_hit(answer.sources, item.get("expected_files", []))
        cit_valid, cit_invalid = answer.citations_valid, answer.citations_invalid
        cit_total = cit_valid + cit_invalid
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
                "prompt_tokens": q_in,
                "completion_tokens": q_out,
                "cost_yuan": round(q_cost, 5),
                "keyword_hit": round(kw, 2),
                "source_hit": round(src, 2),
                "citations_valid": cit_valid,
                "citations_invalid": cit_invalid,
                "citation_hallucination": round(cit_invalid / cit_total, 4) if cit_total else 0.0,
                "task_completed": 1.0 if (kw >= threshold and src >= threshold) else 0.0,
            },
            "judge": judge_answer(llm, item["question"], answer.final_answer, answer.sources) if args.judge else None,
            "error": answer.error,
        }
        m = record["metrics"]
        print(f"[{qid}] kw={m['keyword_hit']:.0%} src={m['source_hit']:.0%} "
              f"halluc={m['citation_hallucination']:.0%} done={'是' if m['task_completed'] else '否'} "
              f"{m['latency_s']:.1f}s cost=¥{m['cost_yuan']:.4f} | {item['question'][:36]}")
        results.append(record)

    summary = summarize_results(results, threshold=threshold)
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "engine": "myAgents",
           "summary": summary, "results": results}
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

    # 汇总段（S3）
    md += [
        "",
        "## 汇总",
        f"- 题数：{summary['questions']}（任务完成阈值 {summary['threshold']}）",
        f"- 任务完成率 completion_rate：{summary['completion_rate']:.0%}",
        f"- 引用幻觉率 hallucination_rate：{summary['hallucination_rate']:.1%}",
        f"- 平均 / 最大延迟：{summary['avg_latency_s']:.1f}s / {summary['max_latency_s']:.1f}s",
        f"- 总成本：¥{summary['total_cost_yuan']:.4f}",
        f"- 退化率（fallback 题数占比）：{summary['fallback_rate']:.0%}",
    ]
    if summary["judge_avg"] is not None:
        md.append(f"- LLM 评审均分 judge_avg：{summary['judge_avg']:.2f} / 2")
    (ROOT / "results.md").write_text("\n".join(md), encoding="utf-8")
    print(f"汇总表已写入 {ROOT / 'results.md'}")


if __name__ == "__main__":
    main()
