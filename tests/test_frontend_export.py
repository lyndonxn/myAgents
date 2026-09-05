"""W6-S1 测试：Next.js 静态导出伺服（Handler._static_export）。

全部离线：tempfile 伪造 frontend/out 目录，monkeypatch 模块级 EXPORT_DIR，
验证根路径/index 映射、嵌套资源 MIME、路径穿越拒绝、未知文件回 None（触发 legacy 回退）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents import web_server as web_server_mod
from agents.web_server import Handler


class StaticExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self._orig = web_server_mod.EXPORT_DIR
        web_server_mod.EXPORT_DIR = self.out

    def tearDown(self) -> None:
        web_server_mod.EXPORT_DIR = self._orig
        self._tmp.cleanup()

    def _get(self, path: str):
        return Handler._static_export(object.__new__(Handler), path)

    def test_missing_export_dir_returns_none(self):
        """out/ 不存在（未构建的全新克隆）：返回 None → 服务器回退 legacy 页面。"""
        web_server_mod.EXPORT_DIR = self.out / "not-exist"
        self.assertIsNone(self._get("/"))

    def test_index_served_for_root_and_index(self):
        (self.out / "index.html").write_text("<html>next-shell</html>", encoding="utf-8")
        body, mime = self._get("/")
        self.assertIn(b"next-shell", body)
        self.assertTrue(mime.startswith("text/html"))
        body2, _ = self._get("/index.html")
        self.assertEqual(body, body2, "/ 与 /index.html 应同源")

    def test_nested_asset_served_with_js_mime(self):
        d = self.out / "_next" / "static" / "chunks"
        d.mkdir(parents=True)
        (d / "app.js").write_text("console.log(1)", encoding="utf-8")
        body, mime = self._get("/_next/static/chunks/app.js")
        self.assertEqual(body, b"console.log(1)")
        self.assertIn("javascript", mime)

    def test_path_traversal_rejected(self):
        """`..` 穿越导出目录必须拒绝（返回 None，不泄露导出目录外文件）。"""
        secret = self.out.parent / "w6-secret.txt"
        secret.write_text("secret", encoding="utf-8")
        try:
            self.assertIsNone(self._get("/../w6-secret.txt"))
            self.assertIsNone(self._get("/..%2Fw6-secret.txt"))
        finally:
            secret.unlink()

    def test_unknown_asset_returns_none(self):
        (self.out / "index.html").write_text("x", encoding="utf-8")
        self.assertIsNone(self._get("/nope.js"))

    def test_query_string_ignored(self):
        (self.out / "index.html").write_text("<html>q</html>", encoding="utf-8")
        body, _ = self._get("/?v=123")
        self.assertIn(b"q", body)


if __name__ == "__main__":
    unittest.main(verbosity=1)
