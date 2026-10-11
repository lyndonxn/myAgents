# LangGraph 编排引擎 — Phase 2 验收规格（spec/08 §6 对应）

来源：工作区 harness `/Users/mima1234/.zcode/workspace/default/spec/08-langgraph-upgrade-plan.md`（T-008）。
分支：`feat/langgraph-orchestration`（接 Phase 0/1）。验收前缀：`LG-ACC`。

## 范围

token 级流式输出（LG-2.1）、节点指标随 Answer 暴露（LG-2.2）、benchmark 双引擎 A/B 框架与免费档跑批（LG-2.3）。付费完整 A/B 与报告（LG-2.4/2.5）待决策点 D2，本切片不含。

## 交付物

- `src/agents/llm.py`：`chat_stream`（OpenAI 兼容 SSE；include_usage；连接期可重试、产出 token 后不可重试）
- `src/agents/agent.py`：`_synthesis_messages`（提示词单点，_synthesize/synthesize_stream 共用）+ `synthesize_stream`；`Answer.node_metrics` 新字段（带默认值）
- `src/agents/agent_graph/nodes.py`：generate 节点 `streaming` 闸门（仅 stream 构建置真）+ `get_stream_writer` 产出 `{"event":"delta"}` 自定义事件
- `src/agents/agent_graph/runner.py`：`_prepare_run`/`_finalize_answer` 共用层 + `stream_graph_ask` 同步生成器（stream_mode=["custom","values"]）
- `src/agents/agent_graph/state.py`：`error` 键 `_first_error` 归并 reducer（并行分支同时失败不再 InvalidUpdateError——回归实测抓出）
- `src/agents/web_server.py`：/api/ask_stream 按 engine 分支——langgraph 走 stream_graph_ask 实时下发 delta；legacy 保持原 24 字符切块
- `benchmark/run_benchmark.py`：`--engine {legacy,langgraph}`；retrieval-only 按 engine 选执行路径（legacy 管线 / 图子图）

## 验收标准（Given/When/Then）

### LG-ACC-007 真 token 流式
- Given：`engine: langgraph`、支持流式的 LLM（测试用双通道假 LLM）
- When：`stream_graph_ask(...)` 迭代事件
- Then：delta 事件为逐 token 序列（与 SSE chunk 一致，非整段切块）；终止事件 `_result` 携带完整 Answer（ok、引用校验完成）；invoke 路径（run_graph_ask）零触达流式实现（守卫假 LLM 断言）；流式 × Checkpointer 会话共存（第二轮改写 + 逐 token 流式）。
- 前端呈现（浏览器实测）属运行时验证项：事件顺序 delta 先于 meta（与 legacy 相反），待用户浏览器验证。

### LG-ACC-009 节点指标归因
- Given：图引擎完成一次问答
- When：读取 `Answer.node_metrics`
- Then：包含全部 9 节点（route_entry/vector_recall/bm25_recall/fuse/expand_parents/rerank/generate/validate_citations/commit_turn）且每项含 `duration_ms`。

### LG-ACC-008（免费档部分）A/B 框架与检索无回退
- Given：同一 vault 知识库与 26 题评测集、C6 检索冻结
- When：`--retrieval-only --engine {legacy,langgraph}` 各跑一遍
- Then：两侧命中率完全一致（文件/章节），延迟同量级；实测（2026-10-11）：legacy 96.2%/80.8% p50=28ms vs langgraph 96.2%/80.8% p50=25ms（快照 `benchmark/results-lg-phase2-ab-retrieval-2026-10-11-{legacy,langgraph}.txt`）。
- 付费完整 A/B（答案质量/Token/拒答/引用幻觉）待 D2 授权后执行。

### 回归与硬约束
- 全套 218 测试绿（214+4）；新增测试全离线；`_first_error` reducer 修复并行失败崩溃（C5 增强）；legacy ask 流式路径行为不变（守卫测试）。

## 已声明偏差（评审依据，非遗漏）

1. **web 响应未加 node_metrics 键**：`_answer_payload` 顶层键与 metrics 键集被既有契约测试锁定（ACC-T1-03，键集相等断言）；节点指标经 `Answer.node_metrics`（引擎 API）与 benchmark 消费暴露，web 层透出留待前端需求确认后做加法扩展。
2. **get_stream_writer 闸门**：generate 节点仅在 `streaming=True`（stream_graph_ask 构建）时尝试取 writer；invoke 路径 on_token 恒 None → 非流式 chat，测试假 LLM 不会误触真实 SSE 实现。
3. **chat_stream usage 依赖 include_usage**：端点不支持时 token 计 0（成本记 0），如实降级。
4. **事件顺序变化**：图引擎流式下 delta 先于 meta（meta 依赖完成后才有的 sources）；与 legacy 顺序不同，前端按事件类型渲染，浏览器实测确认。
5. **D2 未决**：付费完整 A/B（~100 题 × 2 引擎 × 1-2 次调用，预估成本 < ¥1）待用户授权。
