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

# 2026-09 升级（进行中）

分支：`feature/upgrade-2026-09`（起点 main @ 9e6791f）。规格：`spec/upgrade-2026-09.md`（11 目标 / 4 批，G1–G11）。控制器 = 根会话；分支会话用子代理承载，均已完成并整合。

## Gate 0 能力清单（一次性，全分支复用）
darwin 25.6.0 arm64；原生 Read/Glob/Grep/Bash/Edit 可用（Windows 脚本不适用）；python=.venv 3.13；本升级无外部连接器需求；付费 LLM benchmark 禁跑（除 ACC-U9-03 待单独授权）。

## 已完成
| 切片 | 内容 | 验收 | 提交 |
| --- | --- | --- | --- |
| G1 | 测试入口标准化（P0-1）：10 个 plain-script 套件 → unittest 可发现，76 用例；直跑兼容保留；统一入口写入 AGENTS.md | 独立验收 ACCEPTED（ACC-P0-1-a..f 全过） | bcb8121(spec) + cfee0d7(tests+AGENTS) |
| G2 | 配置校验（P0-4）：validate_config/ConfigError/normalize_config 落地规则表；load_config 失败抛错不静默启动；POST /api/config 无效值 400（errors 列表，不回显 Key）；归一化布尔字符串与枚举 | 独立验收 ACCEPTED（ACC-P0-4-a..f 全过，含用户 runtime.json 逐字节不变取证） | 85957d5（含 spec 规则表） |

验证证据：`unittest discover` 76/76 OK（改造前 0）；直跑 10/10 退出码 0；失败路径非 0（/tmp 验证）；离线（fake LLM/桩，data/ 用户库零写入）；六类覆盖映射完整。

## 观察项（预存行为，非本切片缺陷，待后续切片决定）
1. `tests/test_smoke.py::test_load_chunks` 在 cfg.kb_path 指向真实知识库时会只读访问（有存在性守卫、旧行为原样保留）；如需完全隔离可改 fixture 库。
2. `tests/test_benchmark_sample.py` 依赖本机已缓存的 bge-small-zh-v1.5；冷缓存环境可能触发下载。
3. （G2 验收发现）`/api/config` 保存路径 `llm.api_key` 沿用既有 `str()` 转换，int 型 Key 被转字符串落盘而 `vision.api_key` 会 400——统一口径放到 G5 密钥安全切片。
4. （G2 验收发现）CONFIG_FIELDS 白名单外键在保存路径被静默丢弃，属既有行为。

## 延期决策
- G1 覆盖率指标（≥80%）延期：需 coverage.py，超出轻依赖授权，待用户授权。
- G2 沿用 `{"error", "errors"}` 响应体：P1-5 统一错误码契约挂账后置。

## 下一步动作
G3 数据外发默认关闭 + 回答状态披露（P0-3 + ACC-U3-01..03）：三开关默认 false、`allow_web`/`remember` 字段、runtime.json 迁移策略、degraded/外发标记透出 payload 与答案卡片；与 G4 记忆治理共用设置页改动面。push 状态：未 push（未授权）。
