"""统一日志模块：控制台 + 滚动文件双输出。

日志文件：<data_dir>/logs/myagents.log（RotatingFileHandler，1MB × 5 轮转）
级别：控制台 INFO，文件 INFO；错误单独记 ERROR/WARNING。

用法：
    from .logger import get_logger
    log = get_logger("webui")
    log.info("...")
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOG_CONFIGURED = False
_LOGS_DIR: Path | None = None


def setup_logging(logs_dir: Path | None = None) -> Path:
    """初始化根日志（幂等）。返回日志目录。"""
    global _LOG_CONFIGURED, _LOGS_DIR
    if _LOG_CONFIGURED and logs_dir is None:
        return _LOGS_DIR or Path("logs")
    if logs_dir is not None:
        _LOGS_DIR = logs_dir
    logs_dir = _LOGS_DIR or Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "myagents.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not _LOG_CONFIGURED:
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
        _LOG_CONFIGURED = True
    # 文件 handler：每次 setup 刷新路径（可热切换日志目录）
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)
    return logs_dir


def get_logger(name: str) -> logging.Logger:
    """获取模块日志器（保证日志目录已初始化）。"""
    if not _LOG_CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
