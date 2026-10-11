"""LangGraph 编排引擎包（spec/08 T-006 Phase 0）。

lazy 导出（PEP 562）：langgraph 为可选依赖，legacy 模式（默认）不触发任何
本包的 langgraph 导入——未安装时对既有链路零影响。
"""
from __future__ import annotations

_LAZY_EXPORTS = {
    "GraphState": ".state",
    "build_retrieval_subgraph": ".retrieval_subgraph",
    "build_answer_graph": ".runner",
    "run_graph_ask": ".runner",
    "stream_graph_ask": ".runner",
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(target, __name__), name)
