"""知识库加载、清洗与分片。

职责：
1. 递归扫描知识库目录中的 Markdown 文件（跳过隐藏目录 / 模板等）。
2. 清洗 Obsidian 语法：YAML frontmatter、iframe、图片嵌入、wikilink、callout、
   HTML 标签、代码块标记等，保留可检索的正文文本。
3. 按标题（Heading）结构 + 最大长度 + 重叠切分片段，返回 Chunk 列表。

片段元数据保留 文件相对路径 / 标题路径 / 片段序号，供生成阶段引用来源。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---- Obsidian / Markdown 语法清洗 ----

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL | re.MULTILINE)
_IFRAME_RE = re.compile(r"<iframe[^>]*>.*?</iframe>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_IMAGE_EMBED_RE = re.compile(r"!\[\[[^\]]+\]\]")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_CALLOUT_RE = re.compile(r"^>\s*\[!([a-zA-Z]+)\]\s*(.*)$", re.MULTILINE)
_BLOCKQUOTE_MARK_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # [文本](url) -> 文本
_URL_RE = re.compile(r"https?://\S+")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def clean_markdown(text: str) -> str:
    """把一篇 Obsidian 笔记清洗为纯文本（保留标题层级作为分片依据）。"""
    text = _FRONTMATTER_RE.sub("", text)
    text = _IFRAME_RE.sub(" ", text)
    text = _IMAGE_EMBED_RE.sub(" ", text)
    text = _CODE_FENCE_RE.sub(lambda m: m.group(1), text)  # 代码保留正文
    text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = _WIKILINK_RE.sub(lambda m: m.group(2) if m.group(2) else m.group(1), text)
    text = _CALLOUT_RE.sub(lambda m: m.group(2), text)
    text = _BLOCKQUOTE_MARK_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()


@dataclass
class Chunk:
    """一个可检索的文本片段。"""

    id: int
    text: str
    file: str          # 相对知识库根的文件路径（含扩展名）
    heading: str = ""  # 所在标题路径，如 "RAG 工作机制详解 > 整体流程"
    source: str = ""   # 展示用来源，如 "我的笔记/agent 学习/RAG 工作机制详解…md#整体流程"

    def __post_init__(self) -> None:
        if self.heading:
            self.source = f"{self.file}#{self.heading}"

    @property
    def brief(self) -> str:
        return self.text[:80].replace("\n", " ")

    @property
    def search_text(self) -> str:
        """用于向量/BM25 检索的文本：标题 + 正文（标题是关键检索信号）。"""
        return f"{self.heading}\n{self.text}" if self.heading else self.text


def _collect_md_files(root: Path, exclude_dirs: list[str], exclude_files: list[str]) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in exclude_dirs for part in rel.parts[:-1]):
            continue
        if path.name in exclude_files:
            continue
        files.append(path)
    return files


def _split_by_headings(cleaned: str) -> list[tuple[int, str, str]]:
    """按标题切分，返回 [(level, heading_path, body)]。

    heading_path 用栈追踪标题层级生成，如 "RAG 机制 > 整体流程 > 流程图解"。
    一级标题（#）作为文档标题参与路径。
    """
    lines = cleaned.splitlines()
    sections: list[tuple[int, str, str]] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    buf: list[str] = []
    cur_level, cur_path = 0, ""

    def flush() -> None:
        if buf:
            body = "\n".join(buf).strip()
            if body:
                sections.append((cur_level, cur_path, body))
            buf.clear()

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            # 弹出所有层级 >= 当前标题的祖先，再压入当前标题
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_level, cur_path = level, " > ".join(t for _, t in stack)
        else:
            buf.append(line)
    flush()
    return [(lv, p, bo) for lv, p, bo in sections if bo]


def _split_long_text(body: str, max_chars: int, overlap: int) -> list[str]:
    """超过 max_chars 的正文按段落滑窗切分，带 overlap。"""
    if len(body) <= max_chars:
        return [body]
    paras = [p.strip() for p in body.split("\n") if p.strip()]
    pieces: list[str] = []
    cur = ""
    for p in paras:
        if len(p) > max_chars:  # 超长段落按字符硬切
            if cur:
                pieces.append(cur)
                cur = ""
            for i in range(0, len(p), max_chars - overlap):
                pieces.append(p[i : i + max_chars])
            continue
        if cur and len(cur) + len(p) + 1 > max_chars:
            pieces.append(cur)
            cur = cur[-overlap:] if overlap else ""
        cur = (cur + "\n" + p).strip()
    if cur:
        pieces.append(cur)
    return [p for p in pieces if p]


# ---------------- 父子分块（Parent-Child Chunking） ----------------
# 父块 = 标题章节（供生成使用，上下文完整）
# 叶子 = 章节内的短片段（供索引召回，粒度精确）

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*")


@dataclass
class Leaf:
    """索引粒度的短片段，指向其父块。"""

    id: int
    text: str
    file: str
    heading: str = ""
    source: str = ""
    parent_idx: int = -1  # 父块在 parents 列表中的下标


def split_leaves(text: str, max_leaf: int = 320, min_leaf: int = 24) -> list[str]:
    """把父块文本切成叶子：按段落 → 超长段落按句子切 → 小段并入前一段。"""
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    leaves: list[str] = []
    for para in paras:
        if len(para) <= max_leaf:
            leaves.append(para)
            continue
        # 超长：按句子边界切
        pieces = [p.strip() for p in _SENT_SPLIT_RE.split(para) if p.strip()]
        cur = ""
        for piece in pieces:
            if cur and len(cur) + len(piece) + 1 > max_leaf:
                leaves.append(cur)
                cur = piece
            else:
                cur = (cur + " " + piece).strip() if cur else piece
        if cur:
            leaves.append(cur)
    # 合并过短叶子（避免检索噪声）
    merged: list[str] = []
    for leaf in leaves:
        if merged and len(leaf) < min_leaf:
            merged[-1] = merged[-1] + " " + leaf
        else:
            merged.append(leaf)
    return [l for l in merged if l]


def load_chunks_with_leaves(root: Path, config) -> tuple[list[Chunk], list[Leaf]]:
    """返回 (父块列表, 叶子列表)。叶子与父块一一对应（parent_idx）。"""
    files = _collect_md_files(root, config.chunk_exclude_dirs, config.chunk_exclude_files)
    parents: list[Chunk] = []
    leaves: list[Leaf] = []
    for file in files:
        try:
            raw = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cleaned = clean_markdown(raw)
        if not cleaned:
            continue
        sections = _split_by_headings(cleaned)
        if not sections:
            continue
        for level, heading_path, body in sections:
            if len(body) < config.chunk_min_chars:
                continue
            for piece in _split_long_text(body, config.chunk_max_chars, config.chunk_overlap):
                rel = str(file.relative_to(root))
                parent_idx = len(parents)
                parent = Chunk(id=parent_idx, text=piece, file=rel, heading=heading_path, source="")
                parents.append(parent)
                for leaf_text in split_leaves(piece):
                    leaves.append(
                        Leaf(
                            id=len(leaves),
                            text=leaf_text,
                            file=rel,
                            heading=heading_path,
                            source="",
                            parent_idx=parent_idx,
                        )
                    )
    return parents, leaves


def load_chunks(root: Path, config) -> list[Chunk]:
    """兼容入口：只返回父块（等价于旧的 load_chunks 行为）。"""
    parents, _ = load_chunks_with_leaves(root, config)
    return parents


def summarize_corpus(chunks: list[Chunk]) -> str:
    """构建索引后的简要统计，用于日志。"""
    files = len({c.file for c in chunks})
    chars = sum(len(c.text) for c in chunks)
    return f"{len(chunks)} chunks / {files} files / {chars:,} chars"
