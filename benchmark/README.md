# 评测与对标 Codex

本目录用于量化评估 myAgents 的知识库问答质量，并为后续**对标 Codex** 提供同一套题库与指标口径。

## 指标口径

| 指标 | 含义 |
| --- | --- |
| keyword_hit | 答案文本中命中期望关键词的比例（0~1） |
| source_hit / section_hit | 检索召回是否包含期望**文件** / **章节**的比例（0~1）；章节级比文件级严格得多 |
| citation_hallucination | 引用幻觉率：非法 `[n]` 引用占全部引用标记的比例 invalid/(valid+invalid)（按实际来源数校验，无引用记 0；校验逻辑见 `src/agents/citations.py`） |
| task_completed | 任务完成：keyword_hit 与 source_hit 均达到 `--complete-threshold`（默认 0.5）记 1，否则 0；汇总输出 completion_rate |
| judge | 可选 `--judge`：LLM 按检索证据评审答案支撑度（0=无证据支撑 / 1=部分支撑 / 2=有证据支撑，输出 JSON）；汇总输出 judge_avg（默认关闭，付费） |
| latency_s | 单题端到端耗时（秒） |
| cost_yuan | 按 DeepSeek 定价估算的 LLM 成本（元）。注意：`--judge` 的评审调用开销不计入该成本与总成本 |
| 计划步骤 / 退化 | 规划层是否产生了多步计划，或退化为直接检索（健壮性指标） |

完整评测的 `results.md` 末尾附「## 汇总」段：completion_rate、hallucination_rate、
平均/最大延迟、总成本、退化率（fallback 题数占比），启用 `--judge` 时另有 judge_avg；
`results.json` 顶层 `summary` 字段为同一份数据。

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

## 示例知识库评测（samples/kb，T4）

`samples/kb/` 内置 5 篇**完全原创**的技术短文，配套题库 `benchmark/questions_sample.json`
（10 题，q01–q10，其中 q04/q09/q10 为改写/意译难例——问题文本不出现文档标题词）。
`--kb` 让本次运行用样例库**在内存中重建索引**（`build_index(persist=False)`）：
绝不读写 `data/index*` 与 `data/chunks.json`，用户索引零污染，仅本次运行生效。

```bash
# 免费跑样例库检索命中（不调 LLM、不写 data/）
python -m benchmark.run_benchmark --retrieval-only --kb samples/kb --questions benchmark/questions_sample.json

# 完整评测同样支持 --kb / --questions（调用 LLM，付费）
python -m benchmark.run_benchmark --kb samples/kb --questions benchmark/questions_sample.json

# 也可临时指向任意本地目录（如私有库做一次性体检，同样不写 data/）
python -m benchmark.run_benchmark --retrieval-only --kb /path/to/你的库
```

样例库内容（每篇含 1 个一级标题 + 多个二级标题，二级标题供章节级命中判定）：

| 文档 | 二级章节 |
| --- | --- |
| `RAG 检索增强生成入门.md` | 什么是 RAG / 为什么需要 RAG / RAG 的整体流程 |
| `父子分块与索引策略.md` | 什么是父子分块 / 父块与叶子块如何分工 / 重叠与切分参数 / 元数据与来源追溯 |
| `混合检索与重排序.md` | 单路检索的局限 / 向量检索与 BM25 / RRF 融合 / Cross-Encoder 精排 |
| `Agent 规划与工具调用.md` | 任务拆解 / ReAct 循环 / 反思与重规划 / 工具注册与降级 |
| `MCP 模型上下文协议概览.md` | 解决什么问题 / Host、Client 与 Server 三种参与者 / 能力协商 / 典型工作流程 |

### 如何据此扩题

1. **换/加文档**：替换或新增 `samples/kb/` 下的样例 Markdown（保持原创、标题层级清晰；
   每个二级章节正文建议 ≥80 字，低于 `chunking.min_chars` 的章节会被丢弃、无法命中）。
2. **同步补题**：在 `benchmark/questions_sample.json` 追加条目——`expected_files` 用
   文件名（去 `.md`）子串；`expected_sections` 用二级标题子串；`expected_keywords`
   3~5 个正确答案应含的关键词。
3. **校准**：先跑 `--retrieval-only` 把文件/章节命中调到尽量 100%，再上完整评测校准
   关键词；想要改写难例，就让问题文本避开文档标题词与章节标题（参考 q04/q09/q10）。
4. **验收**：`tests/test_benchmark_sample.py` 会离线复跑样例库检索（文件命中 ≥80%）、
   校验 `--kb` 不落盘、并核对题库与样例文档的结构一致性。

### 隐私红线

私人知识库（个人笔记、公司内部文档）**不得**放入 `samples/kb/` 或据此出题提交入库——
文件名、章节标题、关键词都会间接泄漏私人内容的结构与措辞。样例库只收可公开的原创短文；
私有库评测请用上面的 `--kb /path/to/私有库` 临时运行（索引只在内存，不落盘），题目文件不要提交。

## 题库结构（questions.json）

每题包含：
- `expected_keywords`：判断答案质量（关键词子串）
- `expected_files`：判断文件级检索命中（文件名子串）
- `expected_sections`：判断章节级检索命中（标题路径子串，最严格）

题库含 5 道**改写/意译难例**（h01-h05，问题不直接匹配文件标题），用于暴露检索在
同义改写、指代、噪声场景下的短板。

## 扩容题库的步骤

题库默认对准 `knowledge_base/` 中的**可公开示例知识库**。扩容时按以下步骤操作：

1. **准备语料**：把可公开的示例 Markdown 放入 `knowledge_base/`（例如自写的主题笔记样例），
   重建索引后先跑 `python -m benchmark.run_benchmark --retrieval-only` 确认新内容可召回。
2. **编写条目**：在 `questions.json` 追加
   `{"id", "question", "expected_keywords", "expected_files", "expected_sections"}`：
   - `expected_keywords`：正确答案中应出现的关键词（子串匹配、不区分大小写），判定答案质量；
   - `expected_files`：应被召回的文件名子串，判定文件级检索命中；
   - `expected_sections`：应命中的标题路径子串，判定章节级命中（最严格）。
3. **校准建议**：先跑 `--retrieval-only` 校准 `expected_files` / `expected_sections`
   （检索层面应尽量 100%）；再跑完整评测校准 `expected_keywords`——若答案正确但措辞不同
   导致 keyword 偏低，应放宽关键词而不是直接下"质量差"的结论。一题 3~5 个关键词、
   1~2 个期望来源为宜，避免判定过严。
4. **隐私红线**：私人知识库（如个人笔记库）**不得**据此生成题目提交入库——题目中的
   文件名、章节标题、关键词都会间接泄漏私人笔记的标题与结构；示例题目只能基于
   可公开的样例知识库编写。

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
