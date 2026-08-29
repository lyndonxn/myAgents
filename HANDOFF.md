# HANDOFF — Agent 五大核心模块补全 ✅ 已完成

根任务：按用户核对清单补全 myAgents 五大核心模块。规格：`spec/agent-core-modules.md`。契约：`AGENTS.md`。
分支：`feature/agent-core-modules`（起点 9ee2763）。推送状态：**未 push**（未获授权，如需推送请明确指示）。

## 完成总览（5/5 切片，全部通过独立验收）

| 切片 | 内容（对应清单项） | 验收 | 提交 |
| --- | --- | --- | --- |
| S1 | 工具层强化：JSON Schema 参数强校验、步骤级重试、KB→Web 降级路由、chat_json 解析失败修复轮（清单 3/9/10） | ACC-S1-01..04 全 PASS | 56ad6bf |
| S2 | ReAct 迭代：反思重规划（Self-Reflection）、`@step:N` 步骤数据传递（多工具链式）、预算护栏（清单 1/2） | ACC-S2-01..04 全 PASS | c5c6519 |
| S3 | 引用校验：幻觉引用剔除与计数、评测新增 completion_rate/hallucination_rate/task_completed、可选 `--judge` LLM 评审（清单 7/8） | ACC-S3-01..03 全 PASS | 40438d5 |
| S4 | 记忆体系：长期记忆向量库（data/memory/，TF-IDF/本地句向量）、滚动摘要压缩、容量遗忘（hits+新旧）、实体记忆、检索增强规划、摘要随会话持久化（清单 4/6） | ACC-S4-01..04 全 PASS | cc24700 |
| S5 | 任务状态机：TaskStore 逐步落盘（data/tasks/）、暂停/恢复/取消、崩溃恢复（recover_running）、`/api/tasks` 系端点（清单 5，仅后端） | ACC-S5-01..04 全 PASS（复验通过） | 320498e |

## 新增能力入口
- 配置：`config.yaml` 新增 `llm.json_repair_rounds`、`tools.max_retries/kb_fallback_web`、`planner.reflect/max_reflections`、`memory.*` 四键（默认开启，可关）。
- API：`GET/POST /api/tasks`、`GET /api/tasks/{id}`、`POST /api/tasks/{id}/pause|resume|cancel`（现有 /api/ask 系行为不变）。
- Agent 内部：`plan_only()/finish_task()` 任务路径拆解件；`Answer.citations_valid/invalid`；`Plan.rounds/reflections`。
- 评测：`python -m benchmark.run_benchmark --complete-threshold 0.5 [--judge]`（--judge 付费，默认关）。

## 验证状态（最终全量回归，全绿）
`py_compile` 全仓 ✓；tests/test_smoke、test_web_store、test_tool_hardening、test_react_loop、test_citations、test_long_memory、test_task_runner 七个套件全部通过。完整 LLM 评测（付费）未运行；`--retrieval-only` 实跑 15 题文件命中 100%/章节命中 93.3%。

## 过程记录
- 每切片流程：实现分支会话 → 控制器审 diff+复跑验证 → 独立验收子代理逐条核对验收 ID → 提交。
- S5 首轮验收 REJECTED（HTTP 任务控制路由解析段数错误恒 404），控制器修复后复验 ACCEPTED——该缺陷单元测试未覆盖（只测了 TaskRunner 层），由验收子代理的 HTTP 级实测发现。
- 控制器补充修复两处：S2 payload 透出 rounds/reflections；S4 会话摘要随 SQLite 持久化（消除每 ask 一次的重复压缩调用）+ 旧库迁移（sessions.summary 列）。

## 未完成 / 延期项
- 任务暂停/恢复的 Web UI、引用校验前端展示（用户已确认延期；webui.html 无 /api/tasks 引用）。
- 评测题库扩容：步骤见 benchmark/README.md「扩容题库的步骤」；私人知识库不得生成题目入库。
- CLI（scripts/ask.py）未暴露任务路径；任务答案不写回会话消息（record 本身即持久化）。

## 风险与注意
- 用户工作区未提交变更（`dist/` 删除、`docs/`、`.zcode/` 未跟踪）保持原样，未纳入任何提交。
- `data/runtime.json` 含真实 API Key（已 gitignore；曾在分析上下文出现，建议轮换）。
- S4 长期记忆位于 `data/memory/`、S5 任务位于 `data/tasks/`，均已随 data/ 被 gitignore 排除。

## 下一步动作
如需合入：将 feature/agent-core-modules 合并/提 PR 至 main（需用户授权 push）。如需继续：前端接入任务面板、CLI 任务模式、题库扩容（需可公开示例知识库）。
