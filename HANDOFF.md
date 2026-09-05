# HANDOFF — myAgents Agent 化改造（全部完成 ✅）

分支：`feature/agent-core-modules`（起点 9ee2763）。推送状态：**未 push**（未获授权）。
规格：`spec/agent-core-modules.md`（S 阶段 + T 阶段）。契约：`AGENTS.md`。

## 第一期 S1–S5（五大核心模块补全，已验收）

| 切片 | 内容 | 提交 |
| --- | --- | --- |
| S1 | 工具层强化：schema 校验/重试/KB→Web 降级/JSON 修复轮 | 56ad6bf |
| S2 | ReAct 迭代：反思重规划/@step 占位符传递/预算护栏 | c5c6519 |
| S3 | 引用校验 + 评测指标（幻觉率/完成率/--judge） | 40438d5 |
| S4 | 记忆体系：长期记忆向量库/摘要压缩/遗忘/实体记忆 | cc24700 |
| S5 | 任务状态机：逐步落盘/暂停恢复取消/崩溃恢复 API | 320498e |

## 第二期 T1–T5（遗留事项处理，已验收）

| 切片 | 内容 | 验收 | 提交 |
| --- | --- | --- | --- |
| T1 | 任务答案写回会话（on_complete 回调）+ payload 透出 citations_valid/invalid | ACC-T1-01..03 | 66d6b0f |
| T2 | 任务面板 Web UI（右侧 trace 面板 04 区块：列表/状态徽章/暂停继续取消/步骤详情/发现轮询）+ 引用校验行 + 「后台任务」徽标；浏览器实测通过 | ACC-T2-01..04 | 97d43e2 |
| T3 | CLI 任务模式 `python -m scripts.task`（--question/--list/--watch/--resume/--cancel/--task-store） | ACC-T3-01..03 | b0865e5 |
| T4 | 示例评测库：samples/kb 5 篇原创文档 + questions_sample.json 10 题 + --kb/--questions 内存索引（零落盘） | ACC-T4-01..03 | 3df2e36 |
| T5 | 完整 LLM 评测实跑 + 用量计量修正（按题增量） | 实测 | 39565f9 |

### T 阶段实测发现与修复（浏览器/实跑证据）
1. **任务面板发现死锁**：页面加载时无任务则轮询永不启动，后创建的任务无法被发现 → 改为常驻发现轮询（活跃 2s / 空闲 8s，页面隐藏即停）。97d43e2
2. **任务写回消息缺引用计数**：TaskRecord 未存 citations → TaskRecord 增字段、finish_task 返回 report、写回 metrics 带上，聊天流引用校验行打通。97d43e2
3. **评测总成本重复累计**：Agent.ask 返回会话累计用量，汇总按行求和被放大 ~7 倍 → 按题差值记录。39565f9

### 完整评测结果（benchmark/results.md，gitignored 仅本地）
15 题：关键词命中 100%、来源命中 100%、任务完成率 100%、平均延迟 13.2s、总成本 ¥0.0554、退化率 0%；引用幻觉率 13.0%——全部来自 q10（主题概览题）：`list_knowledge_topics` 工具无来源输出时 LLM 仍编造 [n] 编号，被引用校验如实剔除并计入幻觉率（该指标的教科书案例；若要消除，可给主题工具补来源或让规划器对该类问题跳过引用指令）。

## 验证状态（最终全量回归，全绿）
py_compile 全仓 ✓；八个测试套件（smoke/web_store/tool_hardening/react_loop/citations/long_memory/task_runner/task_cli + benchmark_sample）全部通过。

## 剩余延期项（均已明确，无阻塞）
- 无。原延期项全部落地。可选后续：主题工具来源化（消除 q10 类幻觉）、任务面板创建入口、示例库扩题。

## 风险与注意
- 用户工作区未提交变更（`dist/` 删除、`docs/`、`.zcode/` 未跟踪）保持原样，未纳入任何提交。
- `data/runtime.json` 含真实 API Key（gitignored；建议轮换）。
- results.json/results.md 含私人知识库路径，已由 gitignore 排除，不得提交。

## 下一步动作
如需合入：feature/agent-core-modules → main（合并/PR 需用户授权 push）。

---

# 2026-09 升级（已完成，G1–G11 全部交付）

分支：`feature/upgrade-2026-09`（起点 main @ 9e6791f）。规格：`spec/upgrade-2026-09.md`（11 目标 / 4 批，G1–G11）。控制器 = 根会话；分支会话用子代理承载，均已完成并整合。

## Gate 0 能力清单（一次性，全分支复用）
darwin 25.6.0 arm64；原生 Read/Glob/Grep/Bash/Edit 可用（Windows 脚本不适用）；python=.venv 3.13；本升级无外部连接器需求；付费 LLM benchmark 禁跑（除 ACC-U9-03 待单独授权）。

## 已完成
| 切片 | 内容 | 验收 | 提交 |
| --- | --- | --- | --- |
| G1 | 测试入口标准化（P0-1）：10 个 plain-script 套件 → unittest 可发现，76 用例；直跑兼容保留；统一入口写入 AGENTS.md | 独立验收 ACCEPTED（ACC-P0-1-a..f 全过） | bcb8121(spec) + cfee0d7(tests+AGENTS) |
| G2 | 配置校验（P0-4）：validate_config/ConfigError/normalize_config 落地规则表；load_config 失败抛错不静默启动；POST /api/config 无效值 400（errors 列表，不回显 Key）；归一化布尔字符串与枚举 | 独立验收 ACCEPTED（ACC-P0-4-a..f 全过，含用户 runtime.json 逐字节不变取证） | 85957d5（含 spec 规则表） |
| G3 | 数据外发默认关闭 + 回答状态披露（P0-3 + ACC-U3-01..03）：三开关（kb_fallback_web/long_term_enabled/entities_enabled）默认 false；`ask(remember=, allow_web=)` 请求级许可（None→按配置，False 强禁，True 强许）；runtime.json 显式值优先 + 迁移提示一次/进程；`metrics.degraded/web_used` 透出 /api/ask、任务写回与任务详情；webui 答案卡片状态行 + 设置页外发说明 + 清空会话确认 | 控制器验证全过（15 用例 G3 套件 + 全量 103/103） | 2e9ca34 |
| G4 | 记忆治理（P0-2）：新增长期记忆治理方法（list_episodes 过滤/搜索/截断、delete_episode 单条删除含向量行对齐、delete_by_session 按会话删除、clear 清空）与 EntityMemory.clear；Web API 五端点（GET /api/memory?session_id=&q=&limit=、GET /api/memory/entities、DELETE /api/memory/{id}、DELETE /api/memory?session_id=、POST /api/memory/clear）+ 新增 do_DELETE；/api/reset 联动删除该会话长期记忆（long_term_removed 透出，未开启幂等跳过）；webui 新增「记忆」设置页（搜索/逐条删除/清空全部/实体事实/未开启提示） | 控制器验证全过（9 用例 G4 套件 + 全量 112/112） | 见本次提交 |

验证证据：`unittest discover` 76/76 OK（改造前 0）；直跑 10/10 退出码 0；失败路径非 0（/tmp 验证）；离线（fake LLM/桩，data/ 用户库零写入）；六类覆盖映射完整。

## 观察项（预存行为，非本切片缺陷，待后续切片决定）
1. `tests/test_smoke.py::test_load_chunks` 在 cfg.kb_path 指向真实知识库时会只读访问（有存在性守卫、旧行为原样保留）；如需完全隔离可改 fixture 库。
2. `tests/test_benchmark_sample.py` 依赖本机已缓存的 bge-small-zh-v1.5；冷缓存环境可能触发下载。
3. （G2 验收发现）`/api/config` 保存路径 `llm.api_key` 沿用既有 `str()` 转换，int 型 Key 被转字符串落盘而 `vision.api_key` 会 400——统一口径放到 G5 密钥安全切片。
4. （G2 验收发现）CONFIG_FIELDS 白名单外键在保存路径被静默丢弃，属既有行为。

## 延期决策
- G1 覆盖率指标（≥80%）延期：需 coverage.py，超出轻依赖授权，待用户授权。
- G2 沿用 `{"error", "errors"}` 响应体：P1-5 统一错误码契约挂账后置。

## 收官总览（2026-09-04）

**2026-09 升级收官：G1–G11 全部完成并提交（G11 原延期项经用户 2026-09-05「继续直至全部结束」指令恢复实施）。** 分支 `feature/upgrade-2026-09`（起点 main @ 9e6791f），**未 push、未合入 main（均需用户授权）**。

| 切片 | 内容 | 提交 |
| --- | --- | --- |
| G1 | 测试入口标准化（unittest 76→156 用例） | cfee0d7 |
| G2 | 配置校验（validate_config/400/布尔归一化） | 85957d5 |
| G3 | 数据外发默认关闭 + 回答状态披露（P0-3） | 2e9ca34 |
| G4 | 记忆治理（查看/删除/清空 API + 设置页 + 会话联动） | f46f112 |
| G5 | 密钥安全（权限检查/0600/拒绝保存）+ 任务写回幂等 | 7eb0ab1 |
| G6 | 审计轨迹（按天 JSONL，四层事件，不记正文与 Key） | fb62084 |
| G7 | 任务看门狗（心跳/步骤超时/总超时 → paused 可恢复） | 1b3b352 |
| G8 | 难评测集（100 题×10 类）+ reward 模块 | 19ded63 |
| G9 | 速度成本包（快路径/max_tokens/重排/证据压缩） | c2cb0a1 |
| G10 | 检索迭代循环（改写再检索 + max_search_calls 预算） | ffc7169 |
| G11 | 本地 LLM 离线档位（空 Key 本地端点 / 非 DeepSeek JSON 约束 / allow_web 单点钳制 / prepare_offline 体检） | 882ce00 |
| G3 补遗 | 三开关接入设置面板（CONFIG_FIELDS 白名单 + _config_view 透出 + 检索页/记忆页三下拉） | 4c6e906 |

最终验证：`unittest discover` **179/179 OK**；直跑 20/20 退出码 0；compileall（src+tests+benchmark+scripts）通过；全程离线。

### 延期挂账（均为显式决策，无隐藏风险）
1. **ACC-U9-03 / ACC-U10-03**：付费 100 题实跑对比（延迟/token/reward 前后对比），需用户单独授权后运行 `benchmark/run_benchmark.py --questions benchmark/questions_hard.json`。
2. **P1-2 剩余项**：锁拆分（会话记忆/执行/写回三把锁）与可配置多 worker（单 worker 串行已保证正确性）。
3. **G1 覆盖率指标 ≥80%**：需 coverage.py，超出轻依赖授权。
4. **P1-5 统一错误码契约**、P1-4 证据级引用校验、P1-1 模块解耦：P1 挂账后置。

### 收尾状态
- README 已同步（核心能力/当前边界/升级章节）。
- 用户工作区未提交的私人文件（benchmark 结果、设计文档等）保持未跟踪，未纳入任何提交。
- `data/runtime.json` 含真实 API Key（gitignored；**建议轮换**）。
- G3 默认行为变更：联网降级/长期记忆/实体记忆默认关闭——存量 runtime.json 未显式设置时启动会提示一次。

### 如需重启工作
1. 合入：`feature/upgrade-2026-09` → main（需用户授权 push）。
2. 付费对比评测：授权后先跑改造前基线（main 分支）再跑本分支，对比 p50/p95、token 与 reward。
3. 离线档位启用：设置面板把 llm.mode 改为 local（或 config.yaml），base_url 指向本地 OpenAI 兼容端点（如 Ollama http://localhost:11434/v1），运行 `scripts/prepare_offline.py --check` 体检。

---

## W8 记录（2026-09-05，用户实测反馈三项交互补全）

- **内容**：①证据区收回/展开——折叠改为**仅头部点击切换**（rb-top 加 role=button/aria-expanded/键盘支持，卡片与列表 stopPropagation，点内容不再误折叠）；②删除/清空类操作改**友好确认弹窗**——新增 `components/ConfirmDialog.tsx`（useConfirm hook：Promise 风格 `await confirm({title,message,confirmText,danger})`，danger 红色确认键，Esc/遮罩/取消关闭），替换会话删除/清空会话/记忆删除/清空记忆/日志清空共五处原生 confirm（egress 断言字符串「可同时删除该会话长期记忆」保留）；③起始四个快捷卡片接入点击发送（onQuickAsk → onSend，问题文本与 legacy QUICK_ACTIONS 一致）。**附带**：清理测试产生的空会话（仅删 title=新的会话且 0 消息）。
- **验证**：typecheck/build/197 全绿；8787 冒烟——快捷卡片点击即发送（桩接住）、确认弹窗出现/取消不删/确认删除、折叠双向切换 + 卡片点击不折叠 + 全新加载默认收起、空会话清理后剩 3 个真实会话。
- **涉及文件**：frontend/components/{AnswerCard,Chat,ConfirmDialog(新),SettingsModal}.tsx、frontend/app/page.tsx、frontend/app/globals.css。

## W7 记录（2026-09-05，用户实测反馈三项 UI 精修）

- **内容**：①证据列表改图1 独立卡片式——每来源一张圆角卡（圈号 `.ev-no` + 标题 + 面包屑 + 「」引句框），折叠头改「⌄ N 个引用来源 ⌃」双侧 chevron（展开左旋）；引用跳转选择器随行结构更新为 `[data-idx]`；②详情改右侧抽屉——`.detail-drawer` 420px 右滑入（slide-in 动画）+ 遮罩，标题「检索详情 · DEBUG」，行内容复用 ad-row/ad-badge；③生成阶段改「转圈圈 + 流式输出」（图3）——streaming 相恢复 spinner + 状态文字 + 工具 chip（去除静默线；检索阶段维持原 spinner）。
- **追加（同日用户反馈）**：①检索/生成阶段均去掉扫描线条（spinner-only，此前「二者都在」反馈）；②证据折叠条补 `.show` 类——`.block` 默认 opacity:0，缺 show 导致证据条整体透明（即用户报的「参考来源并没有实现」真根因）；③引用来源整体缩小一号（卡片 9px/12px 内边距、标题 12.5px、圈号 18px、折叠头 6px/12px）。
- **验证**：typecheck/build/197 全绿；8787 桩流+真实历史冒烟：streaming 相 spinner/chip/无扫描条、证据卡渲染（圈号/标题/「」引句）、抽屉右侧锚定 11 行、折叠展开跨轮询持久（受控 details，React 19 重渲染会重置 innerHTML 子树与非受控 details——参考来源折叠必须走受控 JSX）、缩小后样式截图核对。
- **涉及文件**：frontend/components/{AnswerCard,AnswerDetailModal,Chat}.tsx、frontend/app/globals.css。
- **提交**：5b4d36f（W7 三项）、8dc166c（扫描条+show 类）、e9dddcc（缩小一号）。

## W6 切片记录（2026-09-05，前端 Next.js 改写启动；S1 脚手架+管线完成）

- **S8 完成（同日，迁移收尾切换）**：①legacy 退役——`scripts/webui.html` 删除（git 历史 + 标签 `webui-html-final` + 副本 `webui.legacy.html` 三重回溯），服务器 `PAGE` 回退路径改指 `webui.legacy.html`（未构建 out/ 的环境仍可双击启动）；②三个读 webui.html 的静态断言测试迁移到前端源码（egress 两例→AnswerCard/SettingsModal/page 断言渲染条件 `data.metrics?.degraded ?`/`web_used ?` 与三开关 state/payload/回填/label/hint；memory 治理一例→SettingsModal/lib/api 断言接口与确认文案；记忆页补齐 legacy 的「长期记忆与实体记忆默认关闭…」hint）；③轨迹终态修复——setTimeout 链改单个 interval 确定性步进（doneSteps 6/6 验证）；④openSession 同会话重开（延迟刷新）不再重置轨迹/来源/指标。**验证**：typecheck/build 通过、**197/197 OK**（含迁移后断言）；8789 桩流终验：doneSteps 6、来源 2 条、指标保留、答案持久。**切换说明**：用户下次启动（双击启动器或本工具重启）8787 即为 Next.js 版；out/ 已在本地（gitignored），全新克隆需 `cd frontend && npm install && npm run build`。**约束记录**：AGENTS.md「轻依赖」为 Python 口径，Node 工具链系用户明确指令引入（Next.js 改写），运行时仍单进程无新增 Python 依赖。
- **S7 完成（同日）**：设置弹窗六页。新增 `components/SettingsModal.tsx`（模型〔provider/mode/Key 掩码/高级含视觉〕、检索〔预设三档+自定义检测+联网降级+长期/实体记忆开关〕、知识库〔目录/状态/重建轮询〕、工作区创建、记忆〔搜索/列表/逐条删除/清空/实体事实，确认文案与 legacy 一致〕、日志〔行数/级别/关键词/自动刷新 4s/下载/清空〕）；`lib/api.ts` 新增 getConfig/saveConfig/rebuildKb/loadMemory/loadMemoryEntities/deleteEpisode/clearMemoryAll/loadLogs/clearLogs/createWorkspace；`page.tsx` 接 openSettings（TopBar 设置/Rail 管理知识库入口）+ llmConfigured 传递（未配置自动弹模型页+首用提示）。**验证**：typecheck/build/197 全绿；8789 真实后端冒烟——六 tab 渲染、真实配置回填（DeepSeek/云端/掩码 Key）、日志页真实 300 行+过滤、记忆页关键文案（egress/memory 治理测试断言字符串）平移到位。
- **S6 完成（同日）**：右侧面板。`TracePanel.tsx` props 化（轨迹四态 idle/running/done/miss + 装饰性 TRACE_FLOW 脚本、命中来源相关度条、运行指标、后台任务列表〔状态徽章/暂停继续取消/展开详情/时间格式〕）；`lib/api.ts` 新增 loadTasks/loadTaskDetail/postTaskAction/TASK_TERMINAL；`page.tsx` 轨迹动画（runTrace 时序链）、成功后来源+指标填充、openSession 重置、任务轮询（活跃 2s/空闲 8s/隐藏 8s）+ 展开详情（失败可重试）+ 任务操作。**验证**：typecheck/build/197 全绿；8789 冒烟——发送后来源 2 条渲染、指标 2 个来源/2.4s/210 tokens、任务列表真实数据渲染。**已知边界（装饰性）**：回答完成后的延迟会话刷新会把轨迹动画终态偶发重置回 idle（时序竞争，不影响数据）；S8 修。
- **S5 完成（同日）**：composer 行为。`lib/api.ts` 新增 `askImage`（/api/ask_image 非流式）；`Composer.tsx` 接入图片上传（＋→file input）/外部拖入（window 级 dragenter/leave/drop + dragDepth 防抖 + composer 高亮）/52px 预览 + 右上角关闭/语音输入（webkitSpeechRecognition interim 替换输入框，无 API 时降级 no-op）；`page.tsx` 以图搜库分支（有图走 askImage 出完整卡，无图走流式；发送文本为空时显示「图片：文件名」；成功刷新列表+记忆+1，失败错误卡）。**验证**：typecheck/build/197 测试全绿；8789 拖入→预览→发送→卡片（状态条/证据/详情）→×清除全通。**待实测确认**：IAB 自动化环境无 SpeechRecognition API，mic 按钮 disabled/title 读数出现不一致探针（功能有早退保护，安全 no-op）；桌面 Chrome 实测预期正常启用。
- **S4 完成（同日）**：答案卡全家桶。新增 `lib/markdown.ts`（esc/mdCore/answerHtml——引用 chip 转 `data-cite`、末段参考来源收 `<details class="ref-fold">`）、`components/AnswerCard.tsx`（卡头 RESPONSE·NN+三图标 / 两态状态条 / cite-warn·cite-ok·webState 披露行〔字符串与 egress 测试断言一致，S8 迁移断言到本文件〕/ markdown 正文 / 执行轨迹 / 三级证据折叠〔EV_FOLD_OPEN 按 messageId 记忆〕/ 操作行四按钮 / 追问区；cite 与 followup 用容器级事件委托）、`components/AnswerDetailModal.tsx`（Esc+遮罩关闭）。`Chat.tsx` 统一消息模型为 intro/user/stream（历史消息同样走完整答案卡），`page.tsx` 接 handlers（复制/分享/重新生成/反馈〔乐观更新+tick〕/详情/追问即发送）与 RESPONSE·NN 序号标记。**坑**：JSX 字符串 SVG 常量会被转义成纯文本——全部图标渲染点改 `dangerouslySetInnerHTML`（截图抓出）。**验证**：typecheck/build 通过、197 测试全绿；8788 桩流+真实历史双路冒烟：状态条/证据展开/引用跳转 flash/详情弹窗字段/反馈高亮回读/追问渲染与点击发送全通；markdown 真实渲染。
- **S3 完成（同日）**：流式聊天。`lib/api.ts` 新增 `askStream`（NDJSON 读取器：stage/meta/delta/done/followups 事件 + MODEL_NOT_CONFIGURED 错误类）；`Chat.tsx` MsgVM 扩展 `agent-stream` 三相（thinking spinner → streaming 静默行+打字机光标 → done），滚动跟随（用户上滚停止跟随，W3 对齐）；`Composer.tsx` 转受控（Enter 发送、132px 限高自适应、↑/■ 两态）；`page.tsx` 队列式播放器（30ms/4 字符）+ 中断/错误路径（abort 卡/请求失败卡、真失败不刷新列表、成功后仅刷新会话列表标题不换消息——Next 版消息存 React state，无 legacy 交换问题）+ busy 守卫（生成中禁切会话/删除/重置/切工作区）。**验证**：typecheck 0 错、build 通过、197 测试全绿；8788 桩流冒烟：三相截图正确、stop→「思考已停止」卡（桩需接 abort 信号——真实 fetch 自动拒绝 pending read）、记忆计数 +1。已知边界：历史/完成卡暂为纯文本（`**` 记号原样），markdown 渲染与答案卡在 S4；followups 数据已入 state、UI 在 S4。

- **S2 完成（同日）**：会话/工作区状态层。新增 `lib/api.ts`（fetch 封装，与 legacy 1:1）与 `components/{TopBar,Rail,Chat,Composer,TracePanel}.tsx`；`page.tsx` 客户端根：workspaces→sessions→首个会话初始化、会话切换/删除（confirm）/重置/新建（含 ⌘K）、工作区切换（失败回退）、状态轮询 5s（索引灯/记忆轮数/知识库概览四格/上下文竖轨百分比）+ 心跳 3s、toast、主题切换（localStorage 持久化）。消息区为 S2 简化渲染（INTRO+纯文本卡），S4 换完整答案卡；composer 发送/上传/语音为禁用壳（S5 接入）；设置/知识库管理入口暂 toast 提示（S7）。**验证**：typecheck 0 错；`next build` 导出成功；8788 真实后端端到端——13 个真实会话加载、激活「agent定义」消息渲染、状态灯 已连接·已同步、docCount 142/chunkCount 636/命中率 88.9%、会话切换（什么是skill 5问+6卡）全通；截图核对。

- **决策（用户拍板）**：前端从单文件 HTML 迁移到 **Next.js（App Router）静态导出**，由现有 Python 服务器伺服（本地优先单进程不变，API 同源零跨域，后端零协议改动）。**备份**：`scripts/webui.legacy.html`（逐字节副本）+ git 标签 `webui-html-final`（cebe790）。工具链：Node v24.19.0 / npm 11.17.0。
- **目录与管线**：新增 `frontend/`（Next 15 + React 19 + TS loose）；`npm run dev` = 3000 端口 + `/api` 代理到 8787（NEXT_DEV_PROXY=1，浏览器视角同源，后端零 CORS 改动）；`npm run build` = `output:'export'` 产出 `frontend/out/`；`frontend/{node_modules,.next,out,next-env.d.ts}` 已 gitignore，out/ 本地构建后长期有效。
- **服务器（S1）**：`web_server.py` 新增 `EXPORT_DIR` 与 `Handler._static_export()`——`/`→out/index.html、`/_next/*` 等静态资源按 MIME 伺服（resolve + relative_to 防穿越，文本补 charset）；out/ 缺失自动回退 legacy `scripts/webui.html`（双击启动在未构建环境仍可用）。
- **S1 验证证据**：新 test_frontend_export 6 用例（根路径映射/嵌套资源 MIME/穿越拒绝/未知文件回 None/缺目录回退），全量 **197/197 OK**；`npm run build` 导出成功（首载 JS 103kB）；8788 实起 Python：`/` 200 text/html、`/_next/static/chunks/*.js` 200 text/javascript；浏览器截图三栏 chrome 与 legacy 视觉一致（设计 tokens 全量平移至 `frontend/app/globals.css`）。
- **S1 已知边界**：Next 壳为静态 chrome（欢迎页/空会话/idle trace），全部交互仍在 legacy 页——后续切片 S2 会话/工作区 → S3 流式聊天 → S4 答案卡 → S5 composer → S6 任务/trace 面板 → S7 设置弹窗 → S8 收尾（迁移 webui 静态断言测试到前端源码、决定 legacy 文件去留）。**切换完成前 8787 行为不变**（未装 out 的环境自动 legacy）。
- **涉及文件**：新增 frontend/{package.json,next.config.mjs,tsconfig.json,app/layout.tsx,app/page.tsx,app/globals.css}、tests/test_frontend_export.py；修改 src/agents/web_server.py、.gitignore、scripts/webui.legacy.html（备份副本）。

---

## W5 切片记录（2026-09-05，composer 上下结构 + 左侧上下文竖轨 + 圆形图标发送键）

- **追加（同日用户反馈）**：生成阶段不再用 spinner/扫描动画——`liveAnswerHTML` streaming 分支改 `.gen-status` 静默状态行（11.5px 小字「正在生成答案 · 引用 N 段内容」+ 1px 细线，无任何动画）；检索阶段（无正文时）保留 `.retrieving` spinner。冒烟：检索态有 spinner、生成态 noSpinner=true + 光标在。
- **背景**：用户三项目标：①「图片」上传改图1 的上下结构（textarea 整行 + 底部操作行）并约束输入区最大宽度；②上下文表移到页面左侧竖排（图2 左缘样式）；③发送键改圆形图标（↑/■ 两态）。
- **验证证据**：`unittest discover` 191/191 OK；node --check 通过；浏览器冒烟：composerWidth=760（与聊天列同宽约束）、sendArrow=true、竖轨 24 段、12% → 3 段绿 rgb(34,180,46)、93% → 22 段红、明暗 token 自适应；视觉验收由用户实测确认。
- **实现（仅 scripts/webui.html）**：
  - composer 重排：`.inputrow` 改纵向（textarea 整行 + `.input-actions` 底部行）；「图片」文字按钮改为左下 `＋` 圆形按钮（.addbtn，hover 蓝调，仍走 imgBtn/imgFile 与拖入同管道）；语音钮与发送键移至右侧；`.composer-inner` 保持 760px 上限（与 chat-inner 一致）。
  - 发送键：34px 圆形，`syncSendBtn` 换 SVG 两态（↑ 发送 / 红底 ■ 停止），aria-label/title 保留；删除旧 `.filebtn`/`.stop-square` 样式。
  - 上下文竖轨：`#ctxLine` 从 composer 移入 `.chat`（`.ctx-rail`，absolute left:10px 垂直居中，24 段纵向，配色逻辑不变 HSL 绿→红），`.chat` 加 position:relative；≤840px 隐藏。
- **涉及文件**：scripts/webui.html。

---

## W4 切片记录（2026-09-05，输入区/状态条/图片预览/上下文表 四项视觉与交互打磨）

- **背景**：用户实测反馈四项：①长文本输入撑爆视觉（图1）；②状态条字体突兀（对齐原型图2）；③图片预览改小图+右上角关闭、支持外部拖入；④上下文条改分段样式（图4）并按用量绿→红着色（用户明确要求色彩渐变，HSL 插值属运行时色，例外于"仅既有 tokens"口径）。
- **验证证据**：`unittest discover` 191/191 OK；node --check 通过；浏览器冒烟（合成 DragEvent drop + DataTransfer）：长文本 inputH=132 钳制且内部滚动；上下文表 10% → rgb(34,180,54) 绿、95% → 23/24 段 rgb(180,51,34) 红；拖入图片 dragenter 高亮 + 预览小图出现 + 缩略图就绪；× 关闭后预览隐藏、pendingImage 清空；明暗双主题 token 自适应。视觉验收由用户实测确认（用户驱动的快速视觉迭代环）。
- **实现（仅 scripts/webui.html）**：
  - 输入框：CSS `max-height:132px; overflow-y:auto`，JS input/quickAsk 两路径同步钳制高度。
  - 状态条排版：基准 12px/`--ink-2`、模式 12.5px/600/`--ink`（warn 橙）、工具 chip 10.5px mono 加底色——对齐原型图2 的字号灰阶层次。
  - 图片预览：52px 小图（去文件名），`.img-clear` 右上角悬浮圆形关闭钮（hover 红）；文件选择与拖入共用 `setPendingImageFile`；window 级 dragenter/dragover/dragleave/drop 监听（dragDepth 计数防抖，drop 取首个 image/* 文件，busy 时忽略），composer `.dragover` 蓝色高亮。
  - 上下文表：24 段 flex 分段条（.seg），`updateContext` 按 pct 点亮段数并 HSL 插值着色（142°绿→0°红），title 显示 K/百分比；移除 ctxFill。
- **涉及文件**：scripts/webui.html。

---

## W3 切片记录（2026-09-05，用户实测反馈修复 + 答案卡严格还原图1/图2）

- **背景**：用户实测 W1/W2 后报三类问题：①最严重——回答完成后答案消失；②卡头未按原型实现（RESPONSE·01 + 图标操作）；③操作行/追问区缺失，要求严格还原图1（卡头+状态条）图2（操作行+追问）。追问数据源经用户拍板：**LLM 生成**。
- **验证证据**：`unittest discover` **191/191 OK**（W2 后 188 → 新增 test_followups 3 用例）；node --check 通过；独立验收子代理 **ACCEPTED（ACC-W3-01..06 全 PASS，Standards PASS）**；浏览器冒烟（假 NDJSON 桩 + 真行为历史桩）：第一问完成→延迟会话交换后答案保留、追问区保留，追问点击→第二轮流式生成且第一轮完整（RESPONSE·01/02 递增），明/暗双主题截图核对；全程离线（测试零网络）。
- **答案消失根因与修复（ACC-W3-01）**：旧 `loadSessions()` 重建 SESSIONS 时把 messages 重置回 `[INTRO]`，而 W1 播放器收尾 `renderMessages()` 发生在其之后 → 读到空会话（该缺陷 W1 冒烟时被假桩的空历史掩盖，误判为桩伪影——教训：桩必须仿真后端真实时序）。修复：①重建列表按 id 保留内存 messages；②`loadSessions` 加 busy 守卫；③onSend 的会话刷新移到 finally 延迟 50ms（busy=false 后，openSession 以已落库历史重渲染）；④openSession 交换历史时按消息 id 继承 followups 等内存态；⑤ask 真失败（非中断）跳过延迟刷新（验收发现①，避免错误提示被未落库空历史冲掉；中断场景答案已落库仍刷新换回完整回答）；⑥openSession fetch 返回后补 busy 检查（验收发现②，封掉替换吞新消息的窗口）。
- **严格还原（图1/图2，ACC-W3-02..04）**：卡头 `RESPONSE · NN`（renderMessages 预扫序号，INTRO 不计入）+ 复制/重新生成/分享图标（真实函数：copyAnswer / regenerateAnswer=以原问题重新提问追加 / shareAnswer=navigator.share 失败兜底复制）；状态条改为 模式图标+文字（📖知识库 / 🌐联网(amber) / 💬模型回答）· 工具 chip · 命中 N 段 · 耗时 · 成本 · 右侧☀详情（去掉 W1 的徽章胶囊与来源名，信息在证据卡）；操作行改为边框按钮组 复制答案/有帮助/不满意 + 右侧重新生成（feedback .on 回显）；虚线分隔 + 追问 ↓ + → 前缀全宽按钮（data-q + chatInner 事件委托 → quickAsk）。颜色全部沿用既有 tokens；`webState/citeLine` 测试断言字符串原样保留。
- **追问 LLM 生成（ACC-W3-05）**：`Agent.suggest_followups(question, answer_text, count=2)`——独立 LLMClient 实例（不污染 ask 的 llm_calls/token 计数，审计照记属合理）、`chat_json` 强约束输出、questions 非列表/异常/未配置一律回空、逐项 ≤60 字截断；`/api/ask_stream` 在 **done 之后** 发 `followups` 事件（不拖慢正文与操作行呈现，仅 answer 无错误时）；前端 done 后收到事件写入 msg 并按 messageId findAnswer 同步交换后对象，追问区随卡片渲染，无数据即隐藏。已知边界：followups 不落库——同会话内经 openSession 继承保留，页面重载后不显示（后续可选：写回 metrics 持久化）。
- **顺带修复**：用户气泡暗色不可读（`color:#F8FAFC` 硬编码 → `var(--panel)` 随主题翻转，W1 之前既有缺陷）。
- **验收子代理非阻塞发现（已处理/知悉）**：①错误路径刷新冲掉提示 → 已修（streamFailed 跳过）；②延迟刷新 fetch 窗口竞态 → 已修（openSession 二次 busy 检查）；③followups 独立客户端产生 llm_call 审计事件 → 知悉（审计全量 LLM 调用口径正确，answer 计数不受影响）；④提示词中 runInPipeline 为笔误，无实际问题。
- **涉及文件**：src/agents/{agent,web_server}.py、scripts/webui.html、tests/test_followups.py（新增）。

---

## W2 切片记录（2026-09-05，用户驱动前端改版续篇：结构化证据卡 + 真阶段事件）

- **背景**：W1 的后续切片（用户「继续」确认）：demo 证据卡的三级结构（标题/路径/引句）需要真实数据源；「检索→生成」两段状态需要后端真事件而非前端假切换。
- **验证证据**：`unittest discover` 188/188 OK（W1 后 179 → 新增 test_sources_detail_stage 9 用例）；compileall 通过；node --check 通过；全程离线（ScriptedLLM + TF-IDF + 临时 KB + 假 NDJSON fetch 桩浏览器实测：两段 stage 状态切换、打字机、富证据卡、历史回放）。
- **实现**：
  - `tools.py`：`tool_search_knowledge_base` 输出新增 `sources_detail`（与 sources 下标一一对应、同去重）；新增 `_hit_detail`（title=标题路径末段（无标题回退文件名）/path=库内相对路径/heading=完整标题路径/snippet=叶子命中文本折叠空白截 120 字）。工具详情不进 LLM 提示词，仅透出前端。
  - `agent.py`：`Answer.sources_detail`（默认空表，旧字段不变）；`_collect_source_info` 重构 `_collect_sources`（跨步去重语义不变，详情缺失回填最小条目保证下标对齐）；`ask(..., on_stage=None)` 可选回调——执行前发「检索知识库」、合成前发「生成答案」；`_emit_stage` 模块级助手吞回调异常（前端断连不影响生成）。任务路径（finish_task）暂不产出详情（写入端 add_task_result 不带 detail，读出空表回退单行）。
  - `web_server.py`：`_answer_payload` 顶层新增 `sources_detail` 键（旧对象 getattr 回退空表）；`/api/ask_stream` 传 `on_stage=emit_stage`（首条「规划与检索」覆盖 ask 启动前空窗），meta 事件带 `sources_detail`；`_persist_answer` 落库带详情。
  - `web_store.py`：messages 表迁移新增 `sources_detail` 列（沿用 sessions.summary 的 ALTER TABLE 模式，旧库打开即补列）；`add_message` 可选参数、`messages()` 读出 JSON。
  - `webui.html`：证据折叠渲染三级富卡片（标题 12.5px/mono 路径面包屑 path › heading/左边框引句，分隔线，仅既有 tokens）；status 条 st-doc 有详情时显示标题替代原始 `文件#标题` 串；meta/历史加载/ask_image 三路径透传 `sourcesDetail`。
  - 测试：新增 tests/test_sources_detail_stage.py（工具对齐去重/引句折叠截断/E2E on_stage 顺序与详情组装/缺省 None/回调异常兜底/payload 键与旧对象回退/store 往返与旧库迁移）；`test_task_runner` ACC-T1-03 顶层键集合断言按加法扩展更新（W1 注释注明），metrics 键集合断言不变。
- **兼容性**：/api 契约只增不改（payload/meta/sessions 消息各加一个键）；ask 签名新参带默认值；旧库自动迁移；无详情来源回退单行渲染。
- **涉及文件**：src/agents/{tools,agent,web_server,web_store}.py、scripts/webui.html、tests/{test_sources_detail_stage(新),test_task_runner}.py。

---

## W1 切片记录（2026-09-05，用户驱动前端改版：答案卡对齐设计原型）

- **背景**：用户提供设计原型 demo（桌面 redesign-mockup-final.html，含「重播生成过程」演示），确认改造 webui 答案卡；用户约束——**颜色一律沿用原页面既有 tokens（不引入 demo 黑白配色）**、重播按钮与自动播放仅演示不入生产、三栏布局与其余功能不动。已先在浏览器实跑 demo 学习其时间轴（检索态→生成态→完成态、引用跳转、详情抽屉），并核对其缺陷（检索期 opacity 占位留白、`.evidence-item.no` 选择器失效、生成态残留工具 chip、#fef9c3 高亮无暗色适配）——均未带入实现。
- **验证证据**：`unittest discover` 179/179 OK（test_egress_defaults/test_memory_governance 对 webui.html 的静态断言原样通过）；node --check（抽取 &lt;script&gt;）通过；全程离线——假 NDJSON fetch 桩模拟 stage/meta/delta/done 完整流，浏览器实测明/暗双主题下的检索态、打字机态（状态框+正文+光标）、完成态、引用 [n] 跳转高亮、详情弹窗与关闭。
- **实现（仅 scripts/webui.html，后端零改动）**：
  - 答案卡 5 层：L1 两态状态条（「正在规划与检索」→「正在生成答案 · 引用 N 段内容」+打字机正文与光标 → 完成徽章：模式徽章/首个工具 chip/首个来源/命中 N 段/耗时/成本/详情按钮）；L2 证据折叠（默认收起、按 messageId 记忆开合、生成中隐藏）；L3 操作行（复制/有用/没用+时间戳，生成中隐藏防误点半成品）；L5 回答详情弹窗（字段全部来自 plan/metrics 真实数据：模式/是否联网/检索路径/规划轮次/工具步骤/引用校验/命中/耗时/Token/成本/消息 ID；Esc+遮罩关闭）。
  - 队列式流渲染：delta 进缓冲、30ms/4 字符打字机节奏，仅增量替换当前流式卡片（data-stream 锚点），替代原先每 delta 全量 renderMessages；用户上滚不强制跟随；中断/出错停表清态（AbortError 语义不变）。
  - 事件消费增强：stage 事件驱动检索文案；meta 到达即切生成态（此时 sources/metrics 已知，与后端 NDJSON 顺序 stage→meta→delta→done 对齐）；done 错误路径保持原语义。
  - [n] 引用 chip 化：_mdHtml 将 [数字] 转为可点击 .cite（沿用既有蓝色上标样式+hover 底色），点击展开证据折叠并滚动高亮对应来源行（--blue-soft 闪烁，双主题自适应）。
  - 移除旧「RETRIEVING 独立占位气泡」与 thinking-state 用法；测试断言的 webState/cite-line 字符串与设置页文案原样保留。
- **兼容性**：/api 契约不变；用户气泡、INTRO、快速操作、右侧 trace 面板、会话/工作区/记忆/日志/设置未动。demo 中无真实数据源的字段未采纳（缓存命中、快慢路径、追问推荐、面包屑+引句来源卡）——后续可选项：后端 sources_detail 结构化来源、Agent.ask 可选 on_stage 回调实现真「检索→生成」两段事件。
- **涉及文件**：scripts/webui.html（答案卡 CSS（仅既有变量）、回答详情 modal、liveAnswerHTML/onSend 重写、_mdHtml 引用 chip）。

---

## G11 切片记录（2026-09-05，控制器续接；实现子代理被取消后遗留的完整工作区改动经控制器核验 + 独立验收子代理确认）

- **验证证据**：`unittest discover` 173/173 OK（G10 后 156 → 新增 test_offline_profile 17 用例）；直跑 20/20 退出码 0；compileall（src+tests+benchmark+scripts）通过；全程离线（ScriptedLLM + TF-IDF 后端 + tempfile，用户 data/runtime.json 与 .env mtime 前后不变）。
- **实现**：
  - `llm.mode: cloud | local`（默认 cloud 零行为变化）：枚举校验规则 + CONFIG_FIELDS 白名单 + 设置页「运行模式」下拉与离线说明；
  - ACC-U11-01：LLMClient local 档允许空 API Key（请求头省略 Authorization），cloud 档保持原报错；local 有 Key 照常带头；
  - 非 DeepSeek 模型 JSON 约束：chat_json 向 messages 副本末尾追加 `_JSON_ONLY_SYSTEM_HINT`（调用方列表不被修改），修复轮机制不变，deepseek 仍走 response_format；
  - ACC-U11-02：`resolve_allow_web` 单点钳制——local 档 allow_web 一律 False（ask / plan_only / task_runner 三路径共用该单点），钳制提示一次/进程；KB 未命中走既有披露路径、零网络调用；
  - ACC-U11-03：离线 E2E（ScriptedLLM + TF-IDF + 临时 KB）全链路绿、引用/来源正常、零 requests 调用；
  - `scripts/prepare_offline.py`：--check 只读体检（llm.mode/base_url/web 开关/embedding+reranker 缓存可离线加载）给中文修复建议；--prepare 仅打印指引，不做真实下载。
- **独立验收**：ACCEPTED（ACC-U11-a..f 全过，Spec/Standards 双 PASS，含 data/runtime.json 与 .env mtime 前后一致取证）。
- **涉及文件**：src/agents/{llm,agent,config,web_server}.py、config.yaml、scripts/{webui.html,prepare_offline.py}、tests/test_offline_profile.py（新增 17 用例）。

---

## G10 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 156/156 OK（G9 后 152 → 新增 test_search_loop 4 用例）；直跑 18/18 退出码 0；compileall 通过；全程离线（脚本化 LLM + 查询感知假 KB 工具）。
- **实现**（复用 S2 反思机制与 @step:N 占位符，未新增执行路径）：
  - `build_reflect_prompt/reflect` 新增 `search_budget=(已用, 上限)` 注入：有剩余 → 允许改写 query 再检索（要求 query 与已执行的明显不同；KB 本身无内容则不提检索补步）；耗尽 → 明确禁止任何检索补步。
  - `_reflect_and_extend`：记账 `searches_used`（含反思补步追加后的搜索）并传预算；硬约束——反思提出的新检索步骤超出 `planner.max_search_calls` 时被剔除（其余类型补步不受影响），剔除计数落日志。
  - 配置：`planner.max_search_calls` 默认 3（校验 1–10），config.yaml 注释说明。
- **ACC-U10-01** ✓：首轮未命中 → 反思改写 query 二轮命中，总搜索 2 ≤ 3，来源来自二轮，反思 prompt 含预算。
- **ACC-U10-02** ✓：预算耗尽 → 反思 prompt 明确禁检 + 硬约束剔除第 4 次检索，合成提示按未命中路径披露。
- **ACC-U10-03**（难评测集 reward 对比）需付费 LLM 实跑，按规挂起待单独授权；机制已就绪，可在授权后用 `--questions benchmark/questions_hard.json` 实跑对比。
- **涉及文件**：src/agents/{planner,agent,config}.py、config.yaml、tests/test_search_loop.py（新增 4 用例）。

---

## G9 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 152/152 OK（G8 后 143 → 新增 test_speed_cost 9 用例）；直跑 17/17 退出码 0；compileall 通过；全程离线。
- **快路径（ACC-U9-01/02 ✓）**：`_is_simple_question` 启发式门控（短 ≤60 字符、单问句、无多跳线索表）+ 无会话历史 → 跳过规划与反思，直接单步检索→合成（0 次规划调用）；多跳/对比题与追问场景不受影响；`planner.fast_path` 配置开关。简单题 LLM 调用从 3 降为 1。
- **合成优化**：`synthesis.max_tokens`（默认 1024，原用全局 llm.max_tokens）传至合成调用；`_synthesize` prompt 重排——稳定前缀（指令+证据上下文）在前、规划摘要/对话历史/问题在后（DeepSeek 前缀缓存友好）。
- **句子级证据压缩**：`compress_evidence(text, query)`（jieba 提取查询词，按句相关性保留，目标 ≤70% 长度；过短/无相关句/全相关守卫原样返回）；`synthesis.evidence_compression` 开关默认开。实测构造样本 396→140 字符（降 65%）。ACC-U9-03（100 题实跑对比）按规需单独授权，未执行。
- **配置**：config.yaml 新增 `planner.{fast_path,fast_path_max_len}` 与 `synthesis.{max_tokens,evidence_compression}` + 校验规则 + 设置白名单（synthesis 节）。
- **既有测试适配**：考察完整规划路径的测试（egress/long_memory/react_loop/citations/audit）统一注入 `planner.fast_path=False`（深合并保留各用例覆盖）。
- **涉及文件**：src/agents/{agent,config}.py、config.yaml、src/agents/web_server.py（白名单）、tests/{test_speed_cost(新),test_egress_defaults,test_long_memory,test_react_loop,test_citations,test_audit}.py。

---

## G8 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 143/143 OK（G7 后 135 → 新增 test_reward 8 用例）；直跑 17/17 退出码 0；compileall（src+benchmark）通过；全程零 LLM（reward 纯离线判分、检索跑 samples/kb 内存索引）。
- **reward 模块**（新增 `src/agents/reward.py`，benchmark 与离线档位共用）：
  - `score_case(question, answer, sources, expectations) -> RewardResult(score, failure_type, detail)`；十类题型口径：fact/paraphrase/multi_hop/tool_choice/long_context/memory（关键词+来源比例）、refusal/offline（`refusal` 期望：确定性拒答措辞得分、编造带来源答案记 `fabricated` 扣 0 分——ACC-U8-02）、injection（出现 `forbidden_keywords` 即 `injection_followed`）、conflict（关键词命中但非权威来源 → `stale_source` 0.3）。
  - `percentile(values, p)`（线性插值 p50/p95）；`summarize_by_category(results)`（分题型 count/avg_score/pass_rate/p50/p95/失败类型分布 + 总 `refusal_accuracy`）；`refusal_like`。
- **难评测题库**（新增 `benchmark/questions_hard.json`，100 题 = 10 类 × 10 题）：fact/paraphrase/multi_hop/tool_choice/long_context 五类检索可验证（指向 samples/kb 文档与章节）；refusal/conflict/injection/memory/offline 五类行为题（reward 字段：refusal/forbidden_keywords 等）。
- **run_benchmark.py 接入**：`run_retrieval_only` 每题计检索耗时，报告 p50/p95 延迟与分题型命中汇总（reward 检索口径 score=(src+sec)/2）；rows 行尾新增 latency（6 元组，test_citations S3-03 已同步）；全量 run 的 summarize_results 新增 p50/p95/by_category，每题记录 category 与 reward 判分。
- **ACC-U8-01** ✓：--retrieval-only 跑 questions_hard.json（samples/kb，top_k=6）——检索类 5 类 50 题全绿（pass 100%），平均文件命中 99.5%，p50=11ms/p95=32ms，分题型汇总齐备。**ACC-U8-03** ✓：全程无 LLM 客户端（agent.llm is None 断言）、零 token、零费用。
- **涉及文件**：新增 src/agents/reward.py、benchmark/questions_hard.json、tests/test_reward.py；修改 benchmark/run_benchmark.py、tests/test_citations.py（解包适配）。

---

## G7 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 135/135 OK（G6 后 128 → 新增 test_task_watchdog 7 用例）；直跑 16/16 退出码 0；compileall 通过；全程离线（tempfile 任务库 + stub agent，mock 时间钟验证总超时）。
- **P1-2 落实项**：
  - TaskRecord 新增 `heartbeat_at`/`current_step`/`last_error_type`（from_dict 容错兼容旧文件）；每步骤边界刷新心跳与步骤摘要并随 store.update 落盘；失败步骤记错误类型、成功清空。
  - watchdog：`TaskRunner.sweep_once()`（running 且心跳停滞 > step_timeout_s → paused，error 含「看门狗」可读原因；活跃任务不误扫）+ `start_watchdog/stop_watchdog` daemon 扫描线程；心跳缺失回退 updated_at（升级兼容）。
  - 任务级总超时：_execute 步骤循环预检 elapsed > total_timeout_s → 停止推进、转 paused、error 含「总超时」。
  - 步骤循环新增外部迁移预检：落盘状态非 running/queued → 停止推进且不覆盖状态（watchdog/pause/cancel 与 worker 不互踩）。
  - 状态机：canceled 不可 resume、非法迁移 ValueError 含当前状态（既有实现，本轮补测试锁定）。
  - 配置：config.yaml 新增 `tasks.{step_timeout_s:600, total_timeout_s:3600, watchdog_interval_s:30}` + 校验规则 + 设置白名单；web_server.main 装配 start_watchdog。
- **延期决策（P1-2 剩余项，挂账）**：锁拆分（会话记忆/执行/写回三把锁）与可配置多 worker——当前单 worker + Handler.lock 串行已保证「记忆不串线/问答与任务不并发改记忆」，并行执行收益低且重构风险高；待任务量实测瓶颈后再做。
- **涉及文件**：src/agents/{task_store,task_runner}.py、config.yaml、src/agents/{config,web_server}.py、tests/test_task_watchdog.py（新增 7 用例）。

---

## G6 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 128/128 OK（G5 后 121 → 新增 test_audit 7 用例）；直跑 15/15 退出码 0；compileall 通过；全程离线（审计目录 tempfile，绝不写用户 data/audit/）。
- **模块**：新增 `src/agents/audit.py`——`AuditLogger`（JSONL 按天分文件只追加、retention_days 惰性清理、log_content 正文开关）+ 全局装配（`set_logger/get`）+ 会话上下文（`set_current_session`，threading.local）。
- **事件分层**（不记正文与 Key）：
  - `ask`：agent.ask 终态 + 任务 completed 路径（带 task_id）；ok/latency/llm_calls/tokens/cost/degraded/web_used/error_type；正文仅 log_content=True 写入（ACC-U6-02）。
  - `tool_call`：executor.execute 每工具步骤 1 条；action/ok/error_type/latency/attempts/degraded/input_keys（参数只记键名摘要，不记值）。
  - `llm_call`：llm.chat 与 chat_json（含修复轮）每次 _chat 1 条；ok/latency/tokens/model；埋点在 `_chat_audited`，子类覆写 _chat 的假客户端同样生效。
  - `admin`：config_save（只记键名摘要不记值）/memory_delete/memory_delete_session/memory_clear/session_reset/kb_rebuild；含操作者会话与结果摘要（ACC-U6-03）。
- **查询接口**：`GET /api/audit?date=&type=&limit=`——date 缺省当天、type 过滤、limit 1–1000（新→旧）；date 格式错/limit 非法 → 400，未知子路径 → 404（ACC-U6-04）。
- **装配与开关**：`config.yaml` 新增 `audit.{enabled,retention_days,log_content}`（默认 true/30/false）；`web_server.main()` 装配；`audit.enabled=false` 不装配（埋点 no-op）；validate_config 新增 audit.retention_days（1–3650）与 audit.{enabled,log_content} 布尔规则；CONFIG_FIELDS 白名单开放 retention_days/log_content。
- **涉及文件**：新增 src/agents/audit.py、tests/test_audit.py；修改 config.yaml、src/agents/{config,agent,executor,llm,task_runner,web_server}.py。

---

## G5 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 121/121 OK（G4 后 112 → 新增 test_key_writeback_security 9 用例）；直跑 14/14 退出码 0；compileall 通过；全程离线（密钥文件与任务库/会话库均在 tempfile，绝不读写用户 .env 与 data/runtime.json）。
- **密钥安全（P0-5）**：
  - `key_file_permissions_ok(path)`：owner 外任一权限位（组/其他）→ 过宽；文件缺失视为满足。
  - `load_config()` 启动时调用 `warn_key_file_permissions()`：.env / runtime.json 过宽即告警，提示将拒绝保存新密钥与 chmod 600。
  - `save_runtime()` 写后自动 `chmod 0600`（新建/覆写均生效）。
  - `/api/config` 保存新密钥前检查 runtime.json 权限，过宽返回 **403**（提示 chmod 600），普通配置项不受影响；权限收紧后重试放行。
  - **G2 观察项 3 关闭**：llm.api_key 保存路径去掉 `str()` 强转，与 vision.api_key 统一——int/float 等非字符串进入 validate_config 报类型错误 → 400；掩码回显值（含 `****`）忽略不落盘。
  - API 响应沿用 `_config_view` 掩码；TaskRecord to_dict/summary 无密钥字段（测试断言）；日志只记键名不记值。
  - 钥匙串扩展点：config.py 权限段注释已留接口约定（本期不引入依赖）。
- **任务写回幂等（P0-5）**：
  - `TaskRecord.writeback_id`（写回成功后置为 task_id 并持久化；from_dict 容错兼容旧文件）。
  - `WebStore.has_task_writeback(session_id, task_id)`：按 assistant 消息 metrics.task_id 检索；`WebStore.add_task_result(...)`：user+assistant 消息与 query_event 单连接事务原子落库（失败无半写）。
  - `Handler.persist_task_result`：writeback_id 已置位 → 跳过；库中已有同 task_id 消息（崩溃重放/旧记录升级）→ 只补标识不重复写；成功 → 原子写回 + `_mark_writeback` 持久化。重复回调/重启重放消息只出现一次。
  - 写回失败：任务保持 completed、答案不回滚、writeback_id 为空（可重试）；恢复后重试成功且不重复。`_notify_complete` 吞异常语义不变。
- **关键坑**：`from agents.config import RUNTIME_PATH` 是导入期绑定，测试 monkeypatch `config_mod.RUNTIME_PATH` 无效——web_server 改为 `config_mod.RUNTIME_PATH` 模块属性访问。
- **涉及文件**：src/agents/{config,web_server,web_store,task_store}.py、tests/test_key_writeback_security.py（新增 9 用例）。

---

## G4 切片记录（2026-09-04，控制器续接）

- **验证证据**：`unittest discover` 112/112 OK（G3 后 103 → 新增 test_memory_governance 9 用例）；compileall 全仓通过；全程离线（TfidfHashEmbeddingBackend + 裸 Handler 桩，不读写用户 data/runtime.json 与 .env）。
- **P0-2 范围逐条落实**：
  - `GET /api/memory?session_id=&q=&limit=` → `{"episodes":[...], "total":n}`（episode dict 含 id/session_id/ts/question/answer_summary/sources/entities/hits——来源会话、创建时间、命中次数全透出）；长期记忆未开启返回 `{"episodes":[],"total":0,"disabled":true}`。
  - `DELETE /api/memory/{episode_id}` → 200/404；`DELETE /api/memory?session_id=` → `{"ok":true,"removed":n}`；`POST /api/memory/clear` → 长期 + 实体同步清空并返回计数；`GET /api/memory/entities` → 实体事实视图。
  - episode 来源会话/创建时间/命中次数字段 S4 已有（session_id/ts/hits），本切片通过 API 视图透出。
- **关键实现**：`LongTermMemory._drop_index`（TF-IDF 整体重建 / 其余后端向量行删除，保证删除后检索不维度错位）；`Handler.do_DELETE`（与 GET/POST 同款兜底与 Host 校验）；/api/reset 联动长期记忆删除（响应新增 `long_term_removed` 键，向后兼容）。
- **涉及文件**：src/agents/{long_memory,web_server}.py、scripts/webui.html（记忆设置页 + escHtml 工具 + CSS）、tests/test_memory_governance.py（新增 9 用例）。
- **注意**：/api/sessions 分支的原函数内 `from urllib.parse import ...` 局部导入上移到模块级（局部导入会使同函数内后续 `parse_qs` 触发 UnboundLocalError）。

---

## G3 切片记录（2026-09-04，控制器续接 zcode 会话完成）

- **验证证据**：`unittest discover` 103/103 OK（G2 后 88 → 新增 test_egress_defaults 15 用例）；直跑 12/12 退出码 0；`compileall` 全仓通过；全程离线（ScriptedLLM 假客户端 + monkeypatch 临时目录，绝不读写用户 data/runtime.json 与 .env）。
- **ACC-U3-01** ✓：降级步骤 → `/api/ask` 响应 `metrics.degraded==true`，旧键（latency_s/llm_calls/tokens/cost_yuan/citations_*）齐全。
- **ACC-U3-02** ✓：webui.html 答案卡片含「本回答来自 Web 搜索降级」/「本回答使用了 Web 搜索」状态行，渲染条件 `metrics.degraded`（优先）/`metrics.web_used`（次之），皆 false 不渲染。
- **ACC-U3-03** ✓：`_egress_flags` 统一口径（StepResult 与持久化步骤 dict 双支持）落任务写回与任务详情；`remember=false` 时零长期记忆写入、零实体抽取 LLM 调用。
- **关键实现**：`Agent.resolve_allow_web/_tools_for_run`（工具视图副本，强禁剔除 web_search 双保险：规划清单 + executor 降级门控）；`Agent._kb_miss(steps, web_allowed)`（禁网口径视为未联网）；`parse_opt_bool`（字符串布尔归一化，非法值 400）；`config._maybe_log_egress_migration_hint`（存量 runtime.json 未显式设置时 INFO 提示一次/进程）。
- **兼容策略**：runtime.json 显式配置优先、不强制覆盖；`Agent.ask()` 缺省参数下签名向后兼容（verbose/session_id 不变，新参均可选）。
- **涉及文件**：config.yaml（三开关翻转+注释）、src/agents/{config,agent,executor,task_runner,tools,web_server}.py、scripts/webui.html、tests/{test_egress_defaults(新),test_long_memory,test_task_runner,test_tool_hardening}.py。
