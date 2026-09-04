"""S4 长期记忆 + 摘要压缩/遗忘 + 实体记忆测试。

全部离线：LLM 用记录调用次数、按脚本返回文本的假客户端（子类化 LLMClient
覆写 _chat，不发真实请求）；向量后端用内置 TfidfHashEmbeddingBackend 与确定性
假后端；存储目录用 tempfile 临时目录。覆盖 spec ACC-S4-01..04，以及淘汰语义、
排除语义、原子持久化 roundtrip、planner 注入与 Agent.ask 级集成。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent import Agent
from agents.config import Config
from agents.embeddings import TfidfHashEmbeddingBackend
from agents.llm import ChatResult, LLMClient, LLMError
from agents.long_memory import (
    EntityMemory,
    LongTermMemory,
    MemoryCompressor,
    MemoryEpisode,
)
from agents.memory import SessionMemory, Turn
from agents.planner import build_planner_prompt
from agents.tools import Tool, ToolContext


class ScriptedLLM(LLMClient):
    """离线假 LLM：按脚本顺序返回文本，记录每次调用与收到的 messages；可指定第 N 次调用抛错。"""

    def __init__(self, texts: list[str], config: Config, raise_on: set[int] | None = None):
        self.config = config
        self._texts = list(texts)
        self.calls = 0
        self.seen: list[list[dict]] = []
        self._raise_on = set(raise_on or ())

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        if self.calls in self._raise_on:
            raise LLMError("模拟 LLM 故障")
        self.seen.append([dict(m) for m in messages])
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


class _HashBackend:
    """确定性字符哈希后端（非 tfidf 分支）：验证增量向量追加与淘汰行对齐。"""

    name = "fakehash"

    def __init__(self, dim: int = 32):
        self.dim = dim

    def _vec(self, text: str):
        v = np.zeros(self.dim, dtype=np.float32)
        for ch in text:
            v[ord(ch) % self.dim] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_texts(self, texts: list[str]):
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, query: str):
        return self._vec(query)


class _OtherBackend:
    """名字不同的假后端：验证 load 时 backend 名不一致 → 重建空库。"""

    name = "other"
    dim = 8


def _make_tool(name: str, func, properties: dict, required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=f"测试工具 {name}",
        parameters={"type": "object", "properties": properties, "required": required},
        func=func,
    )


PLAN_JSON = json.dumps(
    {
        "reasoning": "测试规划",
        "plan_summary": "检索一步",
        "steps": [{"action": "search_knowledge_base", "input": {"query": "RAG"}, "purpose": ""}],
    },
    ensure_ascii=False,
)
REFLECT_JSON = json.dumps({"need_more": False, "reasoning": "信息足够", "steps": []}, ensure_ascii=False)


def _probe_tool() -> Tool:
    def probe(ctx, query=""):
        return {"text": f"检索 {query} 的片段", "sources": ["kb://a.md"]}

    return _make_tool("search_knowledge_base", probe, {"query": {"type": "string"}}, ["query"])


def _make_agent(td: str, llm: ScriptedLLM, tools: dict[str, Tool], overrides: dict | None = None) -> Agent:
    """离线 Agent：tmp data_dir、tfidf 记忆后端、不加载真实索引。

    G3 起 memory.* 默认关闭；本套件测记忆机制本身，故显式开启两个记忆开关
    （test_agent_switches_disabled 用 overrides 显式关闭覆盖）。
    """
    raw = {"data_dir": td,
           "memory": {"embedding_backend": "tfidf", "max_episodes": 50,
                      "long_term_enabled": True, "entities_enabled": True},
           "tools": {"max_retries": 0}}
    for section, values in (overrides or {}).items():
        raw.setdefault(section, {}).update(values)
    agent = Agent(Config(raw), llm=llm, lazy_index=True)
    agent._index_loaded = True  # 跳过索引加载
    agent.tools = tools
    agent._ctx = ToolContext()
    return agent


class LongMemoryTests(unittest.TestCase):
    """spec ACC-S4-01..04：语义检索/淘汰/摘要压缩/实体记忆，及排除、持久化、planner 注入、ask 集成。"""

    # ---------------- ACC-S4-01 语义检索 ----------------

    def test_acc_s4_01_semantic_search(self):
        """ACC-S4-01：写入 3 条不同主题 episode，search 相关查询 top1 是共享关键词最多条目。"""
        with tempfile.TemporaryDirectory() as td:
            ltm = LongTermMemory(Path(td), TfidfHashEmbeddingBackend(hash_dim=256), max_episodes=10)
            ltm.add(MemoryEpisode(id="e1", session_id="s1", ts="2026-08-01T10:00:00",
                                  question="RAG 检索增强生成的流程", answer_summary="RAG 先检索知识库再用大模型生成答案"))
            ltm.add(MemoryEpisode(id="e2", session_id="s1", ts="2026-08-02T10:00:00",
                                  question="向量数据库的原理", answer_summary="向量数据库用余弦相似度做近邻检索"))
            ltm.add(MemoryEpisode(id="e3", session_id="s1", ts="2026-08-03T10:00:00",
                                  question="今天午餐吃什么", answer_summary="推荐面条和沙拉"))

            hits = ltm.search("RAG 检索增强生成 怎么做", k=3)
            self.assertEqual(hits[0].id, "e1", f"top1 应为语义最近条目 e1，实际 {hits[0].id}")
            self.assertEqual(hits[0].hits, 1, "命中者 hits 应 +1")

            # 换主题同样命中
            hits2 = ltm.search("午餐 吃什么", k=1)
            self.assertEqual(hits2[0].id, "e3")

        print("✓ ACC-S4-01 相关查询 top1 命中共享关键词最多的 episode（hits+1）")

    # ---------------- ACC-S4-02 淘汰语义 ----------------

    def test_acc_s4_02_prune_semantics(self):
        """ACC-S4-02：max_episodes=3、写入 5 条（第 1 条 hits 高），prune 后保留 hits 高/新者。"""
        with tempfile.TemporaryDirectory() as td:
            ltm = LongTermMemory(Path(td), TfidfHashEmbeddingBackend(hash_dim=128), max_episodes=10)
            for i in range(1, 6):
                ltm.add(MemoryEpisode(id=f"e{i}", session_id="s", ts=f"2026-08-0{i}T10:00:00",
                                      question=f"主题{i}", answer_summary=f"内容{i}"))
            self.assertEqual(ltm.size, 5)
            ltm.episodes[0].hits = 3  # 第 1 条 hits 高
            ltm.max_episodes = 3
            evicted = ltm.prune()

            self.assertEqual(
                [e.id for e in ltm.episodes], ["e1", "e4", "e5"],
                f"应保留 hits 高的 e1 与最新的 e4/e5，实际 {[e.id for e in ltm.episodes]}",
            )
            self.assertEqual({e.id for e in evicted}, {"e2", "e3"}, "被淘汰的应是 hits 低且最旧的 e2/e3")
            # 淘汰后 TF-IDF 整体重建（re-fit），检索仍正常
            self.assertEqual(ltm.search("主题5", k=1)[0].id, "e5")

        print("✓ ACC-S4-02 prune 按 (hits 升序, ts 旧优先) 淘汰，保留 3 条且检索可用")

    # ---------------- 排除语义与 as_context 渲染 ----------------

    def test_exclude_and_as_context(self):
        """exclude_session 排除全部 / exclude_session_recent 排除最近 n 条；as_context 渲染与空结果。"""
        with tempfile.TemporaryDirectory() as td:
            ltm = LongTermMemory(Path(td), TfidfHashEmbeddingBackend(hash_dim=128), max_episodes=10)
            ltm.add(MemoryEpisode(id="e1", session_id="sA", ts="2026-08-01T10:00:00", question="苹果公司股价", answer_summary="涨了"))
            ltm.add(MemoryEpisode(id="e2", session_id="sA", ts="2026-08-02T10:00:00", question="苹果新品发布会", answer_summary="发布了新手机"))
            ltm.add(MemoryEpisode(id="e3", session_id="sB", ts="2026-08-03T10:00:00", question="香蕉价格行情", answer_summary="稳定"))

            # (sA, 1)：排除 sA 最近 1 条（ts 最大的 e2），e1 与 sB 的 e3 仍可召回
            ids = [h.id for h in ltm.search("苹果 发布会", k=5, exclude_session_recent=("sA", 1))]
            self.assertNotIn("e2", ids)
            self.assertIn("e1", ids)
            self.assertIn("e3", ids)
            # (sA, 2)：排除 sA 全部
            ids2 = [h.id for h in ltm.search("苹果", k=5, exclude_session_recent=("sA", 2))]
            self.assertNotIn("e1", ids2)
            self.assertNotIn("e2", ids2)
            self.assertIn("e3", ids2)
            # exclude_session：排除该会话全部 episode
            hits3 = ltm.search("苹果", k=5, exclude_session="sA")
            self.assertTrue(all(h.session_id != "sA" for h in hits3))
            self.assertEqual([h.id for h in hits3], ["e3"])

            block = ltm.as_context("苹果 发布会", k=2, exclude_session_recent=("sA", 1))
            self.assertTrue(block.startswith("相关历史经验："))
            self.assertIn("[1] Q: 苹果公司股价", block)
            self.assertIn("A: 涨了", block)

            # 空库 / 全部被排除 → 空串
            empty = LongTermMemory(Path(td) / "none", TfidfHashEmbeddingBackend(hash_dim=64))
            self.assertEqual(empty.as_context("任何问题", k=3), "")
            self.assertEqual(ltm.as_context("苹果", k=3, exclude_session="sA", exclude_session_recent=("sB", 1)), "")

        print("✓ exclude_session / exclude_session_recent=(sid, n) 语义正确，as_context 渲染与空串")

    # ---------------- 原子持久化 roundtrip ----------------

    def test_persistence_roundtrip_and_backend_mismatch(self):
        """三个文件原子落盘（无 tmp 残留）；重载后条目/hits/检索一致；backend 不一致 → 空库。"""
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "mem"
            ltm = LongTermMemory(store, TfidfHashEmbeddingBackend(hash_dim=128), max_episodes=10)
            ltm.add(MemoryEpisode(id="e1", session_id="s1", ts="t1", question="RAG 流程", answer_summary="先检索后生成"))
            ltm.add(MemoryEpisode(id="e2", session_id="s1", ts="t2", question="向量库原理", answer_summary="余弦相似度"))
            ltm.search("RAG 流程", k=1)

            self.assertTrue((store / "episodes.json").exists())
            self.assertTrue((store / "vectors.npy").exists())
            self.assertTrue((store / "meta.json").exists())
            self.assertFalse(list(store.glob("*.tmp")), "原子替换后不应残留 tmp 文件")

            ltm2 = LongTermMemory(store, TfidfHashEmbeddingBackend(hash_dim=128), max_episodes=10)
            self.assertEqual([e.id for e in ltm2.episodes], ["e1", "e2"], "重载后条目一致")
            self.assertEqual(ltm2.episodes[0].hits, 1, "hits 计数应随 episodes.json 持久化")
            self.assertEqual(ltm2.search("RAG 流程", k=1)[0].id, "e1", "重载后检索仍可用")

            # backend 名不一致 → 向量不可比 → 重建空库
            ltm3 = LongTermMemory(store, _OtherBackend(), max_episodes=10)
            self.assertEqual(ltm3.episodes, [])

            # episodes.json 损坏 → 空库不崩溃
            bad = Path(td) / "bad"
            bad.mkdir()
            (bad / "episodes.json").write_text("{{{不是JSON", encoding="utf-8")
            self.assertEqual(LongTermMemory(bad, TfidfHashEmbeddingBackend(hash_dim=64)).episodes, [])

        print("✓ 原子持久化 roundtrip（无 tmp 残留 / hits 保留 / backend 不一致与损坏文件 → 空库）")

    def test_non_tfidf_backend_alignment(self):
        """非 TF-IDF 后端：增量向量与淘汰行对齐（np.delete），持久化 roundtrip 一致。"""
        with tempfile.TemporaryDirectory() as td:
            store = Path(td)
            ltm = LongTermMemory(store, _HashBackend(), max_episodes=3)
            for i in range(1, 6):
                ltm.add(MemoryEpisode(id=f"e{i}", session_id="s", ts=f"t{i}", question=f"主题{i}", answer_summary=""))
            self.assertEqual([e.id for e in ltm.episodes], ["e3", "e4", "e5"], "超容量自动淘汰最旧两条")
            self.assertEqual(ltm._vectors.shape[0], ltm.size, "向量行数应与条目数对齐")
            self.assertEqual(ltm.search("主题5", k=1)[0].id, "e5", "淘汰后检索仍对齐")

            ltm2 = LongTermMemory(store, _HashBackend(), max_episodes=3)
            self.assertEqual([e.id for e in ltm2.episodes], ["e3", "e4", "e5"])
            self.assertEqual(ltm2.search("主题5", k=1)[0].id, "e5")

        print("✓ 非 TF-IDF 后端增量向量/淘汰对齐与 roundtrip")

    # ---------------- 摘要压缩（ACC-S4-03） ----------------

    def test_memory_compressor_normal_and_fallback(self):
        """MemoryCompressor：LLM 正常 → 摘要文本（≤300）；LLM 抛错 → 降级拼接（≤600，首句截断）。"""
        cfg = Config({})
        turns = [
            Turn(question="什么是 RAG？", answer="RAG 是检索增强生成。它先检索再生成。"),
            Turn(question="MCP 呢？", answer="MCP 是模型上下文协议！"),
        ]
        llm = ScriptedLLM(["合并后的摘要"], cfg)
        out = MemoryCompressor(llm).compress("旧摘要内容", turns)
        self.assertEqual(out, "合并后的摘要")
        self.assertLessEqual(len(out), 300)
        user = llm.seen[0][1]["content"]
        self.assertIn("旧摘要内容", user, "prompt 应携带旧摘要与各轮问答")
        self.assertIn("什么是 RAG？", user, "prompt 应携带旧摘要与各轮问答")
        self.assertIn("MCP 呢？", user, "prompt 应携带旧摘要与各轮问答")

        class _BoomLLM(LLMClient):
            def __init__(self, config):
                self.config = config

            def _chat(self, messages, **kwargs):
                raise LLMError("网络中断")

        out2 = MemoryCompressor(_BoomLLM(cfg)).compress("旧摘要。", turns)
        self.assertTrue(out2.startswith("旧摘要。"), "降级拼接应包含旧摘要")
        self.assertIn("问：什么是 RAG／答：RAG 是检索增强生成", out2)
        self.assertNotIn("它先检索再生成", out2)
        self.assertLessEqual(len(out2), 600)

        # LLM 输出超长 → 截断到 300
        self.assertEqual(len(MemoryCompressor(ScriptedLLM(["长" * 400], cfg)).compress("", turns)), 300)

        print("✓ MemoryCompressor 正常/抛错两路（≤300 摘要 / ≤600 降级拼接）")

    def test_acc_s4_03_session_memory_compress(self):
        """ACC-S4-03：maybe_compress 正常时 summary 为 LLM 文本、异常时为降级拼接；均不抛异常且 as_text 含摘要块。"""
        cfg = Config({})
        # 正常路：LLM 摘要生效，窗口保留 max_turns 轮
        mem = SessionMemory(max_turns=2)
        mem.add("q1", "a1", [], "")
        mem.add("q2", "a2", [], "")
        mem.add("q3", "a3", [], "")  # q1 滑出（add 截断时停放待压缩）
        llm = ScriptedLLM(["这是压缩后的摘要"], cfg)
        summary = mem.maybe_compress(llm)
        self.assertEqual(summary, "这是压缩后的摘要")
        self.assertEqual(mem.summary, summary)
        self.assertEqual([t.question for t in mem.turns], ["q2", "q3"], "窗口内轮次保留")
        text = mem.as_text()
        self.assertTrue(text.startswith("此前对话摘要：这是压缩后的摘要"), f"as_text 应含摘要块: {text!r}")
        self.assertIn("用户：q2", text)
        self.assertIn("用户：q3", text)

        # 再次溢出：旧摘要作为输入传给压缩器
        mem.add("q4", "a4", [], "")  # q2 滑出
        llm2 = ScriptedLLM(["第二次摘要"], cfg)
        mem.maybe_compress(llm2)
        self.assertIn("这是压缩后的摘要", llm2.seen[0][1]["content"], "旧摘要应传入压缩 prompt")
        self.assertEqual(mem.summary, "第二次摘要")

        # 异常路：compressor 抛错 → 降级拼接，不抛异常
        mem3 = SessionMemory(max_turns=1)
        mem3.add("问题甲？", "答案甲。第二句", [], "")
        mem3.add("问题乙", "答案乙", [], "")

        class _BoomCompressor:
            def compress(self, old, evicted):
                raise RuntimeError("boom")

        s = mem3.maybe_compress(compressor=_BoomCompressor())
        self.assertIn("问题甲", s)
        self.assertIn("答案甲", s)
        self.assertLessEqual(len(s), 600)
        self.assertTrue(mem3.as_text().startswith("此前对话摘要："))

        # 无溢出 → 不调用 LLM
        llm3 = ScriptedLLM(["不该被用到"], cfg)
        SessionMemory(max_turns=6).maybe_compress(llm3)
        self.assertEqual(llm3.calls, 0)

        print("✓ ACC-S4-03 maybe_compress 正常（LLM 文本）/异常（降级拼接）两路，as_text 含摘要块")

    # ---------------- 实体记忆（ACC-S4-04） ----------------

    def test_acc_s4_04_entity_extract_robustness(self):
        """ACC-S4-04：实体抽取 LLM 抛错/返回坏 JSON → 返回 {} 不崩溃；正常抽取含过滤。"""
        cfg = Config({})
        self.assertEqual(EntityMemory().extract(ScriptedLLM(["not json", "still not json"], cfg), "q", "a"), {})
        self.assertEqual(EntityMemory().extract(ScriptedLLM(['{"foo": 1}'], cfg), "q", "a"), {})          # 缺 entities
        self.assertEqual(EntityMemory().extract(ScriptedLLM(['{"entities": [1, 2]}'], cfg), "q", "a"), {})  # entities 非 dict

        class _BoomLLM(LLMClient):
            def __init__(self, config):
                self.config = config

            def _chat(self, messages, **kwargs):
                raise LLMError("down")

        self.assertEqual(EntityMemory().extract(_BoomLLM(cfg), "q", "a"), {})

        payload = json.dumps(
            {"entities": {"RAG": "检索增强生成", "x" * 31: "超长名丢弃", "": "空名丢弃", "坏": "", "MCP": "模型上下文协议"}},
            ensure_ascii=False,
        )
        out = EntityMemory().extract(ScriptedLLM([payload], cfg), "什么是 RAG 和 MCP", "RAG 是…MCP 是…")
        self.assertEqual(out, {"RAG": "检索增强生成", "MCP": "模型上下文协议"}, f"超长名/空名/空事实应丢弃: {out}")

        payload6 = json.dumps({"entities": {f"实{i}": "事实" for i in range(7)}}, ensure_ascii=False)
        self.assertEqual(len(EntityMemory().extract(ScriptedLLM([payload6], cfg), "q", "a")), 5, "实体数上限 5")

        print("✓ ACC-S4-04 实体抽取 抛错/坏 JSON → {}；正常抽取过滤超长与空值、上限 5")

    def test_entity_memory_merge_prompt_save_load(self):
        """merge 去重与 last_seen 刷新；as_prompt 按 last_seen 倒序与 limit；save/load roundtrip。"""
        with tempfile.TemporaryDirectory() as td:
            store = Path(td)
            em = EntityMemory(store)
            self.assertEqual(em.as_prompt(), "")

            em.merge({"RAG": "检索增强生成"})
            em.merge({"MCP": "模型上下文协议"})
            em.merge({"MCP": "模型上下文协议"})  # 重复事实不重复追加
            self.assertEqual(len(em.entities["MCP"]["facts"]), 1)
            em.entities["RAG"]["last_seen"] = "2026-08-01T10:00:00"
            em.entities["MCP"]["last_seen"] = "2026-08-02T10:00:00"

            prompt = em.as_prompt()
            self.assertTrue(prompt.startswith("已知实体："))
            self.assertLess(prompt.index("MCP"), prompt.index("RAG"), "应按 last_seen 倒序")
            self.assertIn("（最近提及 2026-08-02T10:00:00）", prompt)

            em.merge({f"n{i}": "事实" for i in range(10)})
            self.assertEqual(len(em.as_prompt(limit=3).splitlines()), 4, "标题 + limit 行")

            em.save()
            em2 = EntityMemory(store)
            em2.load()
            self.assertEqual(em2.entities, em.entities, "save/load roundtrip 一致")

            missing = EntityMemory(store / "none")
            missing.load()
            self.assertEqual(missing.entities, {})

        print("✓ EntityMemory merge/as_prompt/save/load（倒序、limit、roundtrip、缺文件空库）")

    # ---------------- 配置与 planner 注入 ----------------

    def test_config_memory_defaults(self):
        """memory.* 四个配置项的默认值与覆盖（G3/P0-3：两个记忆开关默认关闭）。"""
        cfg = Config({})
        self.assertIs(cfg.memory_long_term_enabled, False)
        self.assertIs(cfg.memory_entities_enabled, False)
        self.assertEqual(cfg.memory_max_episodes, 200)
        self.assertEqual(cfg.memory_embedding_backend, "auto")
        cfg2 = Config({"memory": {"long_term_enabled": True, "entities_enabled": True,
                                  "max_episodes": 50, "embedding_backend": "tfidf"}})
        self.assertIs(cfg2.memory_long_term_enabled, True)
        self.assertIs(cfg2.memory_entities_enabled, True)
        self.assertEqual(cfg2.memory_max_episodes, 50)
        self.assertEqual(cfg2.memory_embedding_backend, "tfidf")

        print("✓ config memory.* 开关默认关（G3/P0-3）、默认 200 条、默认 auto，可被 config.yaml 覆盖")

    def test_planner_prompt_longterm_injection(self):
        """build_planner_prompt 注入 longterm_text 块；为空时不注入。"""
        msgs = build_planner_prompt("问题", ["工具A"], 5, "历史", "相关历史经验：\n[1] Q: x\n    A: y")
        user = msgs[1]["content"]
        self.assertIn("相关历史经验：", user)
        self.assertIn("[1] Q: x", user)
        self.assertLess(user.index("对话历史"), user.index("相关历史经验"))
        self.assertLess(user.index("相关历史经验"), user.index("可用工具"))
        msgs2 = build_planner_prompt("问题", ["工具A"], 5)
        self.assertNotIn("相关历史经验", msgs2[1]["content"])

        print("✓ planner prompt 注入相关历史经验块（空串不注入）")

    # ---------------- Agent.ask 级集成 ----------------

    def test_agent_ask_long_term_integration(self):
        """ask 集成：成功后写 episode/实体；第二次 ask 规划 prompt 收到召回的历史经验。"""
        with tempfile.TemporaryDirectory() as td:
            entity_json = json.dumps({"entities": {"RAG": "检索增强生成"}}, ensure_ascii=False)
            llm = ScriptedLLM(
                [
                    PLAN_JSON, REFLECT_JSON, "最终答案[1]", entity_json,  # 第一次 ask：规划/反思/合成/实体
                    "RAG 检索增强生成",                                    # 第二次 ask：追问改写
                    PLAN_JSON, REFLECT_JSON, "第二次回答[1]", entity_json,  # 规划/反思/合成/实体
                ],
                Config({}),
            )
            agent = _make_agent(td, llm, {"search_knowledge_base": _probe_tool()})

            a1 = agent.ask("什么是 RAG", session_id="s1")
            self.assertTrue(a1.ok, f"第一次 ask 不应失败: {a1.error}")
            lm = agent.long_memory
            self.assertIsNotNone(lm)
            self.assertEqual(lm.size, 1, "成功回答应写入 1 条 episode")
            ep = lm.episodes[0]
            self.assertEqual(ep.session_id, "s1")
            self.assertEqual(ep.question, "什么是 RAG")
            self.assertTrue(ep.answer_summary.startswith("最终答案"))
            self.assertEqual(ep.entities, {"RAG": "检索增强生成"})
            self.assertEqual(ep.sources, ["kb://a.md"])
            self.assertTrue((Path(td) / "memory" / "episodes.json").exists())
            self.assertTrue((Path(td) / "memory" / "entities.json").exists())
            self.assertEqual(agent.entity_memory.entities["RAG"]["facts"], ["检索增强生成"])

            a2 = agent.ask("再讲讲 RAG", session_id="s2")
            self.assertTrue(a2.ok)
            self.assertEqual(a2.final_answer, "第二次回答[1]")
            plan_user = llm.seen[5][1]["content"]  # 第二次 ask 的规划 prompt（0规划 1反思 2合成 3实体 4改写 5规划）
            self.assertIn("相关历史经验：", plan_user)
            self.assertIn("Q: 什么是 RAG", plan_user)
            self.assertEqual(lm.size, 2)
            self.assertEqual(lm.episodes[0].hits, 1, "第一次召回命中应使 hits+1 并持久化")
            self.assertEqual(llm.calls, 9, f"两次 ask 共 9 次 LLM 调用（含实体抽取），实际 {llm.calls}")

        print("✓ Agent.ask 集成：episode/实体写入与持久化、规划 prompt 收到召回历史经验、hits 计数")

    def test_agent_ask_failure_no_episode(self):
        """失败回答（合成抛 LLMError）不写 episode、不抽实体、不写会话记忆。"""
        with tempfile.TemporaryDirectory() as td:
            llm = ScriptedLLM([PLAN_JSON, REFLECT_JSON, "x"], Config({}), raise_on={3})
            agent = _make_agent(td, llm, {"search_knowledge_base": _probe_tool()})

            a = agent.ask("问题", session_id="s1")
            self.assertFalse(a.ok)
            self.assertEqual(agent.long_memory.size, 0)
            self.assertEqual(agent.memory.count, 0)
            self.assertFalse((Path(td) / "memory" / "episodes.json").exists())
            self.assertFalse((Path(td) / "memory" / "entities.json").exists())

        print("✓ 失败回答不写长期记忆/实体/会话记忆")

    def test_agent_switches_disabled(self):
        """开关关闭：long_memory/entity_memory 均不建，ask（带 session_id）也无实体抽取调用。"""
        with tempfile.TemporaryDirectory() as td:
            llm = ScriptedLLM([PLAN_JSON, REFLECT_JSON, "答案"], Config({}))
            agent = _make_agent(td, llm, {"search_knowledge_base": _probe_tool()},
                                {"memory": {"long_term_enabled": False, "entities_enabled": False}})
            self.assertIsNone(agent.long_memory)
            self.assertIsNone(agent.entity_memory)

            a = agent.ask("问题", session_id="s1")
            self.assertTrue(a.ok)
            self.assertEqual(agent.memory.count, 1)
            self.assertEqual(llm.calls, 3, f"关闭记忆增强后只有规划/反思/合成 3 次调用，实际 {llm.calls}")
            self.assertFalse((Path(td) / "memory").exists())

        print("✓ memory.* 开关关闭：不建库、无额外 LLM 调用、无落盘")

    def test_agent_no_session_no_side_effects(self):
        """无 session_id 调用（向后兼容）：成功回答也不读写长期记忆、无实体抽取调用。"""
        with tempfile.TemporaryDirectory() as td:
            llm = ScriptedLLM([PLAN_JSON, REFLECT_JSON, "答案"], Config({}))
            agent = _make_agent(td, llm, {"search_knowledge_base": _probe_tool()})

            a = agent.ask("问题")
            self.assertTrue(a.ok)
            self.assertEqual(llm.calls, 3, f"无会话时不应有实体抽取调用，实际 {llm.calls}")
            self.assertFalse((Path(td) / "memory").exists())
            self.assertNotIn("相关历史经验", llm.seen[0][1]["content"])
            self.assertEqual(agent.long_memory.size, 0)  # 惰性建库为空，且未产生落盘
            self.assertFalse((Path(td) / "memory" / "episodes.json").exists())

        print("✓ 无 session_id 调用保持原有行为（无 S4 副作用，规划 prompt 无历史经验块）")


if __name__ == "__main__":
    unittest.main()
