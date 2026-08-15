"""Web 前端入口（薄壳）：逻辑在 src/agents/web_server.py。

用法:
  python -m scripts.webui            # 启动并自动打开浏览器
  python -m scripts.webui --port 8787
  python -m scripts.webui --host 0.0.0.0   # 仅当需要局域网访问（不安全，不推荐）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.web_server import main  # noqa: E402

if __name__ == "__main__":
    main()
