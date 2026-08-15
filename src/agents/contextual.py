"""上下文增强（Anthropic Contextual Retrieval 策略）。

问题：分块后的片段脱离文档语境（如片段只说"召回速度快"），查询提到文档主题时
检索不到。解法：索引时让 LLM 为每个父块生成一句上下文摘要，把
"这篇文档是什么、该片段在讲什么"补进检索文本（Embedding 输入 + BM25 索引），
使片段自包含。参考 Anthropic 2024-09 工程博客 "Contextual Retrieval"。

实现：
- 按文件分批调用 LLM（每批 ≤15 个片段，返回 JSON 映射），控制成本与延迟
- 结果按 文件+片段文本哈希 缓存到 data/contexts.json，重建索引时复用
- 仅索引期调用（build_index --augment），检索期零开销
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .chunking import Chunk

BATCH_SIZE = 15

CONTEXT_SYSTEM = """你是文档上下文标注器。给定一篇文档和它的若干片段，为每个片段生成一句中文上下文说明（≤60字），
说明这段内容在整篇文档中的位置与主题，让片段脱离原文也能被独立检索到。
格式要求：只输出一个 JSON 对象，键为片段编号（字符串），值为上下文说明，不要输出其他内容。"""


def _context_prompt(file: str, chunks: list[Chunk]) -> list[dict]:
    lines = []
    for i, c in enumerate(chunks):
        head = c.text[:180].replace("\n", " ")
        lines.append(f"[{i}] {head}")
    user = (
        f"文档：{file}\n\n片段列表：\n" + "\n".join(lines) +
        "\n\n为每个片段生成上下文说明，输出 JSON：{\"0\": \"...\", \"1\": \"...\"}"
    )
    return [
        {"role": "system", "content": CONTEXT_SYSTEM},
        {"role": "user", "content": user},
    ]


def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def augment_contexts(llm, parents: list[Chunk], data_dir: Path, verbose: bool = False) -> dict[int, str]:
    """为每个父块生成/复用上下文，返回 {parent_idx: context}。"""
    cache_path = data_dir / "contexts.json"
    cache = _load_cache(cache_path)

    # 按文件分组
    by_file: dict[str, list[int]] = {}
    for idx, c in enumerate(parents):
        by_file.setdefault(c.file, []).append(idx)

    result: dict[int, str] = {}
    new_entries: dict[str, str] = {}
    for file, idxs in by_file.items():
        # 拆批
        for start in range(0, len(idxs), BATCH_SIZE):
            batch = idxs[start : start + BATCH_SIZE]
            batch_chunks = [parents[i] for i in batch]
            keys = [f"{file}|{_text_hash(c.text)}" for c in batch_chunks]
            # 全部命中缓存则直接复用
            if all(k in cache for k in keys):
                for i, k in zip(batch, keys):
                    result[i] = cache[k]
                continue
            try:
                obj = llm.chat_json(_context_prompt(file, batch_chunks), temperature=0.2, max_tokens=1200)
            except Exception as exc:  # noqa: BLE001 - 增强失败不影响索引
                print(f"  [context] ⚠ {file} 批次失败: {exc}")
                continue
            if not isinstance(obj, dict):
                continue
            for i, k in zip(batch, keys):
                # LLM 输出键为批内相对编号 [0..n]；容错尝试绝对编号
                ctx = obj.get(str(i - batch[0])) or obj.get(str(i))
                if isinstance(ctx, str) and ctx.strip():
                    result[i] = ctx.strip()
                    new_entries[k] = ctx.strip()
    if new_entries:
        cache.update(new_entries)
        _save_cache(cache_path, cache)
    if verbose:
        print(f"  [context] 已为 {len(result)}/{len(parents)} 个片段生成上下文")
    return result
