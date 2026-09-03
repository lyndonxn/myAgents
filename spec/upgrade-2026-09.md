# Spec · 2026-09 升级（企业安全 + 度量 + 离线）

分支：`feature/upgrade-2026-09`（起点：main @ 9e6791f）。契约：根 `AGENTS.md`。
目标：不动架构、不写新智能体，把已验收的 harness 升级为"质量可度量、企业安全达标、离线可用"的版本。
决策来源：2026-09-04 用户确认的 11 目标清单（对话定稿）。

## 1. 目标总表与交付顺序

| 批次 | # | 目标 | 验收来源 | 主要 Owned paths |
|---|---|---|---|---|
| 一 | G1 | 测试入口标准化 | `p0-p1-improvement.md` P0-1 | `tests/**` |
| 一 | G2 | 配置校验 | 同上 P0-4 | `src/agents/config.py`、`web_server.py` |
| 二 | G3 | 外发默认关闭 + 回答状态披露 | 同上 P0-3 + 本文件 ACC-U3 | `config.yaml`、`config.py`、`web_server.py`、`agent.py`、`webui.html` |
| 二 | G4 | 记忆治理 | 同上 P0-2 | `long_memory.py`、`web_server.py`、`memory.py` |
| 二 | G5 | 密钥安全 | 同上 P0-5 | `config.py`、`web_server.py`、`task_runner.py` |
| 二 | G6 | 审计轨迹 | 本文件 ACC-U6 | 新增 `src/agents/audit.py`、`executor.py`、`llm.py` |
| 三 | G7 | 任务 watchdog | 同上 P1-2 | `task_runner.py`、`task_store.py` |
| 四 | G8 | 难评测集 + reward 模块 | 本文件 ACC-U8 | `benchmark/**`、新增 `src/agents/reward.py` |
| 四 | G9 | 速度与成本包 | 本文件 ACC-U9 | `agent.py`、`planner.py`、`retriever.py`、`config.yaml` |
| 四 | G10 | 检索迭代循环 | 本文件 ACC-U10 | `planner.py`、`executor.py`、`retriever.py` |
| 四 | G11 | 本地 LLM 离线档位 | 本文件 ACC-U11 | `llm.py`、`config.py`、`config.yaml`、`webui.html` |

顺序：G1 → G2 → G3 → G4 → G5 → G6 → G7 → G8 → G9 → G10 → G11。每切片走根 AGENTS.md 完成定义（实现 → 控制器验证 → 独立验收 → 提交 → HANDOFF）。

## 2. 新增验收（既有 P0/P1 验收 ID 继续有效，此处只定义新目标）

### G2 补充：校验规则表（P0-4 的可执行细化）

只校验显式提供的值，缺失键用默认值不算错误；收集全部错误（不 fail-fast）；每条消息含完整 dotted 路径 + 中文修复建议；路径含 key/token/secret 的绝不回显值。

- **整数**（bool 不算 int，范围闭区间）：`retrieval.top_k` 1–100；`retrieval.rerank_candidates` 1–200；`planner.max_steps` 1–50；`planner.max_reflections` 0–10；`tools.max_retries` 0–5；`tools.search_default_top_k` 1–50；`llm.max_retries` 0–10；`llm.json_repair_rounds` 0–5；`llm.max_tokens` 64–32768；`llm.timeout` >0；`memory.max_episodes` 1–100000；`embedding.hash_dim` 64–65536；`chunking.min_chars` 0–100000。
- **浮点**（int 或 float 均可）：`llm.temperature`、`planner.temperature` 0–2；`retrieval.vector_weight`、`retrieval.keyword_weight`、`retrieval.rerank_blend` 0–1；`tools.web_search.timeout` >0；`tools.web_search.cache_ttl` ≥0。跨字段：`chunking.max_chars` 必须大于 `chunking.min_chars`。
- **枚举**（大小写不敏感）：`retrieval.rerank` ∈ off/auto/cross_encoder/llm（bool true/false 向后兼容）；`retrieval.fusion_mode` ∈ rrf/weighted；`embedding.backend`、`memory.embedding_backend` ∈ auto/local/tfidf。
- **布尔**：接受 bool 或字符串 true/false/1/0/yes/no（大小写不敏感，加载时归一化为真 bool）；键集为全部 *_enabled、contextual_augment、multi_query、fallback_direct、reflect、kb_fallback_web。
- **字符串/格式**：`kb_path`、`data_dir` 非空且无 NUL；`llm.base_url`、`vision.base_url`（非空时）须 http(s):// 开头；`llm.chat_model`、`planner.model`、`embedding.model`、`retrieval.reranker_model` 非空且无空白；`vision.model` 非空时无空白；`llm.api_key`、`vision.api_key` 只查类型为 str。
- **接口**：`validate_config(raw) -> list[str]`（空=通过）；`ConfigError(RuntimeError)` 携带 `.errors`；`load_config()` 校验失败抛 ConfigError（进程不静默启动）；POST `/api/config` 对合并后配置校验，失败返回 400（响应含 errors 列表，不回显 Key）。测试经 monkeypatch RUNTIME_PATH/CONFIG_PATH 到临时目录，绝不读写用户 data/runtime.json 与 .env。

### G3 挂载项：回答状态披露（并入 G3 切片交付）

- **ACC-U3-01** Given 某步 KB 检索降级走 Web（`StepResult.degraded=True`），When `/api/ask` 响应，Then `metrics.degraded==true` 且旧字段不变。
- **ACC-U3-02** Given 响应 `metrics.degraded==true`，When WebUI 渲染答案卡片，Then 出现「本回答来自 Web 搜索降级」状态行；false 时不出现。
- **ACC-U3-03** Given 本次问答发生 web_search 外发，When 落库与审计记录，Then 元数据含外发标记；`remember=false` 时不写长期记忆。

### G6 审计轨迹

- 分层：默认记录操作级事件（时间/会话/动作类型/工具名/参数摘要/ok/error_type/耗时/token/来源文件/模型/degraded/外发标记），**不记录问题与答案正文**；正文记录由设置显式开启且默认关；API Key 与 .env 内容永不记录。
- 存储：JSONL 按天分文件（`data/audit/YYYY-MM-DD.jsonl`），只追加，保留天数可配；查询接口 `GET /api/audit?date=&type=&limit=`。
- 埋点单点：工具调用在 `executor.py`、LLM 调用在 `llm.py:_chat`、管理操作（配置保存/记忆删除/索引重建/清空）在各写操作函数。
- **ACC-U6-01** Given 一次成功问答（含工具调用与 LLM 调用），When 查看当日审计文件，Then 至少 1 条 ask 事件、每工具 1 条 tool_call 事件、每 LLM 调用 1 条 llm_call 事件，均无正文、无 Key。
- **ACC-U6-02** Given 开启正文记录，When 问答，Then ask 事件含问题与答案；关闭时不含。
- **ACC-U6-03** Given 记忆删除与配置保存，When 操作完成，Then 对应管理事件存在且含操作者会话与结果。
- **ACC-U6-04** When `GET /api/audit` 带过滤条件，Then 返回匹配事件且非法参数返回 4xx。

### G8 难评测集 + reward 模块

- 题型 ≥10 题/类：直接事实、多跳、无答案拒答、冲突来源、工具选择、长上下文、提示注入、跨会话记忆、网络不可用、（公开样例库）改写同义。
- 指标：p50/p95 延迟、单题 prompt/completion token、分题型分层汇总、失败类型分布、`refusal_accuracy`。
- 判分函数独立为 `src/agents/reward.py`：输入（question, answer, sources, expectations）→ 输出分数与失败类型；benchmark 与未来离线档位共用。
- **ACC-U8-01** Given 九类题库文件，When `--retrieval-only` 跑公开子集，Then 全绿且报告含 p50/p95 与分题型汇总。
- **ACC-U8-02** Given 构造的拒答题（KB 无答案），When reward 评分，Then 拒答正确得分、编造答案扣分并记 failure_type。
- **ACC-U8-03** When 带 `--judge`（付费）之外的方式运行，Then 全程零 LLM 调用、零费用。

### G9 速度与成本包

- 快路径：简单事实题跳过 planner，直接"检索→合成"（启发式门控起步；`planner.need_retrieval` 显式输出为进阶形态，允许后续切片）。
- 合成 `max_tokens` 收紧（默认 ~1024，可配）；`_synthesize` prompt 重排：稳定前缀（系统提示+上下文）在前、对话历史与问题在后（DeepSeek 上下文缓存友好）。
- 精排后句子级证据压缩：对入选父块按叶子/句子相关性裁剪送入合成的文本量，目标输入 token 降 ≥30%。
- **ACC-U9-01** Given 简单事实题，When ask，Then 全程 ≤1 次规划调用（快路径生效），答案与来源正常。
- **ACC-U9-02** Given 需要多跳/工具的题，When ask，Then 快路径不触发，行为与现版本一致（回归）。
- **ACC-U9-03** When 对比改造前后 100 题基准（授权后实跑），Then 单题平均延迟与 prompt token 下降，completion/quality 指标不回退。

### G10 检索迭代循环

- 形态：检索→（LLM 或规则）评估命中→按需改写 query 再检索，复用 `@step:N` 占位符机制；搜索次数预算 `planner.max_search_calls`（默认 3）防失控。
- **ACC-U10-01** Given 多跳题首轮检索未命中，When 反思/迭代触发改写查询再检索，Then 第二轮命中且总搜索次数 ≤ 预算。
- **ACC-U10-02** Given 改写后的空转循环（连续未命中到预算上限），When 预算耗尽，Then 停止迭代、按现有未命中路径披露。
- **ACC-U10-03** When 在难评测集多跳/改写题型上对比 G8 reward，Then 该题型分数提升且其他题型不回退。

### G11 本地 LLM 离线档位

- `LLMClient` 允许本地端点空 API Key；`chat_json` 对非 deepseek 模型强化提示词侧 JSON 约束（修复轮机制不变）。
- config 新增离线档位（`llm.base_url` 指向本地 OpenAI 兼容端点 + 关闭 web 工具注册 + 外发调用快速失败）。
- 一键准备脚本：预拉 embedding/reranker 缓存并校验可用性（不下载 LLM 权重，仅提示 ollama 命令）。
- **ACC-U11-01** Given 无 API Key + 本地 base_url，When 构造 LLMClient 并 chat_json（fake 本地服务），Then 正常工作不抛 Key 错误。
- **ACC-U11-02** Given 离线档位开启，When KB 未命中，Then 不触发任何网络调用，按未命中路径披露。
- **ACC-U11-03** When 离线 E2E 测试（fake 本地 LLM + 本地 embedding），Then 全链路绿。

## 3. 边界与延期决策（明确不做/后置）

- **不做**：重写/换框架、RL 训练栈（veRL 等）、代码解释器工具、embedding 模型换挡、Web UI 重设计。
- **需规格决策后另立切片**：认证/多用户、文档级 ACL、索引增量更新。
- **挂账**：P1-4 证据级引用校验（"证据不足"披露）、P1-1 模块解耦、P1-5 API 错误契约（G3/G6 的 payload 手法须为其留兼容口）。
- **远期可选**：本地小模型 SFT、MCP 接入（审计层已留命名约定）、KB 图片入库检索。
- **G1 覆盖率指标（≥80%）延期**：测量需引入 coverage.py，超出当前轻依赖授权范围；本切片以发现率/通过率/六类覆盖/离线为准，覆盖率工具化待用户授权。

## 4. 全局约束（继承 AGENTS.md）

离线测试（fake LLM/monkeypatch，禁真实 LLM 与联网）；零新增第三方依赖；`Agent.ask()`、`/api/ask`、`/api/ask_stream`、`SessionMemory` 公开方法向后兼容；禁碰 `dist/ docs/ data/ logs/ .env* assets/`。付费 LLM benchmark 未经用户指令不得运行（ACC-U9-03/实跑评测需单独授权）。
