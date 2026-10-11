# MCP vault-mcp — 验收规格（spec/09 T-009 对应）

依据：用户设计（知识库《MCP Server 开发实战：FastMCP 从零到部署》第二节"本库的实际机会：vault-mcp"）——三工具：`search_notes` / `get_decisions` / `log_digestion`，FastMCP 风格，本地 stdio。

## 交付物

- `src/agents/mcp_server.py`：`build_mcp_server(agent)` + `python -m agents.mcp_server` 入口（stdio）
- `config.yaml` mcp 节：`mcp.decisions_dir`（默认 `决策记录`，相对 kb_path）、`mcp.digest_log`（默认空 = `data_dir/mcp_digestion.jsonl`）
- `requirements-mcp.txt`：`fastmcp>=2.0`（opt-in；本机 4.1.0）
- `tests/test_mcp_server.py`

## 验收标准（Given/When/Then）

### MCP-ACC-001 工具面
- Given：FastMCP server（内存 client 连接）
- When：`list_tools`
- Then：恰好 `search_notes` / `get_decisions` / `log_digestion` 三个工具，输入 Schema 由类型注解生成。

### MCP-ACC-002 search_notes
- When：`search_notes(query, top_k)`
- Then：结构化 dict：`{query, hit_count, hits:[{rank,file,heading,snippet,score}]}`；结果与 `Retriever.retrieve` 同源同序（C6）。

### MCP-ACC-003 get_decisions / log_digestion（写白名单 C7）
- Then：
  - `get_decisions(limit)` 返回 `决策记录/` 下 md 文件列表（file/title/content，单篇字符上限防 token 失控）；
  - `log_digestion(note_path, summary)`：note_path 解析后必须在 kb_path 内（越界拒绝且零落盘）；写入仅限配置的 digest 文件（JSONL 追加）；每次成功写产生一条审计事件（audit.enabled 时）。

## 边界（如实）

- 传输仅 stdio（本地单机）；无远程/OAuth（设计笔记的"对外发布"前置，不在本轮）。
- server 进程内 Agent 只读索引（检索/读文件），不承载 /api 问答链路。
