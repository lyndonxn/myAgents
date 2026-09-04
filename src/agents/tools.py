"""工具层（Tools）：统一的工具注册表与内置工具实现。

工具协议：
  Tool(name, description, parameters, func)
  - parameters: JSON Schema（dict），用于 LLM 生成参数
  - func(ctx, **kwargs) -> Any：执行逻辑，返回可序列化结果

执行上下文 ToolContext 向工具注入检索器 / 配置 / LLM 等依赖，
避免工具直接持有全局状态。工具错误由执行器捕获，不影响整体问答。

参数校验（S1 工具层强化）：validate_tool_input 按工具 parameters JSON Schema
校验并纠偏入参（required 缺失报错、未知键剔除、字符串数字纠偏、整数 clamp），
执行器在调用工具前先校验，校验失败不重试。
"""
from __future__ import annotations

import ast
import datetime as _dt
import operator
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .logger import get_logger

LOG = get_logger("tools")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]
    enabled: bool = True

    def describe(self) -> str:
        return f"{self.name}: {self.description} 参数: {self.parameters}"

    def __call__(self, ctx: "ToolContext", **kwargs) -> Any:
        return self.func(ctx, **kwargs)


@dataclass
class ToolContext:
    retriever: Any = None
    config: Any = None
    llm: Any = None
    chunks: list = field(default_factory=list)
    web_search: Any = None


# ---------------- 参数校验（S1 工具层强化） ----------------

class ToolValidationError(ValueError):
    """工具入参不符合 JSON Schema（缺 required、类型无法纠偏等），重试无法恢复。"""


def _coerce_by_schema(value: Any, prop: dict, name: str) -> Any:
    """按 schema 属性定义纠偏单个参数值，并按 minimum/maximum clamp 数值。

    纠偏失败（如把 "abc" 纠偏为整数）抛 ToolValidationError。
    """
    expected = prop.get("type")
    if expected is None or value is None:
        return value
    if expected == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (bool, int, float)):
            return str(value)
        raise ToolValidationError(f"参数 {name} 期望 string，实际 {type(value).__name__}")
    if expected in ("integer", "number"):
        if isinstance(value, bool):
            raise ToolValidationError(f"参数 {name} 期望 {expected}，实际为布尔值")
        if isinstance(value, int):
            num: int | float = value
        elif isinstance(value, float):
            if expected == "integer" and not value.is_integer():
                raise ToolValidationError(f"参数 {name} 期望整数，实际 {value}")
            num = value
        elif isinstance(value, str):
            text = value.strip()
            try:
                num = int(text)
            except ValueError:
                try:
                    num = float(text)
                except ValueError as float_err:
                    raise ToolValidationError(
                        f"参数 {name} 期望 {expected}，无法把 {value!r} 纠偏为数字"
                    ) from float_err
            if expected == "integer":
                if isinstance(num, float) and not num.is_integer():
                    raise ToolValidationError(f"参数 {name} 期望整数，无法把 {value!r} 纠偏为整数")
                num = int(num)
        else:
            raise ToolValidationError(f"参数 {name} 期望 {expected}，实际 {type(value).__name__}")
        minimum, maximum = prop.get("minimum"), prop.get("maximum")
        if minimum is not None and num < minimum:
            num = minimum
        if maximum is not None and num > maximum:
            num = maximum
        return num
    if expected == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1"):
                return True
            if lowered in ("false", "0"):
                return False
            raise ToolValidationError(f"参数 {name} 期望 boolean，无法把 {value!r} 纠偏为布尔值")
        if isinstance(value, (int, float)):
            return bool(value)
        raise ToolValidationError(f"参数 {name} 期望 boolean，实际 {type(value).__name__}")
    return value  # array / object 等其他类型不做处理


def validate_tool_input(tool: Tool, kwargs: dict) -> dict:
    """按工具 parameters 的 JSON Schema 校验并纠偏入参，返回新的干净参数字典。

    - required 缺失（或值为 None）→ 抛 ToolValidationError（执行器不重试）
    - 未知键 → 剔除并 LOG.warning
    - 字符串数字按 schema type 纠偏（"5"→5、"true"→True）
    - 整数按 schema 的 minimum/maximum clamp（工具函数内部已有的 clamp 保持不动）
    - schema 缺少 properties 键时视为未声明参数，原样放行
    """
    schema = tool.parameters if isinstance(tool.parameters, dict) else {}
    if "properties" not in schema:
        return dict(kwargs)
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    missing = [key for key in required if kwargs.get(key) is None]
    if missing:
        raise ToolValidationError(f"缺少必填参数: {', '.join(missing)}")
    out: dict = {}
    for key, value in kwargs.items():
        if key not in props:
            LOG.warning("工具 %s 收到未知参数 %s=%r，已剔除", tool.name, key, value)
            continue
        prop = props[key] if isinstance(props[key], dict) else {}
        out[key] = _coerce_by_schema(value, prop, key)
    return out


# ---------------- 内置工具实现 ----------------

def _fmt_hit(rank: int, hit) -> str:
    """叶子命中点（精确匹配）+ 父块上下文（完整章节，供生成引用）。"""
    title = f"（{hit.chunk.heading}）" if hit.chunk.heading else ""
    leaf_line = ""
    if hit.leaf_text and hit.leaf_text not in hit.text[:200]:
        leaf_line = f"命中点：{hit.leaf_text[:120]}\n"
    head = hit.text[:700].replace("\n", " ")
    return f"[{rank}] {title} 分数 {hit.final_score:.3f}\n{leaf_line}内容：{head}"


def tool_search_knowledge_base(ctx: ToolContext, query: str = "", top_k: int = 5) -> dict:
    """从知识库检索与 query 最相关的片段，返回正文与来源列表。"""
    if not ctx.retriever:
        raise RuntimeError("检索器未初始化")
    top_k = max(1, min(int(top_k or 5), 10))
    hits = ctx.retriever.retrieve(query, top_k=top_k)
    lines = [f"针对「{query}」检索到 {len(hits)} 条相关片段："]
    sources: list[str] = []
    for rank, hit in enumerate(hits, start=1):
        lines.append(_fmt_hit(rank, hit))
        src = hit.source or hit.file
        if src not in sources:
            sources.append(src)
    return {"text": "\n".join(lines), "sources": sources, "hit_count": len(hits)}


def tool_web_search(ctx: ToolContext, query: str = "", max_results: int = 5) -> dict:
    """从 Web 搜索 query，返回标题/URL/摘要列表（无 Key，Bing 网页搜索）。"""
    if not ctx.web_search:
        raise RuntimeError("Web 搜索未初始化")
    return ctx.web_search.search(query, max_results=int(max_results or 5))


def tool_list_topics(ctx: ToolContext) -> dict:
    """列出知识库中的主要主题（每篇笔记的一级/二级标题）。"""
    topics: dict[str, list[str]] = {}
    for c in ctx.chunks or []:
        first = c.heading.split(" > ")[0] if c.heading else c.file
        topics.setdefault(c.file, [])
        if first not in topics[c.file]:
            topics[c.file].append(first)
    lines = [f"{file}: {', '.join(t[:30] for t in ts[:5])}" for file, ts in topics.items()]
    return {"text": "知识库主题概览：\n" + "\n".join(lines[:40]), "topics": topics}


def tool_get_current_time(ctx: ToolContext) -> dict:
    """获取当前日期时间。"""
    now = _dt.datetime.now()
    return {"text": f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（{now.strftime('%A')}）"}


# 安全的算术求值（白名单）
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的字面量: {node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式元素: {type(node).__name__}")


def tool_calculator(ctx: ToolContext, expression: str = "") -> dict:
    """对纯算术表达式做安全求值（+ - * / // % ** 与括号，不支持变量/函数）。"""
    if not expression:
        raise ValueError("expression 不能为空")
    tree = ast.parse(expression.strip(), mode="eval")
    result = _safe_eval(tree)
    return {"text": f"{expression.strip()} = {result}", "result": result}


# ---------------- 注册表 ----------------

def web_search_tool() -> Tool:
    """web_search 工具定义（G3）：供 build_tools 注册与按请求 allow_web 强许路径复用。

    工具本体无状态（执行时经 ToolContext 取 WebSearch 实例），故可独立构建。
    """
    return Tool(
        name="web_search",
        description="在互联网上搜索给定查询，返回网页标题、URL 与摘要。当知识库中找不到答案、或问题需要最新/时效性信息时使用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询，应包含关键术语，尽量具体"},
                "max_results": {"type": "integer", "description": "返回结果数，默认 5", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
        func=tool_web_search,
    )


def build_tools(config, retriever=None, chunks=None, llm=None, web_search=None) -> dict[str, Tool]:
    """按配置构建工具注册表（名字 -> Tool）。"""
    ctx = ToolContext(retriever=retriever, config=config, llm=llm, chunks=chunks, web_search=web_search)
    flags = config.tools_enabled

    tools: dict[str, Tool] = {
        "search_knowledge_base": Tool(
            name="search_knowledge_base",
            description="从知识库中检索与给定查询最相关的文本片段，返回片段内容与来源。回答知识性问题时首选此工具。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询，应包含关键术语，尽量具体"},
                    "top_k": {"type": "integer", "description": "返回片段数，默认 5", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
            func=tool_search_knowledge_base,
        ),
        "web_search": replace(web_search_tool(), enabled=flags["web_search"]),
        "list_knowledge_topics": Tool(
            name="list_knowledge_topics",
            description="列出知识库包含哪些主题/笔记，适合探索性提问（如『知识库里有什么』）。",
            parameters={"type": "object", "properties": {}, "required": []},
            func=tool_list_topics,
            enabled=flags["topics"],
        ),
        "get_current_time": Tool(
            name="get_current_time",
            description="获取当前日期与时间，用于时效性问题。",
            parameters={"type": "object", "properties": {}, "required": []},
            func=tool_get_current_time,
            enabled=flags["time"],
        ),
        "calculator": Tool(
            name="calculator",
            description="安全计算纯算术表达式（如 3*(2+4)），不支持变量与函数调用。",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "算术表达式"}},
                "required": ["expression"],
            },
            func=tool_calculator,
            enabled=flags["calculator"],
        ),
    }
    return {name: t for name, t in tools.items() if t.enabled}


def describe_tools(tools: dict[str, Tool]) -> list[str]:
    return [t.describe() for t in tools.values()]
