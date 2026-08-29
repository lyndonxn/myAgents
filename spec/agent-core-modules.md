# Spec · Agent 五大核心模块补全

需求来源：用户核对清单（规划/记忆/工具/状态/输出容错 + 实现要点）。分支：`feature/agent-core-modules`，起点 9ee2763。

已确认决策：
- S5 任务暂停/恢复仅交付后端 API（Web UI 延期）。
- 新智能能力默认开启，`config.yaml` 提供关闭开关。

---

## S1 工具层强化（清单 3/9/10）

Owned paths: `src/agents/tools.py`、`src/agents/executor.py`、`src/agents/llm.py`、`src/agents/config.py`、`config.yaml`、`tests/test_tool_hardening.py`

设计：
- `tools.py`：新增 `ToolValidationError(ValueError)` 与 `validate_tool_input(tool, kwargs) -> dict`。按工具的 JSON Schema：required 缺失 → 报错；未知键 → 剔除并 LOG；字符串数字纠偏为 schema 类型（如 `"5"`→5）；整数按 min/max clamp。执行器在调用前校验；失败步骤 `ok=False`、`error="参数校验失败: ..."`，**不重试**。
- `executor.py`：步骤级重试 `tools.max_retries`（默认 1，即首次失败后再试 1 次）；`ToolValidationError` 不重试；`StepResult` 增加 `attempts: int = 1`、`degraded: bool = False`。
- KB→Web 降级：`search_knowledge_base` 重试后仍失败，且 web_search 工具在注册表中可用、`tools.kb_fallback_web=true`（默认 true）时，executor 用同一 query 调 web_search，结果 `degraded=True`，text 加 `[降级] 知识库检索失败，已改用 Web 搜索` 前缀，sources 正常收集。
- `llm.py`：`chat_json` 解析失败时执行修复轮——把原 messages + 助手坏输出 + 用户修复指令（"你的输出无法解析为 JSON：{err}，请只输出修正后的合法 JSON"）再调用一次；`llm.json_repair_rounds`（默认 1）轮后仍失败 → 抛 LLMError。config 属性：`llm_json_repair_rounds`、`tools_max_retries`、`kb_fallback_web`。

验收（Given/When/Then）：
- **ACC-S1-01** Given 工具 schema `required:["query"]`，When 调用缺 query，Then 步骤 error 含"参数校验失败"，进程不崩溃，且 attempts==1（无重试）。
- **ACC-S1-02** Given 工具首次抛 RuntimeError、第二次成功，When max_retries=1，Then 结果成功且 attempts==2。
- **ACC-S1-03** Given KB 工具必抛错、web_search 工具可用，When 执行 search_knowledge_base 步骤，Then 返回 web 结果、degraded==True、text 含"[降级]"。
- **ACC-S1-04** Given fake LLM 第一次返回 "not json"、第二次返回合法 JSON，When chat_json，Then 解析成功且共 2 次模型调用；两次都坏 → 抛 LLMError。

## S2 ReAct 迭代 + 反思重规划 + 步骤数据传递（清单 1/2）

Owned paths: `src/agents/planner.py`、`src/agents/executor.py`、`src/agents/agent.py`、`src/agents/config.py`、`config.yaml`、`tests/test_react_loop.py`

设计：
- 步骤传递：input 字符串值支持占位符 `@step:N`（上游输出全文 text）/ `@step:N.field`（上游输出 dict 的 field 字段）。执行器在调用工具前解析；上游步骤缺失/失败 → 该值置空串并在结果里记录 warning。PLANNER_SYSTEM 增补语法说明。
- 反思重规划：`Plan` 增加 `reflections: list[str]`、`rounds: int = 1`。`agent.ask` 首轮执行后，若（任一步失败 或 `planner.reflect`）且反思轮数未超 `planner.max_reflections`（默认 1）且总步数 < `planner.max_steps*2`：调用 `Planner.reflect(question, tool_descriptions, history_text, trajectory)`，输入含每步 ok/error/输出摘要（各截断 ~300 字符），输出 JSON `{need_more, reasoning, steps:[...]}`；need_more=true 时解析新步骤（复用 `_parse` 校验）追加执行。反思 JSON 失败 → 静默跳过（chat_json 已带 S1 修复）。合成在全部步骤后执行一次。
- `web_server._answer_payload` 的 plan 字典增加 `"reflections"`（条数）与 `"rounds"`。`Agent.ask` 签名不变。

验收：
- **ACC-S2-01** Given step1 输出 `{"query": "X", "text": "..."}`，When step2 input 含 `"@step:1.query"`，Then 工具收到 `"X"`；上游缺失 → 空串且不崩溃。
- **ACC-S2-02** Given 首轮某步失败、反思 LLM 返回 need_more+1 个合法步骤，When ask，Then 该步骤被执行且 Plan.reflections 非空、rounds==2。
- **ACC-S2-03** Given 反思连续返回步骤使总步数将超 max_steps*2，When 执行，Then 超出部分被截断，不再新增反思轮。
- **ACC-S2-04** Given 反思 LLM 返回坏 JSON 且修复轮也坏，When ask，Then 无异常、无补步、最终答案正常生成。

## S3 引用校验 + 评测增强（清单 7/8）

Owned paths: 新增 `src/agents/citations.py`；`src/agents/agent.py`、`benchmark/run_benchmark.py`、`benchmark/README.md`、`tests/test_citations.py`

设计：
- `citations.py`：`extract_citations(text) -> list[int]`（匹配 `[数字]`）；`validate_citations(text, n_sources) -> CitationReport(valid_count, invalid_count, cleaned_text, invalid_numbers)`——非法 `[n]` 从 cleaned_text 移除。
- `agent.py`：`Answer` 增加 `citations_valid: int`、`citations_invalid: int`；`_synthesize` 生成后按 `len(answer.sources)` 校验，final_answer 用 cleaned_text，invalid>0 时 LOG.warning；删除 `_synthesize` 中未使用的 `source_map` 死代码。
- benchmark：`run_benchmark.py` 每题记录 `citation_hallucination = invalid/(valid+invalid)`（无引用记 0）与 `task_completed`（keyword_hit≥t 且 source_hit≥t，`--complete-threshold` 默认 0.5）；汇总块输出 completion_rate、hallucination_rate、平均/最大延迟、总成本、退化率；results.md 增加汇总段。可选 `--judge`：LLM 对每题答案按证据 grounding 打 0-2 分（默认关，付费，需显式传参；评审 prompt 输出 JSON）。
- `benchmark/README.md`：新指标口径说明 + 题库扩容步骤（并注明私人知识库不得生成题目入库）。

验收：
- **ACC-S3-01** Given 答案含 `[1]`/`[9]`、来源数 3，When 校验，Then cleaned 移除 `[9]` 保留 `[1]`，valid=1 invalid=1。
- **ACC-S3-02** Given 构造的 results 记录，When 计算汇总，Then completion_rate/hallucination_rate 数值正确。
- **ACC-S3-03** When `--retrieval-only` 运行逻辑（不调 LLM），Then 行为与改动前一致，新指标不引入异常。

## S4 长期记忆 + 摘要压缩/遗忘 + 实体记忆（清单 4/6）

Owned paths: 新增 `src/agents/long_memory.py`；`src/agents/memory.py`、`src/agents/agent.py`、`src/agents/web_server.py`（ask 传 session_id）、`src/agents/config.py`、`config.yaml`、`tests/test_long_memory.py`

设计：
- `MemoryEpisode` dataclass：id、session_id、ts、question、answer_summary（截断）、sources、entities、hits（检索命中计数）。
- `LongTermMemory(store_dir, embed_backend, max_episodes=200)`：`add(episode)` 文本向量化追加；`search(query, k, exclude_session_recent=...)` 余弦 top-k 并对命中者 hits+1；`prune()` 超容量时按 (hits, 新近度) 保留利用率高者；持久化 `episodes.json` + `vectors.npy`（tmp 写 + os.replace 原子替换）；`as_context(query, k)` 渲染 "相关历史经验" 文本块。
- embedding：模块函数 `build_memory_backend(config)`——auto 时优先复用 `LocalEmbeddingBackend(config.embedding_model)`（加载失败/未安装则 TF-IDF 哈希后端，dim 取 `embedding.hash_dim`），保证离线可测。
- `MemoryCompressor(llm)`：`compress(old_summary, evicted_turns) -> str`，单次 LLM 调用（system：把旧摘要与若干轮对话合并为 ≤300 字摘要）；任何异常 → 回退"旧摘要 + 各轮首句截断"拼接。
- `EntityMemory`：`extract(llm, question, answer) -> dict[name, fact]`（LLM JSON，异常/坏结构 → {}）；存储 `dict[name, {facts: list, last_seen: ts}]`，`as_prompt(limit=8)` 渲染；随 LongTermMemory 同目录持久化（`entities.json`）。
- `memory.py`：`SessionMemory` 增加 `summary: str = ""`；`maybe_compress(llm)`——超 max_turns 时对滑出轮次调用 compressor 更新 summary；`as_text()` 在历史前加 "此前对话摘要：{summary}"。既有方法签名不变。
- `agent.py` 集成（全部受开关控制，默认开）：`ask(question, verbose=False, session_id="")` 新增可选参；规划前 `long_memory.as_context(q_work)` 注入 planner prompt（`build_planner_prompt` 增加 `longterm_text` 参数）；成功后写 episode；`memory.entities_enabled` 时抽取实体；`memory.maybe_compress(self.llm)`。`web_server` 三个 ask 入口传 `session_id`。
- config：`memory.long_term_enabled`、`memory.entities_enabled`、`memory.max_episodes`、`memory.embedding_backend`；config.yaml 增段。

验收：
- **ACC-S4-01** Given 写入 3 条不同主题 episode，When search 相关查询，Then top1 是语义最近条目（TF-IDF 后端下用共享关键词判定）。
- **ACC-S4-02** Given max_episodes=3、写入 5 条（第 1 条 hits 高），When prune，Then 保留 3 条且被淘汰的是 hits 低且最旧的。
- **ACC-S4-03** Given fake LLM 摘要正常/抛错两种，When maybe_compress，Then 正常时 summary 为 LLM 文本、抛错时 summary 为降级拼接，两种均不抛异常且 as_text 含摘要块。
- **ACC-S4-04** Given 实体抽取 LLM 抛错/返回坏 JSON，When extract，Then 返回 {} 不崩溃。

## S5 任务状态持久化 + 暂停/恢复（清单 5，仅后端）

Owned paths: 新增 `src/agents/task_store.py`、`src/agents/task_runner.py`；`src/agents/web_server.py`；`tests/test_task_runner.py`

设计：
- `TaskRecord` dataclass：task_id、session_id、workspace_id、question、status（queued/running/paused/completed/failed/canceled）、plan(dict)、steps(list[dict])、final_answer、error、usage(prompt/completion tokens, cost)、created_at/updated_at。
- `TaskStore(root_dir)`：`create/get/list/update`；每任务单 JSON 文件 `data/tasks/{id}.json`，写入用 tmp+`os.replace` 原子替换；`list()` 读索引（按 updated_at 倒序）；`recover_running()` 把 status==running/queued 改为 paused（服务启动时调用——崩溃可恢复）。
- `TaskRunner(store, agent_provider, lock)`：`submit(session_id, workspace_id, question) -> task_id` 入队；单工作线程循环取任务；每步前检查暂停/取消事件；步骤完成即持久化；暂停 → status=paused 退出循环；`resume(task_id)` 从持久化 steps 恢复（跳过 ok 步骤）继续；合成阶段同样可暂停；失败 → failed + error。执行时 `Handler.agent.memory = store.memory(session_id)` 快照并持 `Handler.lock`（与 /api/ask 互斥，防记忆串线）。
- `web_server.py` 新增路由：`POST /api/tasks {question, session_id}` → {task_id}；`GET /api/tasks` → 列表；`GET /api/tasks/{id}` → 详情；`POST /api/tasks/{id}/pause|resume|cancel`。现有接口行为不变。启动时调用 `store.recover_running()`。
- 合成/来源复用 Agent 现有 `_synthesize`、`_collect_sources`、`_accumulate_usage`（必要时在 Agent 上加一个仅供任务路径使用的组装方法，不改 ask）。

验收：
- **ACC-S5-01** Given 3 步计划任务，When 执行中查看 store，Then 每完成一步 JSON 里 steps 长度递增且 status=running。
- **ACC-S5-02** Given 运行中任务，When pause 后 resume，Then 最终 completed、final_answer 非空、已完成步骤未重复执行（attempts/输出与暂停前一致）。
- **ACC-S5-03** Given store 中遗留 status=running 的任务文件，When recover_running，Then 变为 paused，resume 后正常完成。
- **ACC-S5-04** When cancel 运行中/排队任务，Then status=canceled 且不再被 resume 复活。

---

## 配置增量（config.yaml，随各切片落地）

```yaml
llm:      json_repair_rounds: 1
tools:    max_retries: 1; kb_fallback_web: true
planner:  reflect: true; max_reflections: 1
memory:   long_term_enabled: true; entities_enabled: true; max_episodes: 200; embedding_backend: auto
```

## 全局约束
见根 `AGENTS.md`（离线测试、禁碰路径、向后兼容、零新增依赖）。测试全部离线（fake LLM / monkeypatch / 临时目录 store）。
