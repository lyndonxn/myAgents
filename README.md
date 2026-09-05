# MYAGENTS

> 本地优先的 Markdown 知识库问答 Agent：混合检索、ReAct 工具调用、可恢复后台任务与可选本地 LLM。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Local First](https://img.shields.io/badge/运行方式-本地优先-16A34A)](#安全说明)

<p align="center">
  <a href="assets/myagents-poster.png">
    <img src="assets/myagents-poster.png" alt="MYAGENTS 项目宣传海报" width="760">
  </a>
</p>

MYAGENTS 可以把 Markdown 文档目录变成可检索、可连续对话的知识工作区。它使用向量检索和 BM25 查找内容，可选用交叉编码器精排，并在本地保存工作区、会话、任务、反馈与审计轨迹。当前版本已交付 Agent 核心模块、2026-09 企业安全与离线能力，以及 Next.js Web UI 全量迁移。

项目刻意保持核心轻量：HTTP 服务、会话存储、向量库、BM25 索引、规划器、执行器和模型客户端均未依赖大型应用框架，方便阅读、修改和二次开发。

## 核心能力

- **混合 RAG**：结合向量检索与 BM25，并使用 RRF 完成结果融合。
- **两阶段检索**：叶子块宽召回、可选精排、父块上下文扩展。
- **Agent 工作流**：结构化规划、工具执行、回答生成和引用校验。
- **ReAct 迭代**：步骤输出支持 `@step:N` / `@step:N.field` 传递；失败后可反思并追加计划，受最大步数保护。
- **持久化会话**：使用 SQLite 保存历史记录，重启后可继续对话。
- **会话记忆隔离**：不同会话分别恢复上下文，不会相互串线。
- **多工作区**：切换不同知识库目录及其独立会话列表。
- **本地 Web UI**：Next.js 15 + React 19 静态导出，由 Python 服务同源伺服；支持流式回答、阶段状态、图片问答、语音输入、反馈、引用详情、设置、指标和日志。
- **联网降级**：知识库无法回答时可切换公开网页搜索（默认关闭，未命中时先询问是否联网，`allow_web` 可请求级强禁/强许）。
- **执行可观测**：展示检索轨迹、引用来源、耗时、Token 和成本估算；答案卡片披露降级/联网状态。
- **长期记忆**：跨会话保存问答经验，向量召回相关历史；支持摘要压缩、容量淘汰和实体事实记忆（默认关闭，设置面板可开启）。
- **记忆治理**：设置面板查看/搜索/逐条删除/清空长期记忆与实体事实；清空会话联动删除该会话记忆。
- **任务状态机**：后台任务逐步落盘，支持暂停、恢复、取消和服务重启后的崩溃恢复；看门狗把心跳停滞的任务转为可恢复的暂停。
- **审计轨迹**：操作级事件按天落 `data/audit/`（ask/工具/LLM/管理操作），不记录正文与密钥，`GET /api/audit` 可查询。
- **速度与成本**：简单事实题走快路径（跳过规划与反思）、合成输出预算收紧、证据按查询相关性句子级压缩、prompt 前缀缓存友好。
- **检索迭代预算**：反思可改写查询再检索，`planner.max_search_calls` 防失控。
- **输出校验**：清除超出来源范围的 `[n]` 引用，并在接口和界面中报告有效/无效数量。
- **本地优先**：默认仅监听 `127.0.0.1`；联网降级、长期记忆和实体记忆默认关闭，密钥与运行数据不会进入 Git。

## 2026-09 升级状态

后端 G1–G11 已落地，前端 S1–S8 已完成迁移与切换。最近一次离线验证为 **197 个测试全部通过**（2026-09-06）。详细切片记录、验收 ID 与已知边界见 [`HANDOFF.md`](HANDOFF.md) 和 [`spec/upgrade-2026-09.md`](spec/upgrade-2026-09.md)。

| 批次 | 已交付能力 | 关键入口 |
| --- | --- | --- |
| G1–G2 | 统一测试入口、配置类型/范围/跨字段校验，错误不回显密钥 | `validate_config()`、`ConfigError` |
| G3–G5 | 外发默认关闭、回答状态披露、长期记忆治理、密钥权限 0600、任务写回幂等 | `allow_web` / `remember`、`/api/memory` |
| G6–G7 | 按天 JSONL 审计、任务心跳与步骤/总时长看门狗 | `data/audit/`、`tasks.*` |
| G8 | 10 类难评测集、独立 reward 判分、p50/p95 与失败分布 | `benchmark/questions_hard.json`、`src/agents/reward.py` |
| G9–G10 | 简单事实快路径、证据压缩、成本预算、检索改写循环 | `planner.fast_path`、`planner.max_search_calls` |
| G11 | 本地 OpenAI 兼容端点、空 API Key、运行期禁用联网 | `llm.mode=local`、`scripts/prepare_offline.py` |
| S1–S8 | Next.js 静态导出、会话/流式聊天/答案卡/图片输入/任务面板/设置与收尾 | `frontend/`、`frontend/out/` |

明确延期或不在本项目范围：付费 100 题云端对比（需单独授权）、多 worker/锁拆分、coverage.py 覆盖率、认证/多用户、文档级 ACL、增量索引，以及重写 Agent 框架。

## Web 界面

Web UI 当前支持：

- 工作区创建与切换；
- 多会话管理、单条删除及跨重启恢复；
- 每个会话独立的后端记忆；
- 检索轨迹、引用来源和运行指标；
- 回答复制及“有用 / 没用”反馈；
- 图片上传和可选语音输入；
- 模型、检索、视觉、知识库、工作区和日志设置；
- 今日检索次数和知识库命中率；
- 日志筛选、搜索、自动刷新、下载和清空。
- 实时后端与索引状态，不使用静态占位文案；生成过程通过 NDJSON `stage/meta/delta/done/followups` 事件驱动。

页面由 Python 服务提供，不能用 `file://` 直接打开。默认优先加载 `frontend/out/`；全新克隆若尚未构建静态资源，会回退到 `scripts/webui.legacy.html`。

## 系统架构

```text
Next.js Web UI / CLI
          |
          v
      Python HTTP API  ──────── 审计 JSONL
          |
          v
会话记忆 -> 规划器 -> 执行器 -> 工具
                              |    |
                              |    +-> 联网搜索（可选）
                              v
                         混合检索器
                         /       \
                    向量检索    BM25
                         \       /
                           RRF 融合
                               |
                            可选精排
                               |
                        父块上下文 + LLM
                               |
                    回答 + 引用 + 运行指标
                         /              \
                   SQLite 会话       JSON 任务状态
```

离线索引流程：

```text
Markdown -> 清洗 -> 父块 -> 叶子块 -> 向量化 -> 本地索引
```

## 快速开始

### 环境要求

- Python 3.10 或更高版本
- Node.js 20+ 与 npm（仅从源码构建 Next.js 前端时需要；当前开发环境为 Node 24）
- 云端模式需要一个 OpenAI 兼容 API Key；本地模式可不填 Key
- 一个包含 Markdown 文档的目录

### 安装

```bash
git clone https://github.com/lyndonxn/myAgents.git
cd myAgents

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

构建当前 Web UI（首次克隆或前端源码有变更时执行）：

```bash
cd frontend
npm install
npm run build
cd ..
```

`npm run build` 生成的 `frontend/out/` 是静态产物，已被 Git 忽略。开发调试可先启动 Python 后端，再在另一个终端运行：

```bash
PYTHONPATH=src python -m agents.web_server --port 8787 --no-browser
cd frontend && npm run dev       # http://127.0.0.1:3000，/api 自动代理到 8787
```

可选安装本地向量与重排模型：

```bash
pip install "sentence-transformers>=3.0"
```

如果未安装 `sentence-transformers`，系统会自动降级为内置的 TF-IDF 哈希向量后端。
精排模型缺失时会跳过 Cross-Encoder/LLM 精排，混合召回仍可用；离线环境可用 `retrieval.rerank: off` 明确关闭精排。

### 在页面中配置 API

打开“设置 → 模型”，选择云端或本地模式并填写配置。保存后即可提问，密钥不会在设置接口中回传明文。

云端模式（默认）：

```yaml
llm:
  mode: cloud
  base_url: https://api.deepseek.com
  chat_model: deepseek-chat
```

本地离线模式（以 Ollama 为例）：

```yaml
llm:
  mode: local
  base_url: http://localhost:11434/v1
  chat_model: qwen2.5:7b
  api_key: ""                  # 本地端点允许为空
tools:
  web_search_enabled: false
```

然后运行只读体检（不会下载模型或修改配置）：

```bash
PYTHONPATH=src python scripts/prepare_offline.py --check
# 仅查看准备指引：
PYTHONPATH=src python scripts/prepare_offline.py --prepare
```

离线档位会在运行期强制禁止 Web 搜索；知识库未命中时按未命中路径返回，不会把问题外发。`LLMClient` 与 CLI 支持空 Key；若 Web UI 仍提示 `MODEL_NOT_CONFIGURED`，这是当前 HTTP 层按 Key 判断“已配置”的已知限制。

命令行用户也可以使用 `.env`：

创建本地环境文件：

```bash
cp .env.example .env
```

在 `.env` 中填写：

```dotenv
DEEPSEEK_API_KEY=replace-with-your-api-key
```

将 Markdown 文件放入 `knowledge_base/`，或者修改 `config.yaml` 中的 `kb_path`。

主要配置项：

| 配置段 | 用途 |
| --- | --- |
| `chunking` | 父块、叶子块大小，重叠区间和排除目录 |
| `embedding` | 本地模型或 TF-IDF 向量后端 |
| `retrieval` | Top K、融合方式、精排和多查询检索 |
| `llm` | Base URL、模型、温度、输出限制和超时 |
| `planner` | 规划模型和最大执行步骤数 |
| `tools` | 计算器、时间、主题、联网搜索及 KB→Web 降级 |
| `memory` | 长期记忆、实体记忆、容量与向量后端 |
| `audit` | 审计开关、保留天数、是否记录正文（默认否） |
| `tasks` | 步骤超时、总超时和 watchdog 扫描间隔 |
| `synthesis` | 合成输出上限与句子级证据压缩 |

配置优先级为 `data/runtime.json` > `.env` > `config.yaml`。通过 `/api/config` 保存时会先校验合并后的完整配置；错误响应包含可修复的 dotted 路径，但不会回显 Key。含密钥的 `.env` 与 `data/runtime.json` 应保持 0600 权限。

### 构建索引

```bash
PYTHONPATH=src python -m scripts.build_index
```

可选开启上下文增强：

```bash
PYTHONPATH=src python -m scripts.build_index --augment
```

### 启动 Web 界面

```bash
PYTHONPATH=src python -m agents.web_server --port 8787
```

浏览器访问 [http://127.0.0.1:8787/](http://127.0.0.1:8787/)。也可以使用便捷入口：

```bash
python -m scripts.webui
```

macOS 可直接双击项目根目录的 `启动 myAgents.command`；它会固定绑定 `127.0.0.1:8787` 并打开本地页面。

### 使用命令行

```bash
# 单次提问
python -m scripts.ask --question "什么是 RAG？"

# 交互式对话
python -m scripts.ask

# 显示规划、工具调用和检索详情
python -m scripts.ask --question "解释完整检索流程" --verbose
```

交互模式命令：

- `/mem`：查看当前记忆；
- `/reset`：清空当前记忆；
- `exit`：退出交互模式。

### 后台任务（CLI）

长任务可独立于聊天窗口运行，并与 Web 端共享 `data/tasks/`：

```bash
python -m scripts.task --question "总结知识库中的 RAG 流程"
python -m scripts.task --list
python -m scripts.task --watch <task_id>
python -m scripts.task --resume <task_id>
python -m scripts.task --cancel <task_id>
```

任务状态为 `queued`、`running`、`paused`、`completed`、`failed` 或 `canceled`。每个步骤完成即写入 JSON；进程异常退出后，遗留任务会在下次启动时转为 `paused`，不会重复执行已完成步骤。

### HTTP API 摘要

服务默认监听 `127.0.0.1:8787`。问答请求可带 `session_id`、`allow_web`、`remember`；两个布尔字段只影响本次请求，缺省时使用配置值。本地模式会强制将 `allow_web` 视为 `false`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/status` | 索引、向量后端、记忆、上下文与模型配置状态 |
| `GET` | `/api/workspaces` | 工作区列表与当前工作区 |
| `POST` | `/api/workspaces` | `action=create\|switch`，创建或切换工作区 |
| `GET` | `/api/sessions[/{id}]` | 会话列表或消息历史 |
| `POST` | `/api/sessions` | 创建会话；`action=delete` 删除会话 |
| `POST` | `/api/ask` | 非流式问答，返回答案、来源、计划、指标 |
| `POST` | `/api/ask_stream` | NDJSON 流式问答：`stage/meta/delta/done/followups` |
| `POST` | `/api/ask_image` | 图片描述后检索问答（data URL，约 4MB 上限） |
| `POST` | `/api/tasks` | 创建任务（`question`，可选 `session_id`） |
| `GET` | `/api/tasks` | 按更新时间倒序列出任务 |
| `GET` | `/api/tasks/{id}` | 查看计划、步骤、答案和用量 |
| `POST` | `/api/tasks/{id}/pause\|resume\|cancel` | 控制任务状态 |
| `GET` | `/api/memory`、`/api/memory/entities` | 查看长期记忆或实体事实（支持搜索/会话过滤） |
| `DELETE` | `/api/memory/{id}`、`/api/memory?session_id=` | 删除单条或某会话记忆 |
| `POST` | `/api/memory/clear` | 清空长期记忆与实体事实 |
| `GET` | `/api/audit?date=&type=&limit=` | 查询按天审计事件（limit 1–1000） |
| `GET` | `/api/config`、`POST /api/config` | 读取/校验/保存运行时配置 |
| `GET` | `/api/kb`、`POST /api/kb` | 查看知识库状态或异步重建索引 |
| `GET` | `/api/stats` | 当前工作区查询与命中统计 |
| `GET` | `/api/logs`、`POST /api/logs` | 查看或清空日志 |
| `POST` | `/api/reset` | 清空当前会话消息、短期记忆及关联长期记忆 |

任务完成后会写回对应会话；暂停、失败和取消不会写入聊天消息。

`/api/ask` 与 `/api/ask_stream` 的答案 payload 保持兼容，并新增 `sources_detail`、`metrics.degraded`、`metrics.web_used`、`metrics.citations_valid` 和 `metrics.citations_invalid`。`degraded=true` 表示 KB 检索失败后实际走了 Web 降级；`web_used=true` 表示发生了网页外发，两者会在 UI 和审计中披露。

流式事件顺序通常为：`stage`（规划/检索、生成答案）→ `meta`（会话、来源、计划、指标）→ 多个 `delta` → `done` → 可选 `followups`。模型未配置时返回 HTTP 428 与 `code=MODEL_NOT_CONFIGURED`。

## 检索流程

| 阶段 | 实现方式 |
| --- | --- |
| 文档解析 | 清洗 Markdown、元数据和噪声内容 |
| 父子分块 | 小叶子块用于召回，大父块用于提供完整上下文 |
| 混合召回 | 向量 / TF-IDF 检索与 BM25 关键词检索 |
| 结果融合 | RRF 或可配置的加权融合 |
| 精排 | 可选 Cross-Encoder 或 LLM 重排 |
| 去重 | 合并近似重复的父块上下文 |
| 回答生成 | 组装检索证据并附带引用来源 |

## 数据持久化

Web 会话保存在 `data/webui.sqlite3`，包括：

- 工作区；
- 会话和有序消息；
- 回答反馈；
- 每日查询及知识库命中事件。

其他运行数据：

| 路径 | 内容 | 说明 |
| --- | --- | --- |
| `data/index*`、`data/chunks.json` | 向量/BM25 索引与分块 | 可删除后重新构建 |
| `data/tasks/` | 后台任务 JSON | 每步落盘；崩溃恢复为 `paused` |
| `data/memory/` | 长期记忆与实体事实 | 仅对应开关开启时写入 |
| `data/audit/YYYY-MM-DD.jsonl` | 操作级审计事件 | 追加写入，按 `audit.retention_days` 清理 |
| `data/runtime.json` | 设置面板运行时覆盖 | 可能含 Key，权限应为 0600 |
| `data/logs/` | 应用日志 | 设置面板可筛选、下载、清空 |

删除会话时，该会话的消息、记忆和检索统计会一并删除。删除操作不可恢复。

索引、运行时设置、数据库、日志、评测结果、私人知识库内容和 `.env` 均已通过 `.gitignore` 排除。需要迁移会话时备份 `data/webui.sqlite3`；删除会话/清空记忆不可恢复。

## 测试

测试不依赖在线模型接口，使用 fake LLM、TF-IDF 和临时目录隔离运行数据。当前基线：**197 tests, 5.5s, OK**。

```bash
.venv/bin/python -m py_compile src/agents/*.py tests/*.py benchmark/*.py
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

覆盖范围包括 Markdown 清洗、向量后端、混合检索与精排、ReAct/反思、引用校验、会话与长期记忆治理、配置校验、外发控制、密钥权限、审计、任务状态机与 watchdog、前端静态导出，以及速度/成本和离线档位。

前端单独校验：

```bash
cd frontend
npm run typecheck
npm run build
```

## 评测

```bash
PYTHONPATH=src python -m benchmark.run_benchmark
```

仅评测检索效果，不调用 LLM、不产生模型生成费用：

```bash
PYTHONPATH=src python -m benchmark.run_benchmark --retrieval-only
```

### 使用公开样例库

仓库提供 `samples/kb/`（5 篇原创技术短文）与 `benchmark/questions_sample.json`（10 题），可在不污染用户索引的情况下验证检索：

```bash
PYTHONPATH=src python -m benchmark.run_benchmark --retrieval-only \
  --kb samples/kb --questions benchmark/questions_sample.json
```

难评测集为 `benchmark/questions_hard.json`，共 10 类 × 10 题（事实、多跳、拒答、冲突、提示注入、跨会话记忆、网络不可用等）。报告包含 p50/p95 延迟、prompt/completion token、分题型汇总、失败类型和 `refusal_accuracy`。独立判分函数位于 `src/agents/reward.py`；只有显式传 `--judge` 才会调用 LLM 进行付费证据评审。私人知识库只能通过 `--kb` 临时评测，不应把其标题、关键词或题目提交到样例库。

## 当前边界

- 系统默认本地监听，但云端模式下模型回答会把问题与必要上下文发送到配置的服务；联网降级、长期记忆与实体记忆**默认关闭**，显式开启后才会外发或落盘（`data/`）。需要完全离线时使用 `llm.mode=local`，并通过 `prepare_offline.py --check` 验证。
- 本地模式只保证 MYAGENTS 不主动调用 Web 搜索；本地 LLM、embedding 和 reranker 仍需提前安装/缓存，第三方运行时的网络行为不由本项目控制。
- G11 的空 Key 支持已在 `LLMClient`/CLI 验证；Web API 的 `/api/ask*` 仍以 `llm_api_key` 非空作为配置门槛，使用无鉴权本地端点时需后续单独修正该门槛。
- 任务暂停发生在步骤边界或合成阶段检查点，不保证中断正在进行的单次模型请求；看门狗会把心跳停滞的任务转为 paused（可恢复），执行异常进入 `failed`，应查看任务详情后重试。
- 引用校验只判断 `[n]` 是否落在实际来源编号范围内，不证明内容真的支持该陈述；`citation_hallucination` 是格式/编号指标，不是事实正确率。
- 当前仍是单用户本地服务，无认证、多租户和文档级 ACL；不要直接暴露到公网。

生成的 `benchmark/results.json` 和 `benchmark/results.md` 不会提交到 Git。

## 项目结构

```text
myAgents/
├── config.yaml
├── knowledge_base/          # 本地 Markdown，内容不会提交
├── scripts/                 # CLI、索引构建、离线体检与 Web 启动器
│   ├── prepare_offline.py   # 本地 LLM 离线档位准备/体检
│   └── webui.py              # 启动本地 Python Web 服务
├── frontend/                # Next.js 15 + React 19 前端源码
│   ├── app/                  # App Router 页面与设计系统
│   ├── components/           # 聊天、答案卡、设置、任务与轨迹面板
│   └── out/                  # 静态导出产物（Git 忽略，需 npm run build）
├── src/agents/
│   ├── agent.py             # Agent 编排门面
│   ├── planner.py           # 结构化规划
│   ├── executor.py          # 工具执行
│   ├── tools.py             # 工具注册表
│   ├── retriever.py         # 混合检索管线
│   ├── vector_store.py      # 本地向量索引
│   ├── bm25.py              # 关键词索引
│   ├── embeddings.py        # 本地与 TF-IDF 向量后端
│   ├── reranker.py          # Cross-Encoder / LLM 精排
│   ├── memory.py            # 进程内会话记忆
│   ├── long_memory.py       # 跨会话长期记忆与实体记忆
│   ├── citations.py         # [n] 引用提取与合法性校验
│   ├── task_store.py        # 任务 JSON 持久化
│   ├── task_runner.py       # 后台任务执行、暂停/恢复/取消
│   ├── web_store.py         # SQLite 持久化
│   ├── web_server.py        # 本地 HTTP API
│   ├── web_search.py        # 公开网页搜索降级
│   ├── audit.py             # JSONL 审计记录与查询
│   ├── reward.py            # 离线评测判分与分层汇总
│   └── llm.py               # OpenAI 兼容模型客户端
├── benchmark/               # 检索/问答评测与指标汇总
├── samples/kb/              # 5 篇原创公开样例文档
└── tests/
```

## 安全说明

- Web 服务默认仅监听 `127.0.0.1`；
- 校验 Host 请求头和静态文件路径，阻止路径穿越；
- 限制请求体（10MB）和图片 data URL（约 4MB）；
- 设置接口只返回掩码 Key，`.env` 与 `data/runtime.json` 写入后收紧为 0600；
- 审计默认只记录操作摘要、耗时、token、来源、模型和外发标记，不记录问题/答案正文；
- 外发与持久化开关默认关闭，答案会披露是否使用 Web 降级；
- 密钥、运行数据、索引、日志和私人知识库均被 `.gitignore` 排除。

如需开放到局域网或公网，请先增加身份认证、HTTPS、请求限流，并换用生产级 HTTP 服务。详细威胁模型和残余风险见 [SECURITY.md](SECURITY.md)。

## 常见问题

### 页面提示 `Failed to fetch`

不要直接打开 HTML 文件。地址栏以 `file://` 开头时，页面没有连接本地后端。请使用 `python -m scripts.webui` 启动服务后访问 `http://127.0.0.1:8787/`；若使用前端开发服务器，则访问 `http://127.0.0.1:3000/`。

### 问答正常，但索引显示“后端未连接”

新版页面每 5 秒读取一次 `/api/status`，知识库统计接口失败不会再把整个后端标为断开。确认 `frontend/out/` 已通过 `npm run build` 生成；未生成时服务会自动回退到 `scripts/webui.legacy.html`。

### 启动时停在模型下载

精排模型只从本地目录或缓存加载，不会在桌面启动阶段自动下载。模型不存在时会跳过精排，混合检索仍可使用。

## 维护

本地配置、验证流程、发布检查、数据备份和密钥处理方式见 [MAINTENANCE.md](MAINTENANCE.md)。

## 开源协议

项目暂未选择开源协议。仓库目前可以公开查看，但在添加许可证之前，仍适用默认版权限制。
