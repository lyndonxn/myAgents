# LangGraph 编排引擎 — Phase 1 验收规格（spec/08 §5 对应）

来源：工作区 harness `/Users/mima1234/.zcode/workspace/default/spec/08-langgraph-upgrade-plan.md`（T-007）。
分支：`feat/langgraph-orchestration`（接 Phase 0）。验收前缀：`LG-ACC`。Phase 0 规格见 `spec/langgraph-phase0.md`。

## 范围

在 Phase 0 最小图之上增加**会话与恢复语义**：SQLite Checkpointer 持久化、跨实例会话续接、追问改写、检索零命中时的 interrupt 澄清、崩溃恢复。检索引擎继续冻结（C6）。

## 交付物

- `src/agents/agent_graph/state.py`：GraphState 扩展（history/search_query/clarify_round/pending_requery/llm_call_count）
- `src/agents/agent_graph/nodes.py`：route_entry（追问改写，复用 planner.rewrite_query）、clarify_check（interrupt/Command 恢复）、commit_turn（轮次提交）、route_clarify
- `src/agents/agent_graph/runner.py`：`_get_checkpointer`（同步 SqliteSaver）、thread_id=会话映射、挂起检测 + Command(resume)、`resume=True` 崩溃续跑（invoke(None)）
- `src/agents/config.py` + `config.yaml`：`graph.checkpoint_db`（默认 data_dir/checkpoints.sqlite）、`graph.clarify_on_miss`（默认 false）
- `src/agents/agent.py`：`Answer.pending_clarification`（新增字段，带默认值）；`Agent.ask(resume=False)` 新参数
- `requirements-langgraph.txt`：+`langgraph-checkpoint-sqlite>=2.0`（本机 3.1.1）
- `tests/test_agent_graph_session.py`（6 项）+ `tests/_lg_crash_child.py`（崩溃演练子进程）

## 验收标准（Given/When/Then）

### LG-ACC-004 跨实例会话续接
- Given：同一 checkpoint 库、先后创建的两个 Agent 实例；实例 A 已完成一轮问答（session_id=s1）
- When：实例 B 以同 session_id 追问
- Then：B 的追问改写提示词包含 A 轮历史（history 经 checkpoint 可见）；`llm_calls==2`（改写+合成）；legacy SessionMemory 保持为空（不双写）。
- 附：无 session_id 的调用不落 checkpoint、不泄漏历史（两问各 1 次 LLM 调用）。

### LG-ACC-005 interrupt 澄清闭环
- Given：`graph.clarify_on_miss: true`、检索真零命中（空库口径，与 legacy `_kb_miss` 同判据）
- When：`ask(q, session_id)` → 图在 clarify_check 挂起
- Then：`Answer.ok==True` 且 `final_answer==""`、`pending_clarification.type=="clarify"`、`llm_calls==0`；
  同会话下一问被当作澄清内容经 `Command(resume)` 注回 → 重跑检索 →（仍零命中时）澄清轮数上限 1 → miss_hint 口径合成、`llm_calls==1`；
  用户跳过（`Command(resume="")`）→ 直接 miss_hint 口径合成，无循环。

### LG-ACC-006 崩溃恢复
- Given：子进程在合成 LLM 调用点 `os._exit(9)`（retrieve/clarify_check 两 superstep 的 checkpoint 已落库）
- When：父进程以全新实例、同 checkpoint 库调用 `ask("", resume=True, session_id=…)`
- Then：从最近 checkpoint 续跑——合成恰在父进程执行 1 次；检索节点零重跑（store.search 计数为 0）；`Answer.ok==True`。

### C2/C5 延续
- 全量套件绿（Phase 0 后基线 207 + 本切片 6 = 213）；错误路由语义不变；新增测试全部离线。

## 已声明偏差（评审依据，非遗漏）

1. **同步 SqliteSaver（spec 原文为 AsyncSqliteSaver）**：Agent.ask 是同步契约（web 端点在线程池调用），按实际接缝用同步版 `SqliteSaver`（连接 `check_same_thread=False`，单用户本地由 sqlite 写锁串行化）。
2. **澄清触发口径**：`chunks` 为空才触发（与 legacy `_kb_miss` 的 hit_count 判据同源——现有检索 top-k 恒非空，故两者实际只在真空库时触发）。基于分数的置信度阈值受融合分数量纲影响（RRF≈0.0x），Phase 2 评测后再定，本期不做。
3. **澄清的 Web 端点未做**：interrupt 经 `Answer.pending_clarification` 暴露 + "同会话下一问即澄清"语义已可用；专门的 /api clarify 端点属后续切片（避免未经设计评审扩前端契约）。
4. **checkpoint 仅在提供 session_id 或 resume 时启用**：无会话调用保持无状态，避免 checkpoint 无界增长（adhoc 线程不复用）。
5. **checkpoint 序列化**：state 携带 RetrievedChunk/SearchHit/StepResult 等对象，依赖 langgraph serde 的 pickle 兜底——本地单用户可接受；Phase 2 若上云评测需复核。
