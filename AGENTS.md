# AGENTS.md — myAgents 工程契约

## 项目目的
本地优先的 Markdown 知识库问答 Agent（Python，无重框架）。五大核心模块已交付并验收（`spec/agent-core-modules.md`）。当前根任务：2026-09 升级——企业安全（G1–G6）、任务可靠性（G7）、度量与能力（G8–G11），规格见 `spec/upgrade-2026-09.md`。

## 指令优先级
当前用户指令 > 本文件 > spec 验收规格 > 其他文档（README/MAINTENANCE）。

## 硬约束
1. **离线测试**：所有新增测试不得调用真实 LLM API、不得联网（仿照 `tests/test_smoke.py` 的 plain-script 风格，用 fake LLM / monkeypatch）。运行 LLM 的 benchmark 属付费操作，未经用户指令不得运行。
2. **用户所有物不可触碰**：`dist/`（含未提交删除）、`docs/`、`data/`、`logs/`、`.env*`、`assets/`。禁止 `git add` 这些路径。`scripts/webui.html` 自 T 阶段（遗留事项处理）起允许修改，但必须保持既有设计系统与明暗双主题，禁止整页重写或更换设计风格。
3. **向后兼容**：`Agent.ask()` 现有签名与返回结构、`/api/ask` 与 `/api/ask_stream` 行为、`SessionMemory` 现有公开方法不得破坏；新能力通过新增字段/参数（带默认值）与新增端点交付。
4. **轻依赖**：不引入新的第三方依赖（requests/yaml/numpy/sentence-transformers 之外）。长期记忆复用项目内 embedding 后端与 numpy。
5. **风格**：与现有代码一致——中文 docstring、模块级注释、dataclass、`from __future__ import annotations`。
6. 分支会话只写自己的 owned paths；发现必须越界才能完成时，停止并报告，不要自行扩大改动。

## 验证命令（每次切片必跑）
```bash
cd /Users/mima1234/Documents/myAgents
.venv/bin/python -m py_compile src/agents/*.py tests/*.py benchmark/*.py
.venv/bin/python -m unittest discover -s tests -p "test_*.py"   # 统一入口（P0-1）：发现并执行全部离线测试，失败必须非 0
.venv/bin/python tests/<本切片新测试>                            # 直跑兼容仍保留
```

## 完成定义
切片实现 → 控制器跑验证 → 独立验收子代理按 spec 验收 ID 逐条核对 → 提交（只含本切片文件）→ 更新 HANDOFF.md。
