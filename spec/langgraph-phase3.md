# LangGraph Phase 3 — ReAct 子图 + 澄清端点（spec/09 T-010/T-011 对应）

依据：工作区 harness `spec/09-mcp-and-phase3-plan.md`。分支 `feat/mcp-vault-phase3`。验收前缀 `PH3-ACC`。

## 交付物

- `src/agents/agent_graph/react.py`：`make_react_nodes`——plan/execute/reflect 三节点（**整体复用** Planner/Executor/`_reflect_and_extend`，@step 占位、工具重试/降级、检索预算、反思硬上限全部 legacy 语义零复制）；plan_node 内做 G9 快路径判定（legacy 同口径）
- `src/agents/agent_graph/nodes.py`：generate 节点双路汇合（state.steps 存在 → ReAct 口径：真实工具步骤 + `_collect_source_info`/`_kb_miss`；否则单发口径）
- `src/agents/agent_graph/runner.py`：`allow_web` 贯穿（`resolve_allow_web`/`_tools_for_run` 与 legacy ask 同口径）；`_finalize_answer` 支持 steps 列表与 legacy 计数口径
- `src/agents/agent_graph/state.py`：`fast_path`/`steps`/`reflect_llm_calls` 字段；`llm_call_count` 加 `operator.add` 归并（**修复 route 的改写计数被 plan 覆盖丢失**——回归实测抓出）
- `src/agents/web_server.py`：`/api/clarify`（409 守卫 NO_PENDING_CLARIFICATION / 引擎守卫）、`pending_clarification` 入 payload、流式 `clarify` 事件
- `tests/test_agent_graph_react.py`（4）+ `tests/test_clarify_endpoint.py`（2）+ ACC-T1-03 契约测试加法演进

## 验收标准

- **PH3-ACC-002 快路径路由**：简单事实题（无历史）→ 单发分支，llm_calls==1，plan.reasoning 含快路径（回归 Phase 0/1）。
- **PH3-ACC-003 多步路径**：复杂问题 plan→execute→reflect→synth；补步被执行（steps 含 search+calculator）、Plan.rounds+1；反思无需补步不扩展；LLM 全面故障时 planner 内部 fallback（legacy 同款）→ 合成失败 → C5 优雅终止无半截答案。
- **PH3-ACC-001 澄清端点**：挂起会话经 `/api/clarify` 恢复闭环（守卫 → ask 同路径 → 挂起清除）；payload 两态透出；无挂起 409。
- **回归**：全套 229 绿；多轮会话第二问现在走完整规划（legacy 同口径——Phase 0/1 的"永远单发"简化被本切片修正，相关测试脚本已同步演进）。

## 已声明偏差（非遗漏）

1. 图引擎 ReAct 模式的检索经 legacy 工具路径（`tool_search_knowledge_base`），并行子图检索仅单发分支（C6）。
2. 长期记忆注入仍缺（Phase 0/1 同口径）。
3. 前端澄清卡片不在本切片（`/api/clarify` + 流式 `clarify` 事件已就绪，UI 另行切片）。
4. ReAct 的付费 A/B 待用户授权（沿用 5 题口径框架）。
5. checkpoint serde pickle 兜底对象新增 StepResult/Plan（既有偏差 5 的延伸，升级 langgraph 前统一复核）。
