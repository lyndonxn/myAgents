"""T4 样例库评测测试（ACC-T4-01..03）。

全部离线、不调用 LLM：
- ACC-T4-01：真实跑 run_retrieval_only（samples/kb + questions_sample.json），
  断言平均文件命中 ≥0.8，且运行前后 data/ 下用户索引文件（index*、chunks.json）
  的存在性与 mtime 完全不变（--kb 内存构建零污染）；
- ACC-T4-02：打桩 Agent.build_index / load_index 后分别以「带 --kb」与「不带 --kb」
  跑 main()，断言 --kb 路径只调 build_index(persist=False)、绝不 load_index；
  默认路径保持原行为（load_index、不构建）；
- ACC-T4-03：校验 5 篇样例文档结构（非空、1 个一级标题 + 多个二级标题）与 10 题
  字段完整性、expected_files 与样例文件名匹配、expected_sections 与目标文档标题匹配。

embedding 用本机缓存模型（与既有 --retrieval-only 行为一致，已缓存时不发网络请求）；
ACC-T4-02 的打桩路径完全不加载模型。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

SAMPLES_KB = ROOT / "samples" / "kb"
QUESTIONS_SAMPLE = ROOT / "benchmark" / "questions_sample.json"
# 用户索引产物：--kb 评测路径绝不触碰（ACC-T4-02 口径：data/index.* 与 data/chunks.json）
INDEX_FILES = ("index.npy", "index.json", "chunks.json")
# 改写/意译题（问题文本不得出现期望文档标题的连续片段）
PARAPHRASE_IDS = {"q04", "q09", "q10"}


def _doc_headings(text: str) -> tuple[list[str], list[str]]:
    """提取一级/二级标题文本。"""
    h1 = [m.group(1).strip() for l in text.splitlines() if (m := re.match(r"^#\s+(.+)$", l))]
    h2 = [m.group(1).strip() for l in text.splitlines() if (m := re.match(r"^##\s+(.+)$", l))]
    return h1, h2


def _index_snapshot() -> dict[str, tuple[bool, int | None]]:
    """data/ 用户索引文件的存在性与 mtime 快照。"""
    out: dict[str, tuple[bool, int | None]] = {}
    for name in INDEX_FILES:
        p = ROOT / "data" / name
        out[name] = (p.exists(), p.stat().st_mtime_ns if p.exists() else None)
    return out


class BenchmarkSampleTests(unittest.TestCase):
    """spec ACC-T4-01..03：样例库结构 / --kb 内存构建零污染 / 检索命中率门槛。"""

    def test_acc_t4_03_sample_kb_and_questions_structure(self):
        docs = sorted(SAMPLES_KB.glob("*.md"))
        self.assertEqual(len(docs), 5, f"样例文档应为 5 篇，实际 {[p.name for p in docs]}")
        stems: list[str] = []
        headings_by_stem: dict[str, list[str]] = {}
        for p in docs:
            text = p.read_text(encoding="utf-8")
            self.assertTrue(text.strip(), f"{p.name} 内容为空")
            h1, h2 = _doc_headings(text)
            self.assertEqual(len(h1), 1, f"{p.name} 应恰好 1 个一级标题，实际 {len(h1)}")
            self.assertGreaterEqual(len(h2), 3, f"{p.name} 二级标题不足 3 个")
            stems.append(p.stem)
            headings_by_stem[p.stem] = h1 + h2

        questions = json.loads(QUESTIONS_SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(len(questions), 10, f"样例题库应 10 题，实际 {len(questions)}")
        self.assertEqual([q["id"] for q in questions], [f"q{i:02d}" for i in range(1, 11)])

        for q in questions:
            for field in ("question", "expected_keywords", "expected_files", "expected_sections"):
                self.assertTrue(q.get(field), f"{q['id']} 缺字段 {field}")
            self.assertTrue(3 <= len(q["expected_keywords"]) <= 5, f"{q['id']} 关键词数需 3~5")
            for ef in q["expected_files"]:
                self.assertTrue(any(ef in s for s in stems), f"{q['id']} expected_files 无法匹配样例文件名: {ef}")
            # expected_sections 必须是期望文档（expected_files[0] 对应文件）的标题子串
            target = next(s for s in stems if q["expected_files"][0] in s)
            for es in q["expected_sections"]:
                self.assertTrue(
                    any(es in h for h in headings_by_stem[target]),
                    f"{q['id']} 章节 {es!r} 不是 {target} 的标题子串",
                )

        # 改写/意译题：问题文本不得出现期望文档标题的连续 4 字片段（不泄漏标题词）
        for q in questions:
            if q["id"] in PARAPHRASE_IDS:
                title = next(s for s in stems if q["expected_files"][0] in s)
                grams = {title[i: i + 4] for i in range(len(title) - 3)}
                leaked = [g for g in grams if g in q["question"]]
                self.assertFalse(leaked, f"{q['id']} 问题泄漏标题词片段: {leaked}")
        print("✓ ACC-T4-03 样例库/题库结构校验（5 篇文档 + 10 题字段完整且绑定正确）")

    def test_acc_t4_02_kb_override_only_builds_in_memory(self):
        import benchmark.run_benchmark as rb
        from agents.agent import Agent

        queries_seen: list[str] = []
        calls = {"build": [], "load": 0}

        class FakeHit:
            def __init__(self) -> None:
                self.file = "占位.md"
                self.chunk = SimpleNamespace(heading="占位 > 章节")

        class FakeRetriever:
            def retrieve(self, query: str, top_k=None):  # noqa: ANN001 - 测试桩
                queries_seen.append(query)
                return [FakeHit()]

        orig_build, orig_load = Agent.build_index, Agent.load_index

        def fake_build(self, chunks=None, persist=True, augment=None):  # noqa: ANN001
            calls["build"].append(persist)
            self.retriever = FakeRetriever()
            self._index_loaded = True
            return self

        def fake_load(self):  # noqa: ANN001
            calls["load"] += 1
            self.retriever = FakeRetriever()
            self._index_loaded = True
            return self

        def run_main(argv: list[str]) -> tuple[list[bool], int]:
            calls["build"].clear()
            calls["load"] = 0
            queries_seen.clear()
            old_argv = sys.argv
            sys.argv = argv
            try:
                rb.main()
            finally:
                sys.argv = old_argv
            return list(calls["build"]), calls["load"]

        Agent.build_index, Agent.load_index = fake_build, fake_load
        try:
            # 带 --kb：只调用 build_index(persist=False) 内存构建，绝不 load_index
            build_calls, load_calls = run_main(
                ["run_benchmark.py", "--retrieval-only", "--kb", str(SAMPLES_KB),
                 "--questions", str(QUESTIONS_SAMPLE)]
            )
            self.assertEqual(build_calls, [False], f"--kb 应只以 persist=False 构建，实际 {build_calls}")
            self.assertEqual(load_calls, 0, "--kb 路径不得调用 load_index（读盘/可能重建写盘）")
            sample_questions = json.loads(QUESTIONS_SAMPLE.read_text(encoding="utf-8"))
            self.assertEqual(
                queries_seen, [q["question"] for q in sample_questions],
                "--kb 运行应对样例题库逐题检索",
            )

            # 对照：不带 --kb 保持原行为（load_index 加载既有索引，不构建）
            build_calls, load_calls = run_main(["run_benchmark.py", "--retrieval-only"])
            self.assertEqual(build_calls, [])
            self.assertEqual(load_calls, 1, f"默认路径应 load_index 且不构建，实际 build={build_calls} load={load_calls}")
            self.assertGreater(len(queries_seen), 0)
        finally:
            Agent.build_index, Agent.load_index = orig_build, orig_load
        print("✓ ACC-T4-02 --kb 只内存构建（persist=False、不 load_index），默认路径行为不变")

    def test_acc_t4_01_sample_kb_retrieval_without_touching_data(self):
        import benchmark.run_benchmark as rb
        from agents.config import load_config

        before = _index_snapshot()
        questions = json.loads(QUESTIONS_SAMPLE.read_text(encoding="utf-8"))
        config = load_config()
        agent = rb.build_benchmark_agent(config, kb=str(SAMPLES_KB))
        # kb_path 已被本次运行覆盖为样例库（而非 runtime.json 里的私人库）
        self.assertEqual(config.kb_path, str(SAMPLES_KB.resolve()))

        rows = rb.run_retrieval_only(questions, config, agent=agent)
        self.assertEqual(len(rows), len(questions))
        avg_file = sum(r[2] for r in rows) / len(rows)
        avg_sec = sum(r[3] for r in rows) / len(rows)
        print(f"  样例库检索命中：文件 {avg_file:.0%} / 章节 {avg_sec:.0%}")

        self.assertGreaterEqual(avg_file, 0.8, f"平均文件命中 {avg_file:.0%} < 80%（ACC-T4-01 门槛）")
        self.assertEqual(_index_snapshot(), before, "运行后 data/ 用户索引文件被写入或改动")
        print("✓ ACC-T4-01 样例库检索全绿（文件命中 ≥80%）且 data/ 零污染")


if __name__ == "__main__":
    unittest.main()
