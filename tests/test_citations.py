"""S3 引用校验 + 评测增强测试：extract/validate、汇总指标、retrieval-only、Agent 集成、judge。

全部离线：LLM 用记录调用次数、按脚本返回文本的假客户端（子类化 LLMClient
覆写 _chat，不发真实请求）；Agent 不加载真实索引（直接注入假工具注册表）；
retrieval-only 用假 Agent/假检索器打桩。覆盖 spec ACC-S3-01..03 及边界用例。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.agent as agent_module
import benchmark.run_benchmark as rb
from agents.agent import Agent
from agents.citations import extract_citations, validate_citations
from agents.config import Config
from agents.llm import ChatResult, LLMClient
from agents.tools import Tool, ToolContext


class ScriptedLLM(LLMClient):
    """离线假 LLM：按脚本顺序返回文本，记录每次调用与收到的 messages。"""

    def __init__(self, texts: list[str], config: Config):
        self.config = config
        self._texts = list(texts)
        self.calls = 0
        self.seen: list[list[dict]] = []

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


class _ListHandler(logging.Handler):
    """收集日志消息的内存 handler（用于断言引用校验 warning）。"""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _make_tool(name: str, func, properties: dict, required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=f"测试工具 {name}",
        parameters={"type": "object", "properties": properties, "required": required},
        func=func,
    )


# ---------------- ACC-S3-01 引用校验 ----------------

def test_acc_s3_01_validate_basic():
    """ACC-S3-01：来源数 3，[1] 保留、[9] 移除，valid=1 invalid=1。"""
    text = "RAG 分为五个环节[1]，其中重排很关键[9]。"
    report = validate_citations(text, 3)

    assert report.valid_count == 1 and report.invalid_count == 1
    assert report.invalid_numbers == [9]
    assert "[1]" in report.cleaned_text and "[9]" not in report.cleaned_text
    assert report.cleaned_text == "RAG 分为五个环节[1]，其中重排很关键。", \
        f"只删标记本身，其余文字原样: {report.cleaned_text!r}"

    # 重复出现的非法编号去重记录，计数按出现次数
    report2 = validate_citations("[2] 与 [7] 与 [2]", 1)
    assert report2.valid_count == 0 and report2.invalid_count == 3
    assert report2.invalid_numbers == [2, 7]  # 去重、按首次出现顺序
    assert report2.cleaned_text == " 与  与 ", f"只删标记本身: {report2.cleaned_text!r}"

    print("✓ ACC-S3-01 [1] 保留 [9] 移除，valid/invalid 计数正确")


def test_extract_and_false_positives():
    """非数字 [abc]、双方括号 [[wiki]]、Markdown 链接 [1]: url 不误判。"""
    text = "见[1]与[2]；[abc] 不是引用；[[wiki]] 不是；[10] 是。"
    assert extract_citations(text) == [1, 2, 10]

    # Markdown 链接定义 [1]: url：[1] 不算引用，保持原样
    md = "[1]: https://example.com/a\n正文引用[2]"
    assert extract_citations(md) == [2]
    rep = validate_citations(md, 5)
    assert rep.cleaned_text == md, "误判排除后文本应原样保留"
    assert rep.valid_count == 1 and rep.invalid_count == 0

    # 双方括号内的数字不命中
    assert extract_citations("[[1]] 与 [[wiki]]") == []

    print("✓ [abc]/[[wiki]]/[1]: url 不误判，合法 [n] 正常提取")


def test_validate_edge_cases():
    """空引用文本、全非法、[0]、[10] 边界（n_sources=10 合法 / 超界非法）。"""
    # 空引用：文本原样，计数为 0
    rep = validate_citations("没有任何引用标记", 3)
    assert (rep.valid_count, rep.invalid_count) == (0, 0)
    assert rep.cleaned_text == "没有任何引用标记" and rep.invalid_numbers == []

    # 全非法：全部移除
    rep2 = validate_citations("[2] 开头，[7] 结尾", 1)
    assert (rep2.valid_count, rep2.invalid_count) == (0, 2)
    assert rep2.cleaned_text == " 开头， 结尾"

    # [0]：编号 < 1 非法
    rep3 = validate_citations("引用[0]", 3)
    assert rep3.invalid_count == 1 and rep3.invalid_numbers == [0]
    assert rep3.cleaned_text == "引用"

    # 边界：n_sources=10 时 [10] 合法、[11] 非法；n_sources=9 时 [10] 非法
    assert validate_citations("尾号[10]", 10).valid_count == 1
    rep4 = validate_citations("尾号[11]", 10)
    assert rep4.invalid_count == 1 and rep4.cleaned_text == "尾号"
    assert validate_citations("尾号[10]", 9).invalid_count == 1

    # 空文本
    rep5 = validate_citations("", 3)
    assert rep5.cleaned_text == "" and rep5.valid_count == 0 and rep5.invalid_count == 0

    print("✓ 空引用 / 全非法 / [0] / [10] 边界 / 空文本")


# ---------------- ACC-S3-02 汇总指标 ----------------

def _rec(qid, kw, src, lat=1.0, cost=0.01, valid=0, invalid=0, fallback=False, judge=None):
    """构造与 run_benchmark 逐题 record 同构的测试记录。"""
    return {
        "id": qid,
        "question": qid,
        "plan": {"fallback": fallback, "steps": []},
        "judge": judge,
        "metrics": {
            "keyword_hit": kw,
            "source_hit": src,
            "latency_s": lat,
            "cost_yuan": cost,
            "citations_valid": valid,
            "citations_invalid": invalid,
        },
    }


def test_acc_s3_02_summarize_results():
    """ACC-S3-02：构造 records 调 summarize_results，completion_rate/hallucination_rate 数值正确。"""
    results = [
        _rec("a", 1.0, 1.0, lat=1.0, cost=0.01, valid=2, invalid=0),
        _rec("b", 0.6, 0.4, lat=2.0, cost=0.01, valid=1, invalid=1),  # src 0.4 < 0.5 → 未完成
        _rec("c", 0.5, 1.0, lat=3.0, cost=0.01, valid=0, invalid=2),  # kw 0.5 >= 0.5 → 完成
        _rec("d", 0.0, 0.0, lat=4.0, cost=0.01, fallback=True),
    ]
    s = rb.summarize_results(results, threshold=0.5)

    assert s["questions"] == 4
    assert s["completion_rate"] == 0.5, "a、c 完成各 0.5"
    assert s["hallucination_rate"] == 0.5, "invalid 合计 3 / (valid 3 + invalid 3)"
    assert s["avg_latency_s"] == 2.5 and s["max_latency_s"] == 4.0
    assert s["total_cost_yuan"] == 0.04
    assert s["fallback_rate"] == 0.25
    assert s["judge_avg"] is None, "未启用 judge 时为 None"

    # 阈值变化影响完成判定：阈值 0.7 时只有 a 完成
    assert rb.summarize_results(results, threshold=0.7)["completion_rate"] == 0.25

    # judge 均分只统计 score 非 None 的记录
    results_j = [
        _rec("a", 1.0, 1.0, judge={"score": 2, "reason": "有依据"}),
        _rec("b", 0.0, 0.0, judge={"score": None, "reason": "评审失败"}),
        _rec("c", 1.0, 1.0, judge={"score": 1, "reason": "部分"}),
    ]
    assert rb.summarize_results(results_j, 0.5)["judge_avg"] == 1.5

    # 空结果不崩溃
    s0 = rb.summarize_results([], 0.5)
    assert s0["questions"] == 0 and s0["completion_rate"] == 0.0 and s0["judge_avg"] is None

    print("✓ ACC-S3-02 summarize_results 数值正确（含阈值变化 / judge / 空表）")


# ---------------- ACC-S3-03 retrieval-only 行为不变 ----------------

class _FakeHit:
    def __init__(self, file: str, heading: str):
        self.file = file
        self.chunk = SimpleNamespace(heading=heading)


class _FakeRetriever:
    """按问题返回固定命中序列的假检索器。"""

    def __init__(self, hits_by_question: dict[str, list[_FakeHit]]):
        self._hits = hits_by_question

    def retrieve(self, query: str, top_k: int = 5):
        return self._hits.get(query, [])[:top_k]


class _FakeAgent:
    """替身 Agent：跳过真实索引加载（offline）。"""

    def __init__(self, config):
        self.config = config
        self.retriever = _FakeRetriever({})

    def load_index(self):
        return self


def test_acc_s3_03_retrieval_only_unchanged():
    """ACC-S3-03：用假 Agent/假检索器跑 run_retrieval_only，行为与改动前一致。"""
    questions = [
        {"id": "x1", "question": "什么是 RAG",
         "expected_files": ["rag.md"], "expected_sections": ["整体流程"]},
        {"id": "x2", "question": "MCP 是什么", "expected_files": [], "expected_sections": []},
    ]
    fake = _FakeAgent(SimpleNamespace(top_k=2))
    fake.retriever._hits = {
        "什么是 RAG": [_FakeHit("rag.md", "RAG/整体流程"), _FakeHit("other.md", "别的章节")],
        "MCP 是什么": [_FakeHit("mcp.md", "MCP/三个参与者")],
    }

    # T4 起 run_retrieval_only 支持 agent 注入（--kb 内存索引场景），测试直接注入假 Agent
    rows = rb.run_retrieval_only(questions, SimpleNamespace(top_k=6), top_k=2, agent=fake)

    assert len(rows) == 2
    qid, q, src, sec, files = rows[0]
    assert qid == "x1" and q == "什么是 RAG"
    assert src == 1.0 and sec == 1.0, "rag.md 与 整体流程 均命中"
    assert files == ["rag.md", "other.md"]
    assert rows[1][2] == 1.0 and rows[1][3] == 1.0, "空期望默认全命中"

    # 未命中场景：hit 值正确降低，不抛异常
    fake.retriever._hits["什么是 RAG"] = [_FakeHit("unrelated.md", "无关章节")]
    rows2 = rb.run_retrieval_only([questions[0]], SimpleNamespace(top_k=6), top_k=2, agent=fake)
    assert rows2[0][2] == 0.0 and rows2[0][3] == 0.0

    print("✓ ACC-S3-03 run_retrieval_only 既有行为不变（假检索器跑通）")


# ---------------- Agent 集成：引用校验接入 ask ----------------

def test_agent_citation_validation():
    """ask 集成：按 len(sources) 校验正文 [n]，非法引用被剔除并回填计数、LOG.warning。"""
    def probe(ctx, query=""):
        return {"text": f"检索 {query} 的片段", "sources": ["kb://a.md", "kb://b.md", "kb://c.md"]}

    tools = {"search_knowledge_base": _make_tool(
        "search_knowledge_base", probe, {"query": {"type": "string"}}, ["query"]
    )}
    plan_json = json.dumps({
        "reasoning": "检索", "plan_summary": "检索一步",
        "steps": [{"action": "search_knowledge_base", "input": {"query": "RAG"}, "purpose": ""}],
    }, ensure_ascii=False)
    reflect_json = json.dumps({"need_more": False, "reasoning": "信息足够", "steps": []}, ensure_ascii=False)
    synth_text = "RAG 是检索增强生成[1]。编造的编号[9]不存在。"

    llm = ScriptedLLM([plan_json, reflect_json, synth_text], Config({"tools": {"max_retries": 0}}))
    agent = Agent(Config({"tools": {"max_retries": 0}}), llm=llm, lazy_index=True)
    agent._index_loaded = True  # 跳过索引加载
    agent.tools = tools
    agent._ctx = ToolContext()

    handler = _ListHandler()
    logger = logging.getLogger("agent")
    logger.addHandler(handler)
    try:
        answer = agent.ask("什么是 RAG")
    finally:
        logger.removeHandler(handler)

    assert answer.ok, f"ask 不应失败: {answer.error}"
    assert len(answer.sources) == 3, "3 个来源 → [1] 合法、[9] 非法"
    assert answer.citations_valid == 1 and answer.citations_invalid == 1
    assert answer.final_answer == "RAG 是检索增强生成[1]。编造的编号不存在。", \
        f"非法标记被剔除且不动周围文字: {answer.final_answer!r}"
    joined = "\n".join(handler.messages)
    assert "非法引用" in joined and "9" in joined, f"invalid>0 应 LOG.warning: {joined}"
    assert llm.calls == 3, f"规划+反思+合成共 3 次调用，实际 {llm.calls}"

    # 全部引用合法时不告警、文本不变
    llm2 = ScriptedLLM([plan_json, reflect_json, "干净答案[1]与[2]"], Config({"tools": {"max_retries": 0}}))
    agent2 = Agent(Config({"tools": {"max_retries": 0}}), llm=llm2, lazy_index=True)
    agent2._index_loaded = True
    agent2.tools = tools
    agent2._ctx = ToolContext()
    answer2 = agent2.ask("什么是 RAG")
    assert answer2.final_answer == "干净答案[1]与[2]"
    assert answer2.citations_valid == 2 and answer2.citations_invalid == 0

    print("✓ Agent.ask 引用校验集成（cleaned 回填 / 计数 / warning / 合法不告警）")


# ---------------- judge 评审 ----------------

def test_judge_answer():
    """--judge 评审：正常打分；解析失败 score=None（含修复轮）；越界分值记 None。"""
    # 正常：score 2
    llm_ok = ScriptedLLM(['{"score": 2, "reason": "关键论断在来源中均有依据"}'], Config({}))
    res = rb.judge_answer(llm_ok, "问题", "答案", ["kb://a.md"])
    assert res["score"] == 2 and "依据" in res["reason"]
    assert "检索到的来源" in llm_ok.seen[0][1]["content"], "评审 prompt 应携带问题与来源"

    # 解析失败（修复轮也坏）→ score=None，不抛异常
    llm_bad = ScriptedLLM(["完全不是 JSON", "仍然不是 JSON"], Config({}))
    res2 = rb.judge_answer(llm_bad, "问题", "答案", [])
    assert res2["score"] is None and res2["reason"]
    assert llm_bad.calls == 2, f"chat_json 含 1 轮修复共 2 次调用，实际 {llm_bad.calls}"

    # 越界分值 → None
    llm_range = ScriptedLLM(['{"score": 5, "reason": "x"}'], Config({}))
    assert rb.judge_answer(llm_range, "q", "a", [])["score"] is None

    print("✓ judge 评审（正常打分 / 解析失败 score=None / 越界记 None）")


if __name__ == "__main__":
    test_acc_s3_01_validate_basic()
    test_extract_and_false_positives()
    test_validate_edge_cases()
    test_acc_s3_02_summarize_results()
    test_acc_s3_03_retrieval_only_unchanged()
    test_agent_citation_validation()
    test_judge_answer()
    print("\nS3 引用校验与评测增强测试全部通过 ✅")
