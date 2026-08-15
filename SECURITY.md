# 安全审查报告（2026-08）

对 myAgents 全栈（Web 服务、RAG 管线、LLM 客户端、工具层）的安全审查结果。

## 审查范围

| 组件 | 文件 |
| --- | --- |
| Web 服务 | `src/agents/web_server.py`、`scripts/webui.py` |
| 问答管线 | `src/agents/agent.py`、`planner.py`、`executor.py`、`tools.py` |
| 检索 | `retriever.py`、`vector_store.py`、`bm25.py`、`web_search.py` |
| LLM 客户端 | `llm.py` |
| 配置/密钥 | `config.py`、`.env`、`data/runtime.json` |

## 发现的问题与修复状态

| # | 风险 | 严重度 | 状态 |
| --- | --- | --- | --- |
| 1 | `/vendor/` 静态服务**路径遍历**：`rel` 可含 `../` 读取 `scripts/` 外文件（只读，但违反最小权限） | 中 | ✅ 已修复：`resolve()` + 前缀白名单校验 |
| 2 | **Host 头伪造 / DNS rebinding**：无 Host 校验，恶意域名解析到 127.0.0.1 时可能绕过同源策略 | 中 | ✅ 已修复：Host 白名单（127.0.0.1 / localhost / ::1），其他一律 403 |
| 3 | **请求体无上限**：超大 POST 可能耗尽内存 | 中 | ✅ 已修复：Content-Length 上限 10MB（图片 data URL 6MB） |
| 4 | **异常堆栈泄漏**：未捕获异常返回 500 且可能带堆栈 | 低 | ✅ 已修复：全局兜底 `_send_json({"error": "服务器内部错误"}, 500)`，堆栈仅写日志 |
| 5 | **密钥文件权限**：`.env`、`data/runtime.json`（明文 API Key）默认 644 | 低 | ✅ 已修复：chmod 600 |
| 6 | **CSRF 写入**：`POST /api/config`、`/api/kb` 可被恶意网页诱导本机浏览器发起 | 低 | ✅ 缓解：JSON Content-Type 触发 CORS preflight，跨源被浏览器拦截；Host 校验进一步收紧 |
| 7 | API Key 明文回传 | 低 | ✅ 已修复（早前）：仅返回脱敏值 `sk-****xxxx` |
| 8 | 敏感信息入日志 | 低 | ✅ 已修复：日志只记录 Key 掩码、问题前 60 字符 |

## 已确认安全的设计

- ✅ 服务默认仅绑定 `127.0.0.1`（`--host` 参数存在但不推荐开放）
- ✅ LLM 客户端使用标准 TLS 证书校验（无 `verify=False`），Key 仅经请求头传递
- ✅ 工具层 `calculator` 使用 AST 白名单求值，无 `eval/exec`
- ✅ 前端所有用户/LLM 内容经 `esc()` 转义后渲染（XSS 防护），图片仅作 `<img src>`（data URL，不执行脚本）
- ✅ 问答串行化（`threading.Lock`），无共享状态竞态
- ✅ 知识库重建用独立 Agent，失败自动回滚，不破坏运行中索引

## 残余风险（接受项）

| 风险 | 说明 | 缓解建议 |
| --- | --- | --- |
| LLM 提示注入 | 用户输入可引导模型；检索内容可能含注入文本 | 系统提示已约束"仅依据检索片段"，生产环境可加输出过滤 |
| 视觉模型图片泄露 | 上传图片会发送给所配置的视觉模型 API | 仅使用信任的视觉服务商；本机单用户场景可接受 |
| Web 搜索结果可信度 | `web_search` 返回互联网内容 | 答案带来源 URL，用户可核对 |
| 本地无鉴权 | 任何本机进程可调用 API | 单用户工具可接受；如需多用户应加 Token 认证 |
| 明文 Key 存储 | `.env` / `runtime.json` 存明文 | 已限权限 600；可改用系统钥匙串 |

## 建议（生产化时）

1. 若开放局域网访问：加 `Authorization: Bearer <token>` 鉴权 + HTTPS
2. 输出侧幻觉/注入过滤（答案与检索片段的一致性校验）
3. 用系统钥匙串（macOS Keychain）替代明文密钥文件
4. 请求频率限制（防本地恶意脚本刷 API 额度）
