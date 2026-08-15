# 评测与对标 Codex

本目录用于量化评估 myAgents 的知识库问答质量，并为后续**对标 Codex** 提供同一套题库与指标口径。

## 指标口径

| 指标 | 含义 |
| --- | --- |
| keyword_hit | 答案文本中命中期望关键词的比例（0~1） |
| source_hit / section_hit | 检索召回是否包含期望**文件** / **章节**的比例（0~1）；章节级比文件级严格得多 |
| latency_s | 单题端到端耗时（秒） |
| cost_yuan | 按 DeepSeek 定价估算的 LLM 成本（元） |
| 计划步骤 / 退化 | 规划层是否产生了多步计划，或退化为直接检索（健壮性指标） |

## 使用

```bash
# 完整评测（调用 LLM，消耗 API 额度）
python -m benchmark.run_benchmark

# 免费快速验证召回质量（不调 LLM，仅检索命中检查）
python -m benchmark.run_benchmark --retrieval-only

# 检索精度控制：top_k、精排模式、多查询扩展
python -m benchmark.run_benchmark --retrieval-only --top-k 1
python -m benchmark.run_benchmark --retrieval-only --rerank off|auto|llm
python -m benchmark.run_benchmark --retrieval-only --multi-query
```

输出 `results.json`（完整明细）与 `results.md`（汇总表）。

## 题库结构（questions.json）

每题包含：
- `expected_keywords`：判断答案质量（关键词子串）
- `expected_files`：判断文件级检索命中（文件名子串）
- `expected_sections`：判断章节级检索命中（标题路径子串，最严格）

题库含 5 道**改写/意译难例**（h01-h05，问题不直接匹配文件标题），用于暴露检索在
同义改写、指代、噪声场景下的短板。

## 对标 Codex 的方法

1. **同一题库**：`questions.json` 为固定题库。在 Codex 中逐题运行：
   ```bash
   for q in $(jq -r '.[].question' questions.json); do codex exec "$q" --output-format json; done
   ```
   （也可将知识库目录作为 Codex 的工作区，让其自行读取笔记文件。）
2. **统一指标**：对 Codex 的回答套用相同的 `keyword_hit` 判定，来源命中改为
   检查回答中是否提及对应文件/主题（Codex 无显式检索步骤，该项可记为"回答是否覆盖期望来源内容"）。
3. **对比表**：把 Codex 的结果并入 `results.md` 同一张表，新增"引擎"列即可直接对比
   检索质量、延迟与成本。

## 注意

- 完整评测消耗真实 API 额度（每轮 15 题 × 3~4 次 LLM 调用，约 ¥0.04）；`--retrieval-only` 免费。
- 题库可按需增删；新增题目时补充 `expected_keywords`（答案质量）、
  `expected_files`（文件级命中）与 `expected_sections`（章节级命中，标题路径子串）。
- 若题目期望本身存疑（如回答正确但措辞不同），先校准题库再下结论。
