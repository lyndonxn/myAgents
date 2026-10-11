"""vault-mcp：把 MyAgents 知识库包装为本地 stdio MCP Server（spec/09 T-009）。

工具设计来自知识库《MCP Server 开发实战：FastMCP 从零到部署》第二节"本库的实际机会"：
- search_notes：复用 MyAgents 混合检索（Retriever.retrieve，C6 冻结，零检索逻辑复制）
- get_decisions：读 kb_path 下"决策记录/"，让任何 MCP 客户端继承既有决策（ADR）
- log_digestion：记录消化进度（写工具，C7 最小权限：目标文件白名单 +
  note_path 越界拒绝 + 审计事件）

安全口径（同设计笔记"MCP 安全篇"自查清单）：本地 stdio 仅本机可用；
fastmcp 为可选依赖（requirements-mcp.txt）。启动：`python -m agents.mcp_server`。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import audit as audit_mod
from .logger import get_logger

LOG = get_logger("mcp_server")

_DECISION_CHARS = 4000   # 单篇决策记录返回字符上限（防 token 失控）
_DIGEST_SUMMARY_CHARS = 500


def build_mcp_server(agent):
    """以就绪的 Agent（索引已加载）构建 FastMCP server（stdio 传输由 run() 决定）。"""
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise ImportError(
            "fastmcp 未安装：请执行 .venv/bin/pip install -r requirements-mcp.txt"
        ) from exc

    mcp = FastMCP(name="myagents-vault")

    @mcp.tool
    def search_notes(query: str, top_k: int = 5) -> dict:
        """在 MyAgents 知识库中检索最相关的笔记段落。query 为自然语言问题，返回带来源路径、标题与相关度得分的片段列表。"""
        if agent.retriever is None:
            raise RuntimeError("检索器未初始化")
        k = max(1, min(int(top_k or 5), 10))
        hits = agent.retriever.retrieve(query, top_k=k)
        return {
            "query": query,
            "hit_count": len(hits),
            "hits": [
                {
                    "rank": h.rank,
                    "file": h.file,
                    "heading": h.chunk.heading,
                    "snippet": (h.leaf_text or h.chunk.text or "").strip()[:400],
                    "score": round(float(h.final_score), 4),
                }
                for h in hits
            ],
        }

    @mcp.tool
    def get_decisions(limit: int = 20) -> dict:
        """列出知识库"决策记录"目录下的决策/ADR 笔记内容，让 Agent 继承既有决策再行动。"""
        base = Path(agent.config.kb_path)
        decisions_dir = base / agent.config.mcp_decisions_dir
        if not decisions_dir.is_dir():
            return {"decisions": [], "count": 0, "note": f"决策记录目录不存在: {agent.config.mcp_decisions_dir}"}
        k = max(1, min(int(limit or 20), 100))
        decisions = []
        for p in sorted(decisions_dir.glob("*.md"))[:k]:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 - 单篇读取失败不影响其余
                decisions.append({"file": p.name, "error": str(exc)})
                continue
            decisions.append({"file": p.name, "title": p.stem, "content": text[:_DECISION_CHARS]})
        return {"decisions": decisions, "count": len(decisions)}

    @mcp.tool
    def log_digestion(note_path: str, summary: str) -> dict:
        """记录一篇知识库笔记的消化进度摘要。note_path 必须指向知识库内的笔记；服务端仅允许追加写入配置的 digest 日志文件。"""
        base = Path(agent.config.kb_path).resolve()
        target = Path(note_path).expanduser()
        if not target.is_absolute():
            target = base / target
        target = target.resolve()
        # C7 白名单：note_path 必须落在知识库内（防任意路径读写）
        if target != base and base not in target.parents:
            return {"ok": False, "error": f"note_path 越出知识库范围: {note_path}"}
        if not target.is_file():
            return {"ok": False, "error": f"笔记不存在: {note_path}"}
        log_path = agent.config.mcp_digest_log
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "note_path": str(target.relative_to(base)),
            "summary": str(summary or "")[:_DIGEST_SUMMARY_CHARS],
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # C7 审计：写事件落审计轨迹（audit 关闭时 no-op）
        audit = audit_mod.get()
        if audit is not None:
            try:
                audit.log_admin("mcp_log_digestion", ok=True, detail=entry["note_path"])
            except Exception:  # noqa: BLE001 - 审计失败不影响业务
                pass
        return {"ok": True, "log": str(log_path), "entry": entry}

    return mcp


def main() -> None:
    """stdio 入口：stdout 是 MCP 协议通道，一切日志走 stderr。"""
    from .agent import Agent
    from .audit import AuditLogger
    from .config import load_config

    config = load_config()
    # C7：写工具的审计轨迹与 web 服务同口径初始化（audit.enabled 关闭时 no-op）
    if config.audit_enabled:
        audit_mod.set_logger(AuditLogger(
            config.data_dir / "audit",
            retention_days=config.audit_retention_days,
            log_content=config.audit_log_content,
        ))
    agent = Agent(config)
    agent.load_index()
    server = build_mcp_server(agent)
    print(f"[mcp] vault-mcp 就绪: kb={config.kb_path} decisions={config.mcp_decisions_dir}", file=sys.stderr)
    server.run()


if __name__ == "__main__":
    main()
