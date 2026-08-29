"""引用校验（S3）：解析答案中的 [n] 引用标记并按来源数校验合法性。

答案正文用 [n] 标注依据（n 对应参考来源编号），但模型可能编造超出来源
数量的编号（幻觉引用）。本模块负责提取、校验并从答案中剔除非法标记，
供 Agent 主流程（生成后校验）与评测脚本（citation_hallucination 指标）复用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 引用标记：`[` 后紧跟一串数字紧跟 `]`（形如 \[(\d+)\]）。环视用于排除误判：
# - (?<!\[) / (?!\])：排除双方括号（[[wiki]] / [[1]]）的内层命中
# - (?!:)：排除 Markdown 链接定义 [1]: url
_CITATION_RE = re.compile(r"(?<!\[)\[(\d+)\](?!\]|:)")


@dataclass
class CitationReport:
    """引用校验结果。

    valid_count / invalid_count：合法 / 非法标记的出现次数；
    cleaned_text：移除非法标记后的文本（只删标记本身，其余文字与空格原样保留）；
    invalid_numbers：非法编号（去重，按首次出现顺序）。
    """

    valid_count: int = 0
    invalid_count: int = 0
    cleaned_text: str = ""
    invalid_numbers: list[int] = field(default_factory=list)


def extract_citations(text: str) -> list[int]:
    """按出现顺序提取文本中所有 [数字] 引用编号。

    Markdown 链接定义（[1]: url）、wiki 式双方括号（[[1]]）与非数字
    方括号（[abc]）均不会被误判为引用。
    """
    return [int(m.group(1)) for m in _CITATION_RE.finditer(text or "")]


def validate_citations(text: str, n_sources: int) -> CitationReport:
    """按来源数 n_sources 校验文本中的 [n] 引用。

    编号 n < 1 或 n > n_sources 视为非法：从 cleaned_text 移除该标记本身，
    合法标记与其余文本原样保留。
    """
    text = text or ""
    valid_count = 0
    invalid_count = 0
    invalid_nums: list[int] = []

    def _replace(m: "re.Match[str]") -> str:
        nonlocal valid_count, invalid_count
        num = int(m.group(1))
        if 1 <= num <= n_sources:
            valid_count += 1
            return m.group(0)  # 合法引用原样保留
        invalid_count += 1
        if num not in invalid_nums:
            invalid_nums.append(num)
        return ""  # 只删除非法标记本身，不动周围文字

    cleaned = _CITATION_RE.sub(_replace, text)
    return CitationReport(
        valid_count=valid_count,
        invalid_count=invalid_count,
        cleaned_text=cleaned,
        invalid_numbers=invalid_nums,
    )
