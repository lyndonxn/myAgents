# HANDOFF — Agent 五大核心模块补全

根任务：按用户核对清单补全 myAgents 五大核心模块。规格：`spec/agent-core-modules.md`。契约：`AGENTS.md`。
分支：`feature/agent-core-modules`（起点 9ee2763）。推送状态：**未 push**（未获授权）。

## 已完成工作与证据
- 基线验证（改动前）：`py_compile` 全过、`tests/test_smoke.py` 全过、`tests/test_web_store.py` 全过。
- Harness 文档：`AGENTS.md`、`spec/agent-core-modules.md`、本文件。

## 已确认决策
- S5 任务暂停/恢复仅后端 API，Web UI 延期。
- 新智能能力（反思重规划/长期记忆/实体记忆）默认开启，config.yaml 可关。

## 切片状态
| 切片 | 内容 | 状态 |
| --- | --- | --- |
| S1 | 工具层强化（schema 校验/重试/降级路由/JSON 修复） | 进行中 |
| S2 | ReAct 迭代 + 反思重规划 + 步骤传递 | 未开始 |
| S3 | 引用校验 + 评测增强 | 未开始 |
| S4 | 长期记忆 + 压缩/遗忘 + 实体记忆 | 未开始 |
| S5 | 任务状态持久化 + 暂停/恢复（后端） | 未开始 |

## 未完成 / 延期项
- 任务暂停/恢复 Web UI、引用校验前端展示（用户已确认延期）。
- 评测题库扩容：需绑定知识库内容，用户私人笔记不得生成题目入库；步骤见 benchmark/README.md（S3 更新）。
- 完整评测（付费 LLM 调用）未运行；仅离线测试。

## 风险与注意
- 用户工作区有未提交变更（`dist/` 删除、`docs/` 未跟踪），属于用户所有，禁止纳入提交。
- `data/runtime.json` 含真实 API Key（已 gitignore，曾出现在分析上下文中，建议用户轮换）。

## 下一步动作
派发 S1 实现分支会话 → 控制器验证 → 独立验收 → 提交。
