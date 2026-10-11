"""vault-mcp 测试（spec/09 T-009，MCP-ACC-001/002/003）。

全部离线：tmp 知识库 + tmp 决策记录 + 内存 FastMCP client（不经 stdio/网络）。
覆盖：工具面（恰好 3 个 + Schema）、search_notes 结构化返回与 Retriever 同源、
get_decisions 读取、log_digestion 白名单（越界拒绝零落盘）与审计事件。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import fastmcp  # noqa: F401

    HAS_FASTMCP = True
except ImportError:
    HAS_FASTMCP = False

from agents.agent import Agent
from agents.chunking import Chunk
from agents.config import Config

_KB_NOTE = (
    "RAG（Retrieval-Augmented Generation）的核心是先检索再生成。索引阶段把文档切成父子分块，"
    "叶子块用于精确召回，父块用于扩展上下文。检索阶段混合向量召回与 BM25 关键词召回，用 RRF 融合两路排名。"
)

ADR_1 = "# ADR-0001 检索引擎自研\n\n决定自研混合检索而非引入框架，理由是可控与教学价值。"
ADR_2 = "# ADR-0002 默认拒绝联网\n\n外发默认关闭，用户显式授权后才允许 web_search。"


def make_config(tmp: Path) -> Config:
    return Config({
        "data_dir": str(tmp / "data"),
        "kb_path": str(tmp / "kb"),
        "embedding": {"backend": "tfidf", "hash_dim": 256},
        "retrieval": {"rerank": "off", "fusion_mode": "rrf"},
        "audit": {"enabled": True},
    })


def make_fixture(tmp: Path) -> Agent:
    kb = tmp / "kb"
    (kb / "决策记录").mkdir(parents=True)
    (kb / "决策记录" / "ADR-0001.md").write_text(ADR_1, encoding="utf-8")
    (kb / "决策记录" / "ADR-0002.md").write_text(ADR_2, encoding="utf-8")
    (kb / "笔记0.md").write_text(_KB_NOTE, encoding="utf-8")
    config = make_config(tmp)
    agent = Agent(config, lazy_index=True)
    agent.build_index(
        chunks=[Chunk(id=0, text=_KB_NOTE, file="笔记0.md", heading="RAG", source="笔记0.md")],
        persist=False,
    )
    return agent


def _data(result):
    """兼容 fastmcp 不同版本的返回读取（结构化 data 优先，回退 content 文本）。"""
    data = getattr(result, "data", None)
    if data is not None:
        return data
    return json.loads(result.content[0].text)


@unittest.skipUnless(HAS_FASTMCP, "fastmcp 未安装（可选依赖，见 requirements-mcp.txt）")
class VaultMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.agent = make_fixture(self.tmp)
        from agents.mcp_server import build_mcp_server

        self.mcp = build_mcp_server(self.agent)
        # 与 mcp_server.main 同口径初始化审计（audit.enabled=True → tmp data/audit）
        from agents.audit import AuditLogger
        from agents import audit as audit_mod

        audit_mod.set_logger(AuditLogger(
            self.agent.config.data_dir / "audit",
            retention_days=self.agent.config.audit_retention_days,
            log_content=self.agent.config.audit_log_content,
        ))

    def tearDown(self):
        self._tmp.cleanup()

    async def test_tool_surface(self):
        """MCP-ACC-001：恰好三个工具，Schema 由类型注解生成。"""
        from fastmcp import Client

        async with Client(self.mcp) as client:
            tools = await client.list_tools()
        names = {t.name for t in tools}
        self.assertEqual(names, {"search_notes", "get_decisions", "log_digestion"})
        by_name = {t.name: t for t in tools}
        self.assertIn("query", by_name["search_notes"].inputSchema["properties"])
        self.assertIn("note_path", by_name["log_digestion"].inputSchema["properties"])
        print("✓ MCP-ACC-001 工具面：3 工具 + Schema")

    async def test_search_notes_structured(self):
        """MCP-ACC-002：结构化返回，与 Retriever 同源。"""
        from fastmcp import Client

        async with Client(self.mcp) as client:
            result = await client.call_tool("search_notes", {"query": "RAG 检索", "top_k": 3})
        data = _data(result)
        self.assertEqual(data["query"], "RAG 检索")
        self.assertGreaterEqual(data["hit_count"], 1)
        hit = data["hits"][0]
        for key in ("rank", "file", "heading", "snippet", "score"):
            self.assertIn(key, hit)
        self.assertEqual(hit["file"], "笔记0.md")
        # 同源核对：与直接调用 Retriever 的首个文件一致
        direct = self.agent.retriever.retrieve("RAG 检索", top_k=3)
        self.assertEqual(hit["file"], direct[0].file)
        print("✓ MCP-ACC-002 search_notes 结构化返回且与 Retriever 同源")

    async def test_get_decisions(self):
        from fastmcp import Client

        async with Client(self.mcp) as client:
            result = await client.call_tool("get_decisions", {"limit": 10})
        data = _data(result)
        self.assertEqual(data["count"], 2)
        titles = {d["file"] for d in data["decisions"]}
        self.assertEqual(titles, {"ADR-0001.md", "ADR-0002.md"})
        self.assertIn("自研混合检索", data["decisions"][0]["content"])
        print("✓ MCP-ACC-003a get_decisions 读取决策记录")

    async def test_log_digestion_whitelist_and_audit(self):
        """MCP-ACC-003b：合法写入 + 越界拒绝零落盘 + 审计事件。"""
        from fastmcp import Client

        digest = self.tmp / "data" / "mcp_digestion.jsonl"
        async with Client(self.mcp) as client:
            ok = _data(await client.call_tool(
                "log_digestion", {"note_path": "笔记0.md", "summary": "已消化 RAG 篇"}
            ))
            self.assertTrue(ok["ok"])
            rejected = _data(await client.call_tool(
                "log_digestion", {"note_path": str(self.tmp / "outside.md"), "summary": "x"}
            ))
            self.assertFalse(rejected["ok"], "越出知识库的路径必须被拒绝")
            missing = _data(await client.call_tool(
                "log_digestion", {"note_path": "不存在.md", "summary": "x"}
            ))
            self.assertFalse(missing["ok"])
        # 唯一写入：digest 文件恰 1 行合法 JSON
        lines = digest.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1, "越界与缺失路径不得落盘")
        entry = json.loads(lines[0])
        self.assertEqual(entry["note_path"], "笔记0.md")
        self.assertEqual(entry["summary"], "已消化 RAG 篇")
        # 审计事件
        audit_files = list((self.tmp / "data" / "audit").glob("*.jsonl"))
        self.assertTrue(audit_files, "写操作应产生审计事件")
        self.assertTrue(any("mcp_log_digestion" in f.read_text(encoding="utf-8") for f in audit_files))
        print("✓ MCP-ACC-003b log_digestion 白名单/越界拒绝/审计")


if __name__ == "__main__":
    unittest.main(verbosity=1)
