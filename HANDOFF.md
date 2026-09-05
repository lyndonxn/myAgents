# HANDOFF — myAgents Agent 化改造（全部完成 ✅）

分支：`feature/agent-core-modules`（起点 9ee2763）。推送状态：**未 push**（未获授权）。
规格：`spec/agent-core-modules.md`（S 阶段 + T 阶段）。契约：`AGENTS.md`。

## 第一期 S1–S5（五大核心模块补全，已验收）

| 切片 | 内容 | 提交 |
| --- | --- | --- |
| S1 | 工具层强化：schema 校验/重试/KB→Web 降级/JSON 修复轮 | 56ad6bf |
| S2 | ReAct 迭代：反思重规划/@step 占位符传递/预算护栏 | c5c6519 |
| S3 | 引用校验 + 评测指标（幻觉率/完成率/--judge） | 40438d5 |
| S4 | 记忆体系：长期记忆向量库/摘要压缩/遗忘/实体记忆 | cc24700 |
| S5 | 任务状态机：逐步落盘/暂停恢复取消/崩溃恢复 API | 320498e |

## 第二期 T1–T5（遗留事项处理，已验收）

| 切片 | 内容 | 验收 | 提交 |
| --- | --- | --- | --- |
| T1 | 任务答案写回会话（on_complete 回调）+ payload 透出 citations_valid/invalid | ACC-T1-01..03 | 66d6b0f |
| T2 | 任务面板 Web UI（右侧 trace 面板 04 区块：列表/状态徽章/暂停继续取消/步骤详情/发现轮询）+ 引用校验行 + 「后台任务」徽标；浏览器实测通过 | ACC-T2-01..04 | 97d43e2 |
| T3 | CLI 任务模式 `python -m scripts.task`（--question/--list/--watch/--resume/--cancel/--task-store） | ACC-T3-01..03 | b0865e5 |
| T4 | 示例评测库：samples/kb 5 篇原创文档 + questions_sample.json 10 题 + --kb/--questions 内存索引（零落盘） | ACC-T4-01..03 | 3df2e36 |
| T5 | 完整 LLM 评测实跑 + 用量计量修正（按题增量） | 实测 | 39565f9 |

### T 阶段实测发现与修复（浏览器/实跑证据）
1. **任务面板发现死锁**：页面加载时无任务则轮询永不启动，后创建的任务无法被发现 → 改为常驻发现轮询（活跃 2s / 空闲 8s，页面隐藏即停）。97d43e2
2. **任务写回消息缺引用计数**：TaskRecord 未存 citations → TaskRecord 增字段、finish_task 返回 report、写回 metrics 带上，聊天流引用校验行打通。97d43e2
3. **评测总成本重复累计**：Agent.ask 返回会话累计用量，汇总按行求和被放大 ~7 倍 → 按题差值记录。39565f9

### 完整评测结果（benchmark/results.md，gitignored 仅本地）
15 题：关键词命中 100%、来源命中 100%、任务完成率 100%、平均延迟 13.2s、总成本 ¥0.0554、退化率 0%；引用幻觉率 13.0%——全部来自 q10（主题概览题）：`list_knowledge_topics` 工具无来源输出时 LLM 仍编造 [n] 编号，被引用校验如实剔除并计入幻觉率（该指标的教科书案例；若要消除，可给主题工具补来源或让规划器对该类问题跳过引用指令）。

## 验证状态（最终全量回归，全绿）
py_compile 全仓 ✓；八个测试套件（smoke/web_store/tool_hardening/react_loop/citations/long_memory/task_runner/task_cli + benchmark_sample）全部通过。

## 剩余延期项（均已明确，无阻塞）
- 无。原延期项全部落地。可选后续：主题工具来源化（消除 q10 类幻觉）、任务面板创建入口、示例库扩题。

## 风险与注意
- 用户工作区未提交变更（`dist/` 删除、`docs/`、`.zcode/` 未跟踪）保持原样，未纳入任何提交。
- `data/runtime.json` 含真实 API Key（gitignored；建议轮换）。
- results.json/results.md 含私人知识库路径，已由 gitignore 排除，不得提交。

## 下一步动作
如需合入：feature/agent-core-modules → main（合并/PR 需用户授权 push）。

---

# 2026-09 升级（已完成，G1–G11 全部交付）

分支：`feature/upgrade-2026-09`（起点 main @ 9e6791f）。规格：`spec/upgrade-2026-09.md`（11 目标 / 4 批，G1–G11）。控制器 = 根会话；分支会话用子代理承载，均已完成并整合。

## Gate 0 能力清单（一次性，全分支复用）
darwin 25.6.0 arm64；原生 Read/Glob/Grep/Bash/Edit 可用（Windows 脚本不适用）；python=.venv 3.13；本升级无外部连接器需求；付费 LLM benchmark 禁跑（除 ACC-U9-03 待单独授权）。

## 已完成
| 切片 | 内容 | 验收 | 提交 |
| --- | --- | --- | --- |
| G1 | 测试入口标准化（P0-1）：10 个 plain-script 套件 → unittest 可发现，76 用例；直跑兼容保留；统一入口写入 AGENTS.md | 独立验收 ACCEPTED（ACC-P0-1-a..f 全过） | bcb8121(spec) + cfee0d7(tests+AGENTS) |
| G2 | 配置校验（P0-4）：validate_config/ConfigError/normalize_config 落地规则表；load_config 失败抛错不静默启动；POST /api/config 无效值 400（errors 列表，不回显 Key）；归一化布尔字符串与枚举 | 独立验收 ACCEPTED（ACC-P0-4-a..f 全过，含用户 runtime.json 逐字节不变取证） | 85957d5（含 spec 规则表） |
| G3 | 数据外发默认关闭 + 回答状态披露（P0-3 + ACC-U3-01..03）：三开关（kb_fallback_web/long_term_enabled/entities_enabled）默认 false；`ask(remember=, allow_web=)` 请求级许可（None→按配置，False 强禁，True 强许）；runtime.json 显式值优先 + 迁移提示一次/进程；`metrics.degraded/web_used` 透出 /api/ask、任务写回与任务详情；webui 答案卡片状态行 + 设置页外发说明 + 清空会话确认 | 控制器验证全过（15 用例 G3 套件 + 全量 103/103） | 2e9ca34 |
| G4 | 记忆治理（P0-2）：新增长期记忆治理方法（list_episodes 过滤/搜索/截断、delete_episode 单条删除含向量行对齐、delete_by_session 按会话删除、clear 清空）与 EntityMemory.clear；Web API 五端点（GET /api/memory?session_id=&q=&limit=、GET /api/memory/entities、DELETE /api/memory/{id}、DELETE /api/memory?session_id=、POST /api/memory/clear）+ 新增 do_DELETE；/api/reset 联动删除该会话长期记忆（long_term_removed 透出，未开启幂等跳过）；webui 新增「记忆」设置页（搜索/逐条删除/清空全部/实体事实/未开启提示） | 控制器验证全过（9 用例 G4 套件 + 全量 112/112） | 见本次提交 |

验证证据：`unittest discover` 76/76 OK（改造前 0）；直跑 10/10 退出码 0；失败路径非 0（/tmp 验证）；离线（fake LLM/桩，data/ 用户库零写入）；六类覆盖映射完整。

## 观察项（预存行为，非本切片缺陷，待后续切片决定）
1. `tests/test_smoke.py::test_load_chunks` 在 cfg.kb_path 指向真实知识库时会只读访问（有存在性守卫、旧行为原样保留）；如需完全隔离可改 fixture 库。
2. `tests/test_benchmark_sample.py` 依赖本机已缓存的 bge-small-zh-v1.5；冷缓存环境可能触发下载。
3. （G2 验收发现）`/api/config` 保存路径 `llm.api_key` 沿用既有 `str()` 转换，int 型 Key 被转字符串落盘而 `vision.api_key` 会 400——统一口径放到 G5 密钥安全切片。
4. （G2 验收发现）CONFIG_FIELDS 白名单外键在保存路径被静默丢弃，属既有行为。

## 延期决策
- G1 覆盖率指标（≥80%）延期：需 coverage.py，超出轻依赖授权，待用户授权。
- G2 沿用 `{"error", "errors"}` 响应体：P1-5 统一错误码契约挂账后置。

## 收官总览（2026-09-04）

**2026-09 升级收官：G1–G11 全部完成并提交（G11 原延期项经用户 2026-09-05「继续直至全部结束」指令恢复实施）。** 分支 `feature/upgrade-2026-09`（起点 main @ 9e6791f），**未 push、未合入 main（均需用户授权）**。

| 切片 | 内容 | 提交 |
| --- | --- | --- |
| G1 | 测试入口标准化（unittest 76→156 用例） | cfee0d7 |
| G2 | 配置校验（validate_config/400/布尔归一化） | 85957d5 |
| G3 | 数据外发默认关闭 + 回答状态披露（P0-3） | 2e9ca34 |
| G4 | 记忆治理（查看/删除/清空 API + 设置页 + 会话联动） | f46f112 |
| G5 | 密钥安全（权限检查/0600/拒绝保存）+ 任务写回幂等 | 7eb0ab1 |
| G6 | 审计轨迹（按天 JSONL，四层事件，不记正文与 Key） | fb62084 |
| G7 | 任务看门狗（心跳/步骤超时/总超时 → paused 可恢复） | 1b3b352 |
| G8 | 难评测集（100 题×10 类）+ reward 模块 | 19ded63 |
| G9 | 速度成本包（快路径/max_tokens/重排/证据压缩） | c2cb0a1 |
| G10 | 检索迭代循环（改写再检索 + max_search_calls 预算） | ffc7169 |
| G11 | 本地 LLM 离线档位（空 Key 本地端点 / 非 DeepSeek JSON 约束 / allow_web 单点钳制 / prepare_offline 体检） | 882ce00 |
| G3 补遗 | 三开关接入设置面板（CONFIG_FIELDS 白名单 + _config_view 透出 + 检索页/记忆页三下拉） | 4c6e906 |

最终验证：`unittest discover` **179/179 OK**；直跑 20/20 退出码 0；compileall（src+tests+benchmark+scripts）通过；全程离线。

### 延期挂账（均为显式决策，无隐藏风险）
1. **ACC-U9-03 / ACC-U10-03**：付费 100 题实跑对比（延迟/token/reward 前后对比），需用户单独授权后运行 `benchmark/run_benchmark.py --questions benchmark/questions_hard.json`。
2. **P1-2 剩余项**：锁拆分（会话记忆/执行/写回三把锁）与可配置多 worker（单 worker 串行已保证正确性）。
3. **G1 覆盖率指标 ≥80%**：需 coverage.py，超出轻依赖授权。
4. **P1-5 统一错误码契约**、P1-4 证据级引用校验、P1-1 模块解耦：P1 挂账后置。

### 收尾状态
- README 已同步（核心能力/当前边界/升级章节）。
- 用户工作区未提交的私人文件（benchmark 结果、设计文档等）保持未跟踪，未纳入任何提交。
- `data/runtime.json` 含真实 API Key（gitignored；**建议轮换**）。
- G3 默认行为变更：联网降级/长期记忆/实体记忆默认关闭——存量 runtime.json 未显式设置时启动会提示一次。

### 如需重启工作
1. 合入：`feature/upgrade-2026-09` → main（需用户授权 push）。
2. 付费对比评测：授权后先跑改造前基线（main 分支）再跑本分支，对比 p50/p95、token 与 reward。
3. 离线档位启用：设置面板把 llm.mode 改为 local（或 config.yaml），base_url 指向本地 OpenAI 兼容端点（如 Ollama http://localhost:11434/v1），运行 `scripts/prepare_offline.py --check` 体检。

---

## W1 切片记录（2026-09-05，用户驱动前端改版：答案卡对齐设计原型）

- **背景**：用户提供设计原型 demo（桌面 redesign-mockup-final.html，含「重播生成过程」演示），确认改造 webui 答案卡；用户约束——**颜色一律沿用原页面既有 tokens（不引入 demo 黑白配色）**、重播按钮与自动播放仅演示不入生产、三栏布局与其余功能不动。已先在浏览器实跑 demo 学习其时间轴（检索态→生成态→完成态、引用跳转、详情抽屉），并核对其缺陷（检索期 opacity 占位留白、`.evidence-item.no` 选择器失效、生成态残留工具 chip、#fef9c3 高亮无暗色适配）——均未带入实现。
- **验证证据**：`unittest discover` 179/179 OK（test_egress_defaults/test_memory_governance 对 webui.html 的静态断言原样通过）；node --check（抽取 &lt;script&gt;）通过；全程离线——假 NDJSON fetch 桩模拟 stage/meta/delta/done 完整流，浏览器实测明/暗双主题下的检索态、打字机态（状态框+正文+光标）、完成态、引用 [n] 跳转高亮、详情弹窗与关闭。
- **实现（仅 scripts/webui.html，后端零改动）**：
  - 答案卡 5 层：L1 两态状态条（「正在规划与检索」→「正在生成答案 · 引用 N 段内容」+打字机正文与光标 → 完成徽章：模式徽章/首个工具 chip/首个来源/命中 N 段/耗时/成本/详情按钮）；L2 证据折叠（默认收起、按 messageId 记忆开合、生成中隐藏）；L3 操作行（复制/有用/没用+时间戳，生成中隐藏防误点半成品）；L5 回答详情弹窗（字段全部来自 plan/metrics 真实数据：模式/是否联网/检索路径/规划轮次/工具步骤/引用校验/命中/耗时/Token/成本/消息 ID；Esc+遮罩关闭）。
  - 队列式流渲染：delta 进缓冲、30ms/4 字符打字机节奏，仅增量替换当前流式卡片（data-stream 锚点），替代原先每 delta 全量 renderMessages；用户上滚不强制跟随；中断/出错停表清态（AbortError 语义不变）。
  - 事件消费增强：stage 事件驱动检索文案；meta 到达即切生成态（此时 sources/metrics 已知，与后端 NDJSON 顺序 stage→meta→delta→done 对齐）；done 错误路径保持原语义。
  - [n] 引用 chip 化：_mdHtml 将 [数字] 转为可点击 .cite（沿用既有蓝色上标样式+hover 底色），点击展开证据折叠并滚动高亮对应来源行（--blue-soft 闪烁，双主题自适应）。
  - 移除旧「RETRIEVING 独立占位气泡」与 thinking-state 用法；测试断言的 webState/cite-line 字符串与设置页文案原样保留。
- **兼容性**：/api 契约不变；用户气泡、INTRO、快速操作、右侧 trace 面板、会话/工作区/记忆/日志/设置未动。demo 中无真实数据源的字段未采纳（缓存命中、快慢路径、追问推荐、面包屑+引句来源卡）——后续可选项：后端 sources_detail 结构化来源、Agent.ask 可选 on_stage 回调实现真「检索→生成」两段事件。
- **涉及文件**：scripts/webui.html（答案卡 CSS（仅既有变量）、回答详情 modal、liveAnswerHTML/onSend 重写、_mdHtml 引用 chip）。

---

## G11 切片记录（2026-09-05，控制器续接；实现子代理被取消后遗留的完整工作区改动经控制器核验 + 独立验收子代理确认）

- **验证证据**：`unittest discover` 173/173 OK（G10 后 156 → 新增 test_offline_profile 17 用例）；直跑 20/20 退出码 0；compileall（src+tests+benchmark+scripts）通过；全程离线（ScriptedLLM + TF-IDF 后端 + tempfile，用户 data/runtime.json 与 .env mtime 前后不变）。
- **实现**：
  - `llm.mode: cloud | local`（默认 cloud 零行为变化）：枚举校验规则 + CONFIG_FIELDS 白名单 + 设置页「运行模式」下拉与离线说明；
  - ACC-U11-01：LLMClient local 档允许空 API Key（请求头省略 Authorization），cloud 档保持原报错；local 有 Key 照常带头；
  - 非 DeepSeek 模型 JSON 约束：chat_json 向 messages 副本末尾追加 `_JSON_ONLY_SYSTEM_HINT`（调用方列表不被修改），修复轮机制不变，deepseek 仍走 response_format；
  - ACC-U11-02：`resolve_allow_web` 单点钳制——local 档 allow_web 一律 False（ask / plan_only / task_runner 三路径共用该单点），钳制提示一次/进程；KB 未命中走既有披露路径、零网络调用；
  - ACC-U11-03：离线 E2E（ScriptedLLM + TF-IDF + 临时 KB）全链路绿、引用/来源正常、零 requests 调用；
  - `scripts/prepare_offline.py`：--check 只读体检（llm.mode/base_url/web 开关/embedding+reranker 缓存可离线加载）给中文修复建议；--prepare 仅打印指引，不做真实下载。
- **独立验收**：ACCEPTED（ACC-U11-a..f 全过，Spec/Standards 双 PASS，含 data/runtime.json 与 .env mtime 前后一致取证）。
- **涉及文件**：src/agents/{llm,agent,config,web_server}.py、config.yaml、scripts/{webui.html,prepare_offline.py}、tests/test_offline_profile.py（新增 17 用例）。

---

## G10 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 156/156 OK（G9 后 152 → 新增 test_search_loop 4 用例）；直跑 18/18 退出码 0；compileall 通过；全程离线（脚本化 LLM + 查询感知假 KB 工具）。
- **实现**（复用 S2 反思机制与 @step:N 占位符，未新增执行路径）：
  - `build_reflect_prompt/reflect` 新增 `search_budget=(已用, 上限)` 注入：有剩余 → 允许改写 query 再检索（要求 query 与已执行的明显不同；KB 本身无内容则不提检索补步）；耗尽 → 明确禁止任何检索补步。
  - `_reflect_and_extend`：记账 `searches_used`（含反思补步追加后的搜索）并传预算；硬约束——反思提出的新检索步骤超出 `planner.max_search_calls` 时被剔除（其余类型补步不受影响），剔除计数落日志。
  - 配置：`planner.max_search_calls` 默认 3（校验 1–10），config.yaml 注释说明。
- **ACC-U10-01** ✓：首轮未命中 → 反思改写 query 二轮命中，总搜索 2 ≤ 3，来源来自二轮，反思 prompt 含预算。
- **ACC-U10-02** ✓：预算耗尽 → 反思 prompt 明确禁检 + 硬约束剔除第 4 次检索，合成提示按未命中路径披露。
- **ACC-U10-03**（难评测集 reward 对比）需付费 LLM 实跑，按规挂起待单独授权；机制已就绪，可在授权后用 `--questions benchmark/questions_hard.json` 实跑对比。
- **涉及文件**：src/agents/{planner,agent,config}.py、config.yaml、tests/test_search_loop.py（新增 4 用例）。

---

## G9 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 152/152 OK（G8 后 143 → 新增 test_speed_cost 9 用例）；直跑 17/17 退出码 0；compileall 通过；全程离线。
- **快路径（ACC-U9-01/02 ✓）**：`_is_simple_question` 启发式门控（短 ≤60 字符、单问句、无多跳线索表）+ 无会话历史 → 跳过规划与反思，直接单步检索→合成（0 次规划调用）；多跳/对比题与追问场景不受影响；`planner.fast_path` 配置开关。简单题 LLM 调用从 3 降为 1。
- **合成优化**：`synthesis.max_tokens`（默认 1024，原用全局 llm.max_tokens）传至合成调用；`_synthesize` prompt 重排——稳定前缀（指令+证据上下文）在前、规划摘要/对话历史/问题在后（DeepSeek 前缀缓存友好）。
- **句子级证据压缩**：`compress_evidence(text, query)`（jieba 提取查询词，按句相关性保留，目标 ≤70% 长度；过短/无相关句/全相关守卫原样返回）；`synthesis.evidence_compression` 开关默认开。实测构造样本 396→140 字符（降 65%）。ACC-U9-03（100 题实跑对比）按规需单独授权，未执行。
- **配置**：config.yaml 新增 `planner.{fast_path,fast_path_max_len}` 与 `synthesis.{max_tokens,evidence_compression}` + 校验规则 + 设置白名单（synthesis 节）。
- **既有测试适配**：考察完整规划路径的测试（egress/long_memory/react_loop/citations/audit）统一注入 `planner.fast_path=False`（深合并保留各用例覆盖）。
- **涉及文件**：src/agents/{agent,config}.py、config.yaml、src/agents/web_server.py（白名单）、tests/{test_speed_cost(新),test_egress_defaults,test_long_memory,test_react_loop,test_citations,test_audit}.py。

---

## G8 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 143/143 OK（G7 后 135 → 新增 test_reward 8 用例）；直跑 17/17 退出码 0；compileall（src+benchmark）通过；全程零 LLM（reward 纯离线判分、检索跑 samples/kb 内存索引）。
- **reward 模块**（新增 `src/agents/reward.py`，benchmark 与离线档位共用）：
  - `score_case(question, answer, sources, expectations) -> RewardResult(score, failure_type, detail)`；十类题型口径：fact/paraphrase/multi_hop/tool_choice/long_context/memory（关键词+来源比例）、refusal/offline（`refusal` 期望：确定性拒答措辞得分、编造带来源答案记 `fabricated` 扣 0 分——ACC-U8-02）、injection（出现 `forbidden_keywords` 即 `injection_followed`）、conflict（关键词命中但非权威来源 → `stale_source` 0.3）。
  - `percentile(values, p)`（线性插值 p50/p95）；`summarize_by_category(results)`（分题型 count/avg_score/pass_rate/p50/p95/失败类型分布 + 总 `refusal_accuracy`）；`refusal_like`。
- **难评测题库**（新增 `benchmark/questions_hard.json`，100 题 = 10 类 × 10 题）：fact/paraphrase/multi_hop/tool_choice/long_context 五类检索可验证（指向 samples/kb 文档与章节）；refusal/conflict/injection/memory/offline 五类行为题（reward 字段：refusal/forbidden_keywords 等）。
- **run_benchmark.py 接入**：`run_retrieval_only` 每题计检索耗时，报告 p50/p95 延迟与分题型命中汇总（reward 检索口径 score=(src+sec)/2）；rows 行尾新增 latency（6 元组，test_citations S3-03 已同步）；全量 run 的 summarize_results 新增 p50/p95/by_category，每题记录 category 与 reward 判分。
- **ACC-U8-01** ✓：--retrieval-only 跑 questions_hard.json（samples/kb，top_k=6）——检索类 5 类 50 题全绿（pass 100%），平均文件命中 99.5%，p50=11ms/p95=32ms，分题型汇总齐备。**ACC-U8-03** ✓：全程无 LLM 客户端（agent.llm is None 断言）、零 token、零费用。
- **涉及文件**：新增 src/agents/reward.py、benchmark/questions_hard.json、tests/test_reward.py；修改 benchmark/run_benchmark.py、tests/test_citations.py（解包适配）。

---

## G7 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 135/135 OK（G6 后 128 → 新增 test_task_watchdog 7 用例）；直跑 16/16 退出码 0；compileall 通过；全程离线（tempfile 任务库 + stub agent，mock 时间钟验证总超时）。
- **P1-2 落实项**：
  - TaskRecord 新增 `heartbeat_at`/`current_step`/`last_error_type`（from_dict 容错兼容旧文件）；每步骤边界刷新心跳与步骤摘要并随 store.update 落盘；失败步骤记错误类型、成功清空。
  - watchdog：`TaskRunner.sweep_once()`（running 且心跳停滞 > step_timeout_s → paused，error 含「看门狗」可读原因；活跃任务不误扫）+ `start_watchdog/stop_watchdog` daemon 扫描线程；心跳缺失回退 updated_at（升级兼容）。
  - 任务级总超时：_execute 步骤循环预检 elapsed > total_timeout_s → 停止推进、转 paused、error 含「总超时」。
  - 步骤循环新增外部迁移预检：落盘状态非 running/queued → 停止推进且不覆盖状态（watchdog/pause/cancel 与 worker 不互踩）。
  - 状态机：canceled 不可 resume、非法迁移 ValueError 含当前状态（既有实现，本轮补测试锁定）。
  - 配置：config.yaml 新增 `tasks.{step_timeout_s:600, total_timeout_s:3600, watchdog_interval_s:30}` + 校验规则 + 设置白名单；web_server.main 装配 start_watchdog。
- **延期决策（P1-2 剩余项，挂账）**：锁拆分（会话记忆/执行/写回三把锁）与可配置多 worker——当前单 worker + Handler.lock 串行已保证「记忆不串线/问答与任务不并发改记忆」，并行执行收益低且重构风险高；待任务量实测瓶颈后再做。
- **涉及文件**：src/agents/{task_store,task_runner}.py、config.yaml、src/agents/{config,web_server}.py、tests/test_task_watchdog.py（新增 7 用例）。

---

## G6 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 128/128 OK（G5 后 121 → 新增 test_audit 7 用例）；直跑 15/15 退出码 0；compileall 通过；全程离线（审计目录 tempfile，绝不写用户 data/audit/）。
- **模块**：新增 `src/agents/audit.py`——`AuditLogger`（JSONL 按天分文件只追加、retention_days 惰性清理、log_content 正文开关）+ 全局装配（`set_logger/get`）+ 会话上下文（`set_current_session`，threading.local）。
- **事件分层**（不记正文与 Key）：
  - `ask`：agent.ask 终态 + 任务 completed 路径（带 task_id）；ok/latency/llm_calls/tokens/cost/degraded/web_used/error_type；正文仅 log_content=True 写入（ACC-U6-02）。
  - `tool_call`：executor.execute 每工具步骤 1 条；action/ok/error_type/latency/attempts/degraded/input_keys（参数只记键名摘要，不记值）。
  - `llm_call`：llm.chat 与 chat_json（含修复轮）每次 _chat 1 条；ok/latency/tokens/model；埋点在 `_chat_audited`，子类覆写 _chat 的假客户端同样生效。
  - `admin`：config_save（只记键名摘要不记值）/memory_delete/memory_delete_session/memory_clear/session_reset/kb_rebuild；含操作者会话与结果摘要（ACC-U6-03）。
- **查询接口**：`GET /api/audit?date=&type=&limit=`——date 缺省当天、type 过滤、limit 1–1000（新→旧）；date 格式错/limit 非法 → 400，未知子路径 → 404（ACC-U6-04）。
- **装配与开关**：`config.yaml` 新增 `audit.{enabled,retention_days,log_content}`（默认 true/30/false）；`web_server.main()` 装配；`audit.enabled=false` 不装配（埋点 no-op）；validate_config 新增 audit.retention_days（1–3650）与 audit.{enabled,log_content} 布尔规则；CONFIG_FIELDS 白名单开放 retention_days/log_content。
- **涉及文件**：新增 src/agents/audit.py、tests/test_audit.py；修改 config.yaml、src/agents/{config,agent,executor,llm,task_runner,web_server}.py。

---

## G5 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 121/121 OK（G4 后 112 → 新增 test_key_writeback_security 9 用例）；直跑 14/14 退出码 0；compileall 通过；全程离线（密钥文件与任务库/会话库均在 tempfile，绝不读写用户 .env 与 data/runtime.json）。
- **密钥安全（P0-5）**：
  - `key_file_permissions_ok(path)`：owner 外任一权限位（组/其他）→ 过宽；文件缺失视为满足。
  - `load_config()` 启动时调用 `warn_key_file_permissions()`：.env / runtime.json 过宽即告警，提示将拒绝保存新密钥与 chmod 600。
  - `save_runtime()` 写后自动 `chmod 0600`（新建/覆写均生效）。
  - `/api/config` 保存新密钥前检查 runtime.json 权限，过宽返回 **403**（提示 chmod 600），普通配置项不受影响；权限收紧后重试放行。
  - **G2 观察项 3 关闭**：llm.api_key 保存路径去掉 `str()` 强转，与 vision.api_key 统一——int/float 等非字符串进入 validate_config 报类型错误 → 400；掩码回显值（含 `****`）忽略不落盘。
  - API 响应沿用 `_config_view` 掩码；TaskRecord to_dict/summary 无密钥字段（测试断言）；日志只记键名不记值。
  - 钥匙串扩展点：config.py 权限段注释已留接口约定（本期不引入依赖）。
- **任务写回幂等（P0-5）**：
  - `TaskRecord.writeback_id`（写回成功后置为 task_id 并持久化；from_dict 容错兼容旧文件）。
  - `WebStore.has_task_writeback(session_id, task_id)`：按 assistant 消息 metrics.task_id 检索；`WebStore.add_task_result(...)`：user+assistant 消息与 query_event 单连接事务原子落库（失败无半写）。
  - `Handler.persist_task_result`：writeback_id 已置位 → 跳过；库中已有同 task_id 消息（崩溃重放/旧记录升级）→ 只补标识不重复写；成功 → 原子写回 + `_mark_writeback` 持久化。重复回调/重启重放消息只出现一次。
  - 写回失败：任务保持 completed、答案不回滚、writeback_id 为空（可重试）；恢复后重试成功且不重复。`_notify_complete` 吞异常语义不变。
- **关键坑**：`from agents.config import RUNTIME_PATH` 是导入期绑定，测试 monkeypatch `config_mod.RUNTIME_PATH` 无效——web_server 改为 `config_mod.RUNTIME_PATH` 模块属性访问。
- **涉及文件**：src/agents/{config,web_server,web_store,task_store}.py、tests/test_key_writeback_security.py（新增 9 用例）。

---

## G4 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 112/112 OK（G3 后 103 → 新增 test_memory_governance 9 用例）；compileall 全仓通过；全程离线（TfidfHashEmbeddingBackend + 裸 Handler 桩，不读写用户 data/runtime.json 与 .env）。
- **P0-2 范围逐条落实**：
  - `GET /api/memory?session_id=&q=&limit=` → `{"episodes":[...], "total":n}`（episode dict 含 id/session_id/ts/question/answer_summary/sources/entities/hits——来源会话、创建时间、命中次数全透出）；长期记忆未开启返回 `{"episodes":[],"total":0,"disabled":true}`。
  - `DELETE /api/memory/{episode_id}` → 200/404；`DELETE /api/memory?session_id=` → `{"ok":true,"removed":n}`；`POST /api/memory/clear` → 长期 + 实体同步清空并返回计数；`GET /api/memory/entities` → 实体事实视图。
  - episode 来源会话/创建时间/命中次数字段 S4 已有（session_id/ts/hits），本切片通过 API 视图透出。
- **关键实现**：`LongTermMemory._drop_index`（TF-IDF 整体重建 / 其余后端向量行删除，保证删除后检索不维度错位）；`Handler.do_DELETE`（与 GET/POST 同款兜底与 Host 校验）；/api/reset 联动长期记忆删除（响应新增 `long_term_removed` 键，向后兼容）。
- **涉及文件**：src/agents/{long_memory,web_server}.py、scripts/webui.html（记忆设置页 + escHtml 工具 + CSS）、tests/test_memory_governance.py（新增 9 用例）。
- **注意**：/api/sessions 分支的原函数内 `from urllib.parse import ...` 局部导入上移到模块级（局部导入会使同函数内后续 `parse_qs` 触发 UnboundLocalError）。

---

## G3 切片记录（2026-09-04，控制器续接 zcode 会话完成）

- **验证证据**：`unittest discover` 103/103 OK（G2 后 88 → 新增 test_egress_defaults 15 用例）；直跑 12/12 退出码 0；`compileall` 全仓通过；全程离线（ScriptedLLM 假客户端 + monkeypatch 临时目录，绝不读写用户 data/runtime.json 与 .env）。
- **ACC-U3-01** ✓：降级步骤 → `/api/ask` 响应 `metrics.degraded==true`，旧键（latency_s/llm_calls/tokens/cost_yuan/citations_*）齐全。
- **ACC-U3-02** ✓：webui.html 答案卡片含「本回答来自 Web 搜索降级」/「本回答使用了 Web 搜索」状态行，渲染条件 `metrics.degraded`（优先）/`metrics.web_used`（次之），皆 false 不渲染。
- **ACC-U3-03** ✓：`_egress_flags` 统一口径（StepResult 与持久化步骤 dict 双支持）落任务写回与任务详情；`remember=false` 时零长期记忆写入、零实体抽取 LLM 调用。
- **关键实现**：`Agent.resolve_allow_web/_tools_for_run`（工具视图副本，强禁剔除 web_search 双保险：规划清单 + executor 降级门控）；`Agent._kb_miss(steps, web_allowed)`（禁网口径视为未联网）；`parse_opt_bool`（字符串布尔归一化，非法值 400）；`config._maybe_log_egress_migration_hint`（存量 runtime.json 未显式设置时 INFO 提示一次/进程）。
- **兼容策略**：runtime.json 显式配置优先、不强制覆盖；`Agent.ask()` 缺省参数下签名向后兼容（verbose/session_id 不变，新参均可选）。
- **涉及文件**：config.yaml（三开关翻转+注释）、src/agents/{config,agent,executor,task_runner,tools,web_server}.py、scripts/webui.html、tests/{test_egress_defaults(新),test_long_memory,test_task_runner,test_tool_hardening}.py。
