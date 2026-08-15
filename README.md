# myAgents — 自研 Agent 系统（RAG 问答 + 规划层 + 工具层 + 会话记忆 + Web 搜索）

一个从零实现的轻量 Agent 框架，目标是对**现有知识库**（默认：Obsidian 笔记库）做**检索增强问答（RAG）**，并包含完整的 **规划层**、**工具层** 与 **会话记忆**。核心组件全部自研（向量库、BM25、检索融合、规划器、工具注册表、执行器），LLM 通过 DeepSeek（OpenAI 兼容接口）调用，后续可直接对标 Codex 进行评测。

## 架构总览

```
                        ┌──────────────────────────────┐
  用户问题 ────────────▶ │          Agent 门面           │◀── 会话记忆 SessionMemory
                        └──────────────┬───────────────┘       多轮历史 + 追问改写
                                       │
                    ┌──────────────────┴──────────────────┐
                    │              规划层 Planner          │
                    │  将问题分解为可执行步骤（JSON 计划）     │
                    └──────────────────┬──────────────────┘
                                       │ 步骤列表
                    ┌──────────────────┴──────────────────┐
                    │              执行器 Executor          │
                    │   逐步骤调度工具、收集中间结果          │
                    └──────────────────┬──────────────────┘
                                       │ 调用
                    ┌──────────────────┴──────────────────┐
                    │              工具层 Tools             │
                    │  search_knowledge_base / web_search   │
                    │  calculator / get_current_time / …    │
                    └────────────┬──────────────┬──────────┘
                                 │              │
              ┌──────────────────┴───┐    ┌─────┴──────────────┐
              │  检索层 Retriever     │    │  Web 搜索（Bing）    │
              │  向量+BM25 混合召回    │    │  知识库外/时效信息    │
              └──────────────────┬───┘    └────────────────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              │       知识库索引（离线构建）            │
              │  Markdown 解析/清洗 → 分片 → Embedding │
              └─────────────────────────────────────┘
```

问答流程：**问题 →（记忆改写）→ 规划（拆解）→ 执行（工具调用）→ 检索/搜索 → 生成（带引用）→ 写入记忆**。

## 快速开始

### 1. 安装依赖

```bash
git clone <your-repository-url> myAgents
cd myAgents
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> 推荐安装 `sentence-transformers`（本地中文向量模型，质量最好，离线可用）：
> `pip install "sentence-transformers>=3.0"`
> 首次运行自动下载 `BAAI/bge-small-zh-v1.5`（约 100MB）到本地缓存：
> huggingface.co 可直连则正常下载；国内网络自动改用 hf-mirror.com 镜像；
> 下载完成后完全离线运行（不会再有联网校验卡顿）。
> 不安装时自动回退到内置的 TF-IDF 哈希向量（零外部依赖）。

### 2. 配置

复制 `.env.example` 为 `.env` 并填入 DeepSeek API Key：

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY=sk-...
```

模型与路径等参数在 `config.yaml` 中配置（知识库路径、分片参数、检索权重、模型名等）。
默认知识库目录为项目下的 `knowledge_base/`，该目录只需存放 Markdown 文件。

### 3. 构建索引

```bash
python -m scripts.build_index
```

### 4. 提问

```bash
# 单次提问
python -m scripts.ask --question "RAG 的完整流程是什么？"

# 交互式问答
python -m scripts.ask

# 查看详细执行过程（规划、工具调用、检索）
python -m scripts.ask --question "..." --verbose
```

### 多轮对话

交互模式内置会话记忆：追问（如"那它呢？"）会自动结合历史改写为独立查询再检索，回答保持连贯。

```
❯ MCP 是什么？
❯ 它解决什么问题？        ← 自动消解指代，检索"MCP 解决的问题"
❯ 刚才说的三个参与者分别是谁？
❯ /mem                  ← 查看记忆
❯ /reset                ← 清空记忆，开始新会话
```

### Web 前端（推荐）

```bash
python -m scripts.webui          # 启动并自动打开浏览器 http://127.0.0.1:8787
```

浏览器聊天界面，支持：
- 多轮问答 + 参考来源（可展开）+ 耗时/成本展示 + 规划过程
- **⚙ 设置面板**：模型配置（API Key / Base URL / 模型名 / 温度 / Max Tokens）、
  检索配置（top_k / 精排模式 / 候选数 / 多查询），**保存即生效并持久化**
- **知识库管理**：输入任意 Markdown 目录路径 → 一键重建索引（后台任务，失败自动回滚旧索引）

> 配置保存在 `data/runtime.json`，跨重启生效；API Key 前端只显示脱敏值。

## 评测 / 对标 Codex

```bash
python -m benchmark.run_benchmark
```

- 题库：`benchmark/questions.json`（从知识库内容提炼的问答对，含期望来源文件）
- 指标：回答、延迟、Token 用量、成本估算、检索命中率（期望来源是否被召回）
- 结果输出：`benchmark/results.json` + Markdown 表格
- 后续对标：用同一份题库在 Codex 上运行，对比答案质量与命中率（见 `benchmark/README.md`）

## 目录结构

```
myAgents/
├── config.yaml              # 全局配置
├── requirements.txt
├── .env.example             # DeepSeek API Key 模板
├── src/agents/
│   ├── agent.py             # Agent 门面（规划→执行→生成 + 记忆）
│   ├── planner.py           # 规划层（含追问改写 rewrite_query）
│   ├── executor.py          # 执行器
│   ├── tools.py             # 工具层（注册表 + 内置工具）
│   ├── retriever.py         # 两阶段检索（叶子召回→精排→父块扩展）
│   ├── vector_store.py      # 自研向量库（numpy + 持久化）
│   ├── bm25.py              # 自研 BM25（jieba 分词）
│   ├── embeddings.py        # Embedding 后端（本地模型 / TF-IDF 哈希）
│   ├── chunking.py          # 知识库解析 + 父子分块
│   ├── contextual.py        # 上下文增强（Anthropic Contextual Retrieval）
│   ├── reranker.py          # Cross-Encoder 精排（bge/bce-reranker）
│   ├── memory.py            # 会话记忆（多轮历史 + 截断）
│   ├── web_search.py        # Web 搜索（无 Key，Bing 网页版 + 缓存）
│   ├── llm.py               # DeepSeek 客户端（OpenAI 兼容）
│   └── config.py            # 配置加载
├── scripts/
│   ├── build_index.py       # 构建索引（--augment 开启上下文增强）
│   └── ask.py               # CLI 问答（交互模式含 /reset /mem）
├── benchmark/
│   ├── questions.json       # 题库（含改写/意译难例，带章节级期望）
│   ├── run_benchmark.py     # 评测脚本（--retrieval-only 免费快速验证召回）
│   └── README.md            # 对标 Codex 的方法说明
└── tests/
    ├── test_smoke.py        # 核心检索冒烟测试
    └── test_web_store.py    # 会话持久化与工作区隔离测试
```

维护、发布和故障排查流程见 [`MAINTENANCE.md`](MAINTENANCE.md)。

## RAG 准确率优化（大厂策略落地）

| 策略 | 实现 | 配置 |
| --- | --- | --- |
| 父子分块 | 叶子(≤320字)建索引精确召回 → 命中返回父章节完整上下文 | `chunking.leaf_max_chars` |
| 上下文增强 | 索引时 LLM 为每个父块生成语境摘要（Anthropic Contextual Retrieval） | `build_index --augment` |
| RRF 融合 | BM25 + 向量排名按 Reciprocal Rank Fusion 合并，异构分数更鲁棒 | `retrieval.fusion_mode: rrf` |
| 两阶段精排 | 召回 24 候选 → Cross-Encoder（bce-reranker）精排 Top-K | `retrieval.rerank: auto` |
| 噪声来源降权 | 转录/字幕类章节 ×0.5 惩罚（来源质量过滤） | `retrieval.noise_penalty: 0.5` |
| 近重复去重 | 滑窗重叠产生的近似重复父块合并，缓解 Lost-in-the-Middle | 内置 |
| 多查询扩展 | LLM 生成查询变体扩展召回（可选，多 1 次调用/题） | `retrieval.multi_query: true` |

**验证**（`benchmark/run_benchmark.py --retrieval-only`，章节级命中，15 题含 5 道改写难例）：
- 优化前（旧流水线）：章节命中 93.3%
- 优化后（叶子+RRF+上下文增强+降权+CE 精排混合）：**章节命中 100%**（top_k=6），top_k=3 为 93.3%

**端到端问答**（DeepSeek 生成，默认配置）：15/15 关键词命中 100%、来源命中 100%，
单题约 9s、成本约 ¥0.003。

> 说明：`benchmark/questions.json` 的 `expected_sections` 为章节级期望，比"文件级命中"严格得多；
> 含 5 道改写/意译难例（h01-h05），问题不直接匹配文件标题。

## 设计要点

- **规划层**：让 LLM 输出结构化 JSON 计划（reasoning + steps），每步指定工具与参数；计划解析失败时自动退化为单步检索，保证可用性。
- **工具层**：统一 `Tool` 协议（name/description/parameters JSON Schema/func），执行器按步骤调度；工具错误不会中断整个问答。
- **会话记忆**：多轮历史（最多 6 轮）注入规划与生成；追问先用 LLM 改写为独立查询（指代消解），保证检索不丢上下文；`/reset` 一键清空。
- **Web 搜索**：知识库没有答案或需要时效信息时，规划层自动选择 `web_search`（无 Key、Bing 网页版，进程内缓存 5 分钟），结果以 URL 形式加入引用来源。
- **检索层（两阶段）**：叶子级召回（向量 + BM25，RRF 融合，宽候选池 24）→ 精排（Cross-Encoder 或 LLM）→ 父块扩展去重；上下文增强/噪声降权可配置。
- **可观测**：`--verbose` 输出计划、每步工具调用、检索命中片段与分数；操作日志写入 `data/logs/myagents.log`（滚动轮转）。
- **低依赖**：核心仅依赖 `numpy` / `requests` / `jieba`；向量/精排模型为可选增强（离线可用）。
- **安全**：仅绑定本机 + Host 白名单、静态资源路径遍历防护、请求体上限、异常不泄漏堆栈、密钥文件权限 600（详见 `SECURITY.md`）。

## 模块化与框架演进

### 当前分层（已模块化）

```
scripts/            CLI 薄壳（build_index / ask / webui）→ 逻辑在 src/agents/
src/agents/
├── agent.py        编排门面（规划→执行→生成）
├── planner/executor/tools      Agent 三层
├── retriever/vector_store/bm25 检索层
├── embeddings/reranker         模型层（本地/API 可替换）
├── chunking/contextual         知识库层
├── memory/web_search/llm       能力层
└── web_server.py               Web 层（与业务解耦）
benchmark/          评测层（题库 + 指标，对标 Codex）
```

### 可替换框架路径（按需演进，无需重写）

| 现状（零依赖自研） | 演进选项 | 触发条件 |
| --- | --- | --- |
| 自研 numpy 向量库（数千片段够用） | faiss / hnswlib / pgvector / Milvus | 数据量 >10 万片段 |
| 自研 BM25（jieba） | Elasticsearch / OpenSearch | 需要过滤/聚合/多租户 |
| requests 手写 LLM 客户端 | openai SDK / langchain-llm | 需要流式、函数调用原生支持 |
| `http.server` Web 层 | FastAPI + uvicorn | 需要 WebSocket/鉴权/OpenAPI |
| dataclass 配置 | pydantic-settings | 需要 schema 校验/环境分层 |
| 本地 bge 嵌入 | OpenAI/阿里 text-embedding API | 团队统一 API、省本地显存 |

所有替换点都有**统一接口**（`EmbeddingBackend` / `VectorStore` / `Retriever` / `Tool`），换实现不换调用方。

### 新工具接入方式

```python
# tools.py 注册表加一项即可（执行器/规划器自动感知）
Tool(name="my_tool", description="...", parameters={...}, func=my_fn)
```
