# LangGraph 编排引擎 — Phase 0 验收规格（spec/08 §4 对应）

来源：工作区 harness `/Users/mima1234/.zcode/workspace/default/spec/08-langgraph-upgrade-plan.md`（T-006）。
分支：`feat/langgraph-orchestration`。验收前缀：`LG-ACC`。

## 范围

引入 LangGraph 1.0 作为**可选编排引擎**：`config.yaml` 新增 `engine: legacy | langgraph`（默认 legacy）。
Phase 0 = 最小图打通（route → 检索子图 → generate → 引用校验），检索引擎冻结（C6）。

## 交付物

- `src/agents/agent_graph/`：`state.py`（GraphState）、`retrieval_subgraph.py`（向量/BM25 并行召回→融合→父块→精排）、`nodes.py`（合成/校验/错误出口/条件边）、`runner.py`（主图组装 + run_graph_ask）、`__init__.py`（lazy 导出）
- `src/agents/config.py`：`engine` 枚举 + getter
- `src/agents/agent.py`：`ask()` 单点分派（LG-0.6）
- `requirements-langgraph.txt`：可选依赖（默认 legacy 零新增依赖）
- `tests/test_agent_graph.py`：10 项离线测试

## 验收标准（Given/When/Then）

### LG-ACC-001 图引擎端到端
- Given：`engine: langgraph` 的 Agent（索引就绪、LLM 注入）
- When：调用 `Agent.ask(question)`
- Then：返回 `Answer` 与 legacy 结构兼容——`ok=True`、`final_answer` 非空、`citations_valid/invalid` 按实际来源数统计、`sources`/`sources_detail` 下标对齐、`steps` 恰含一个 `search_knowledge_base` 步骤。
- 证据口径：离线 fake LLM（AGENTS.md 硬约束 1）。**真实 LLM 端到端属付费操作（C1/D2），待用户授权后补跑，当前如实标注为未执行。**

### LG-ACC-002 legacy 无回归
- Given：默认配置（未设 engine）
- When：全量测试套件
- Then：`python -m unittest discover -s tests -p "test_*.py"` 全绿（基线 197 + 本切片 10 = 207）；G9 快路径行为不变（简单事实题 1 次 LLM 调用）；`engine` 非法值被 `validate_config` 拒绝。

### LG-ACC-003 检索子图一致性（冻结引擎 C6）
- Given：同一 Agent 实例（同一索引/精排配置，multi_query 关闭）
- When：同题分别走 `Retriever.retrieve()` 与检索子图
- Then：父块文件序列、排序、`final_score`（6 位小数）、命中叶子文本完全一致。

### C5 错误显式路由（spec/08 硬约束）
- Given：合成节点内 LLM 调用失败（测试注入）
- When：图执行至 generate
- Then：`error` 写入状态 → 条件边路由 `error_node` → `Answer.error` 非空、`final_answer` 为空（无半截产出）、进程不崩溃。

## 已声明偏差（评审依据，非遗漏）

1. **检索子图节点粒度**：spec/08 列"向量召回→BM25→RRF→父块扩展"4 节点；实现为 5 节点（增加 rerank 节点，对应 `retrieve()` 的精排阶段），前三阶段因现有代码内聚于 `Retriever` 的可复用方法而按既有函数边界拆分——零检索逻辑复制，优先满足 C6。
2. **multi_query**：legacy 在 `multi_query=true` 时做 LLM 查询变体展开；图路径 Phase 0 暂不含该分支（config 默认 false）。一致性测试在关闭口径下对齐。
3. **依赖安装方式**：以 `requirements-langgraph.txt` 可选清单交付（本机 venv 已装 1.2.14/1.6.9/1.7.0），不进 `requirements.txt`——保留 AGENTS.md"轻依赖"硬约束精神，用户指令（使用 LangGraph）以 opt-in 形式落实。
4. **Phase 0 不写会话记忆**：单发问答，记忆体系由 Phase 1 checkpointer 接管，避免双写。
