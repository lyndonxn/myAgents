"""S5 任务状态持久化 + 暂停/恢复测试（含 T1 完成回调写回）。

全部离线：LLM 用记录调用次数、按脚本返回文本的假客户端（子类化 LLMClient 覆写
_chat，不发真实请求）；Agent 不加载真实索引（注入假工具注册表）；工具用可门控
闭包——threading.Event 控制每步放行，确定性地观察每一步的中间持久化状态；
TaskStore 用 tempfile 临时目录。等待一律用「事件/轮询 + 超时」，不依赖时序猜测。
覆盖 spec ACC-S5-01..04，以及 failed 路径（步骤级失败节点 / 任务级失败）、
list() 排序、canceled 终态保护、序列化往返与 TaskStore 原子落盘。
T1 部分（ACC-T1-01..03）：completed 任务经 on_complete 写回 WebStore 会话消息、
failed/canceled/paused 不回调、_answer_payload metrics 透出引用校验计数与回调异常吞噬。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.task_store as task_store_mod
from agents.agent import Agent, Answer
from agents.config import Config
from agents.executor import StepResult
from agents.llm import ChatResult, LLMClient
from agents.memory import SessionMemory
from agents.planner import Plan, PlanStep
from agents.task_runner import TaskRunner, plan_from_dict, plan_to_dict, step_from_dict, step_to_dict
from agents.task_store import TaskRecord, TaskStore
from agents.tools import Tool, ToolContext
from agents.web_server import Handler
from agents.web_store import WebStore


class ScriptedLLM(LLMClient):
    """离线假 LLM：按脚本顺序返回文本，记录调用次数（不发真实请求）。"""

    def __init__(self, texts: list[str], config: Config):
        self.config = config
        self._texts = list(texts)
        self.calls = 0

    def _chat(self, messages: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        text = self._texts[self.calls - 1] if self.calls <= len(self._texts) else self._texts[-1]
        return ChatResult(text=text)


def _make_tool(name: str, func, properties: dict, required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=f"测试工具 {name}",
        parameters={"type": "object", "properties": properties, "required": required},
        func=func,
    )


def _probe_tool(
    gates: dict[str, threading.Event] | None = None,
    exec_log: list[str] | None = None,
    fail: bool = False,
) -> Tool:
    """可门控探针工具：每次调用先记入 exec_log；gates[query] 未放行则阻塞（超时兜底）。"""
    def probe(ctx, query=""):
        if exec_log is not None:
            exec_log.append(query)
        if fail:
            raise RuntimeError(f"知识库炸了: {query}")
        if gates is not None and query in gates:
            gates[query].wait(timeout=5)
        return {"text": f"{query} 的检索结果", "sources": [f"kb://{query}.md"]}

    return _make_tool("probe", probe, {"query": {"type": "string"}}, ["query"])


def _make_agent(td: str, llm: ScriptedLLM, tools: dict[str, Tool]) -> Agent:
    """离线 Agent：tmp data_dir、tfidf 记忆后端、不加载真实索引（保留 llm.config 其余覆盖）。"""
    overrides = {"data_dir": td, "memory": {"embedding_backend": "tfidf"}, "tools": {"max_retries": 0}}
    merged = dict(llm.config._raw)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    agent = Agent(Config(merged), llm=llm, lazy_index=True)
    agent._index_loaded = True  # 跳过索引加载
    agent.tools = tools
    agent._ctx = ToolContext()
    return agent


def _web_probe_tool() -> Tool:
    """仅返回 http 来源的探针工具（验证写回 record_query 的 kb_hit 非 http 口径）。"""
    def web(ctx, query=""):
        return {"text": f"{query} 的网页结果", "sources": ["https://example.com/r1"]}

    return _make_tool("web", web, {"query": {"type": "string"}}, ["query"])


def _make_runner(
    store: TaskStore,
    llm: ScriptedLLM,
    tools: dict[str, Tool],
    td: str,
    agent_holder: dict | None = None,
    on_complete: Callable[[TaskRecord], None] | None = None,
) -> TaskRunner:
    """真实 TaskRunner：agent_provider 每次构建新离线 Agent（恢复路径等价于重启后重建）。"""

    def build_agent():
        agent = _make_agent(td, llm, tools)
        if agent_holder is not None:
            agent_holder["agent"] = agent
        return agent

    return TaskRunner(
        store,
        threading.Lock(),
        build_agent,
        memory_provider=lambda session_id: SessionMemory(),  # 空会话记忆（离线）
        on_complete=on_complete,  # T1：completed 终态回调（写回会话消息）
    )


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """轮询等待条件成立（超时返回当时判定结果），不依赖裸 sleep 猜时序。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _plan_json(steps: list[dict], summary: str = "测试计划") -> str:
    return json.dumps({"reasoning": "测试规划", "plan_summary": summary, "steps": steps}, ensure_ascii=False)


PLAN_3STEPS = _plan_json([
    {"action": "probe", "input": {"query": "q1"}, "purpose": "第一步"},
    {"action": "probe", "input": {"query": "q2"}, "purpose": "第二步"},
    {"action": "probe", "input": {"query": "q3"}, "purpose": "第三步"},
], "三步检索")
PLAN_Q1 = _plan_json([{"action": "probe", "input": {"query": "q1"}, "purpose": "p"}], "单步 q1")
PLAN_Q2 = _plan_json([{"action": "probe", "input": {"query": "q2"}, "purpose": "p"}], "单步 q2")
PLAN_Q3 = _plan_json([{"action": "probe", "input": {"query": "q3"}, "purpose": "p"}], "单步 q3")
PLAN_WEB = _plan_json([{"action": "web", "input": {"query": "w1"}, "purpose": "p"}], "单步 web")


class TaskRunnerTests(unittest.TestCase):
    """spec ACC-S5-01..04 + 失败路径/序列化/TaskStore + T1 完成回调写回（ACC-T1-01..03）。"""

    # ---------------- ACC-S5-01 逐步持久化 ----------------

    def test_acc_s5_01_stepwise_persistence(self):
        """ACC-S5-01：3 步计划任务执行中，steps 长度 0→1→2→3 递增、status=running→completed。"""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks")
            gates = {q: threading.Event() for q in ("q1", "q2", "q3")}
            exec_log: list[str] = []
            llm = ScriptedLLM([PLAN_3STEPS, "最终答案[1]"], Config({}))
            runner = _make_runner(store, llm, {"probe": _probe_tool(gates, exec_log)}, td)
            tid = runner.submit("s1", "w1", "三步任务")

            # 进入 running（执行开始对外可见）
            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status == "running"), "任务应进入 running")
            # 规划先于步骤持久化（门控未放行时工作线程阻塞在第一步的工具调用里）
            self.assertTrue(_wait_until(lambda: len((store.get(tid).plan or {}).get("steps") or []) == 3), "计划应先落盘")
            rec = store.get(tid)
            self.assertEqual(rec.steps, [], "第一步完成前 steps 应为空")

            gates["q1"].set()
            self.assertTrue(_wait_until(lambda: len(store.get(tid).steps) >= 1))
            rec = store.get(tid)
            self.assertEqual(rec.status, "running")
            self.assertEqual(len(rec.steps), 1, "第二步被门控阻塞，steps 应停在 1")
            self.assertEqual(rec.steps[0]["step_id"], 1)
            self.assertIs(rec.steps[0]["ok"], True)

            gates["q2"].set()
            self.assertTrue(_wait_until(lambda: len(store.get(tid).steps) >= 2))
            rec = store.get(tid)
            self.assertEqual(rec.status, "running")
            self.assertEqual(len(rec.steps), 2)

            gates["q3"].set()
            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed"), "任务应完成")
            rec = store.get(tid)
            self.assertEqual(len(rec.steps), 3)
            self.assertTrue(all(s["ok"] for s in rec.steps))
            self.assertEqual(rec.final_answer, "最终答案[1]")
            self.assertEqual(rec.sources, ["kb://q1.md", "kb://q2.md", "kb://q3.md"])
            self.assertEqual(rec.error, "")
            self.assertIsInstance(rec.usage.get("prompt_tokens"), int)
            self.assertGreaterEqual(rec.usage.get("prompt_tokens", -1), 0)
            self.assertIsInstance(rec.usage.get("completion_tokens"), int)
            self.assertIn("cost_yuan", rec.usage)
            self.assertEqual(exec_log, ["q1", "q2", "q3"], f"每步应恰好执行一次: {exec_log}")
            self.assertEqual(llm.calls, 2, f"规划+合成共 2 次 LLM 调用，实际 {llm.calls}")

        print("✓ ACC-S5-01 每完成一步 steps 递增落盘（0→1→2→3），status running→completed")

    # ---------------- ACC-S5-02 暂停/恢复 ----------------

    def test_acc_s5_02_pause_resume(self):
        """ACC-S5-02：pause 后 resume → completed、final_answer 非空、已完成步骤未重复执行。"""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks")
            gates = {q: threading.Event() for q in ("q1", "q2", "q3")}
            exec_log: list[str] = []
            llm = ScriptedLLM([PLAN_3STEPS, "暂停恢复后的答案[1]"], Config({}))
            runner = _make_runner(store, llm, {"probe": _probe_tool(gates, exec_log)}, td)
            tid = runner.submit("s1", "w1", "暂停恢复任务")

            self.assertTrue(_wait_until(lambda: len((store.get(tid).plan or {}).get("steps") or []) == 3))
            gates["q1"].set()
            self.assertTrue(_wait_until(lambda: len(store.get(tid).steps) >= 1))

            runner.pause(tid)                      # 非阻塞：设置暂停事件
            gates["q2"].set()                      # 放行在途步骤，让工作线程走到步骤边界检查点
            gates["q3"].set()
            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status == "paused"), "暂停应在步骤边界生效")

            paused_rec = store.get(tid)
            paused_steps = [dict(s) for s in paused_rec.steps]
            self.assertTrue(1 <= len(paused_steps) <= 2, f"暂停点应落在步骤边界: {len(paused_steps)}")
            self.assertEqual(len(paused_steps), len(set(exec_log)), "暂停前每步应恰好执行一次且已持久化")

            self.assertEqual(runner.resume(tid), "queued")
            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed"), "恢复后应完成")
            rec = store.get(tid)
            self.assertTrue(rec.final_answer and "暂停恢复后的答案" in rec.final_answer)
            self.assertEqual(len(rec.steps), 3)
            self.assertTrue(all(s["ok"] for s in rec.steps))
            self.assertEqual(rec.error, "")
            # 已完成步骤未重复执行：每个 query 恰好执行一次；暂停前持久化的步骤结果原样保留
            for q in ("q1", "q2", "q3"):
                self.assertEqual(exec_log.count(q), 1, f"{q} 不应重复执行: {exec_log}")
            for before in paused_steps:
                after = next(s for s in rec.steps if s["step_id"] == before["step_id"])
                self.assertEqual(after, before, f"步骤 {before['step_id']} 的持久化结果不应被改写")

        print("✓ ACC-S5-02 pause→resume：completed、答案非空、已完成步骤未重复且结果不变")

    # ---------------- ACC-S5-03 崩溃恢复 ----------------

    def test_acc_s5_03_recover_running(self):
        """ACC-S5-03：遗留 status=running/queued 任务文件 → recover_running → paused → resume 完成。"""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks")
            # 手工构造遗留任务：running（带已持久化的单步计划，模拟执行中崩溃）
            stuck = TaskRecord(
                session_id="s-rec", workspace_id="w1", question="遗留任务", status="running",
                plan={"reasoning": "r", "plan_summary": "单步", "fallback": False, "rounds": 1, "reflections": [],
                      "steps": [{"action": "probe", "input": {"query": "r1"}, "purpose": "p", "step_id": 1}]},
            )
            tid = store.create(stuck)
            queued_id = store.create(TaskRecord(session_id="s-rec", workspace_id="w1", question="排队遗留", status="queued"))
            self.assertEqual(store.get(tid).status, "running")

            recovered = store.recover_running()
            self.assertEqual(set(recovered), {tid, queued_id}, f"queued/running 均应被恢复: {recovered}")
            self.assertEqual(store.get(tid).status, "paused")
            self.assertEqual(store.get(queued_id).status, "paused")

            # 恢复执行：沿用持久化计划，不重新规划（LLM 只有合成一次调用）
            llm = ScriptedLLM(["恢复完成的答案[1]"], Config({}))
            runner = _make_runner(store, llm, {"probe": _probe_tool()}, td)
            self.assertEqual(runner.resume(tid), "queued")
            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed"), "恢复后应完成")
            rec = store.get(tid)
            self.assertTrue(rec.final_answer and "恢复完成" in rec.final_answer)
            self.assertEqual(len(rec.steps), 1)
            self.assertTrue(rec.steps[0]["ok"])
            self.assertEqual(rec.steps[0]["step_id"], 1)
            self.assertEqual(llm.calls, 1, f"恢复路径只应合成一次，实际 {llm.calls} 次调用")

            # 排队遗留任务同样可恢复完成
            self.assertEqual(runner.resume(queued_id), "queued")
            self.assertTrue(_wait_until(lambda: (store.get(queued_id) or TaskRecord()).status == "completed"))

        print("✓ ACC-S5-03 recover_running 把 running/queued 转 paused，resume 后正常完成且不重新规划")

    # ---------------- ACC-S5-04 取消 ----------------

    def test_acc_s5_04_cancel(self):
        """ACC-S5-04：cancel 运行中/排队任务 → status=canceled 且 resume 无效（终态保护）。"""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks")
            gate_q1, gate_q2 = threading.Event(), threading.Event()
            exec_log: list[str] = []
            llm = ScriptedLLM([PLAN_Q1, PLAN_Q2], Config({}))
            runner = _make_runner(store, llm, {"probe": _probe_tool({"q1": gate_q1, "q2": gate_q2}, exec_log)}, td)

            # 1) 取消运行中任务：工作线程阻塞在 q1 的工具调用里时发起 cancel
            tid_a = runner.submit("s1", "w1", "运行中取消")
            self.assertTrue(_wait_until(lambda: len((store.get(tid_a).plan or {}).get("steps") or []) == 1))
            self.assertEqual(runner.cancel(tid_a), "canceled")
            self.assertEqual(store.get(tid_a).status, "canceled", "cancel 应立即落盘 canceled")
            try:
                runner.resume(tid_a)
                self.fail("canceled 任务不应可 resume")
            except ValueError:
                pass
            gate_q1.set()  # 放行在途步骤：工作线程持久化该步后进入取消收尾（不再合成）
            self.assertTrue(
                _wait_until(
                    lambda: (r := store.get(tid_a)) is not None and r.status == "canceled" and len(r.steps) == 1
                ),
                "取消收尾后应回到 canceled 且记录在途步骤",
            )
            final_a = store.get(tid_a)
            self.assertEqual(final_a.final_answer, "")
            self.assertEqual(exec_log, ["q1"])
            try:
                runner.resume(tid_a)
                self.fail("收尾完成后 resume 仍应被拒绝")
            except ValueError:
                pass
            self.assertEqual(store.get(tid_a).status, "canceled")

            # 2) 取消排队任务：任务 B 阻塞工作线程，任务 C 保持 queued
            tid_b = runner.submit("s1", "w1", "占位任务")
            self.assertTrue(_wait_until(lambda: len((store.get(tid_b).plan or {}).get("steps") or []) == 1))
            tid_c = runner.submit("s1", "w1", "排队取消")
            self.assertEqual(store.get(tid_c).status, "queued", "工作线程被 B 占用时 C 应保持 queued")
            self.assertEqual(runner.cancel(tid_c), "canceled")
            self.assertEqual(store.get(tid_c).status, "canceled")
            try:
                runner.resume(tid_c)
                self.fail("排队即取消的任务不应可 resume")
            except ValueError:
                pass
            gate_q2.set()  # 释放工作线程，让 B 正常完成
            self.assertTrue(_wait_until(lambda: (store.get(tid_b) or TaskRecord()).status == "completed"))
            self.assertEqual(store.get(tid_c).status, "canceled", "B 完成后 C 不应被复活")
            self.assertNotIn("q3", exec_log, "被取消的排队任务不应执行任何步骤")

        print("✓ ACC-S5-04 cancel 运行中/排队任务 → canceled 终态，resume 无效、不被复活")

    # ---------------- 失败路径 ----------------

    def test_step_failure_node_persisted(self):
        """步骤级失败：工具必抛错且无降级 → 失败节点（ok=False/error/attempts）持久化，任务仍完成。"""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks")
            llm = ScriptedLLM([PLAN_Q1, "答案"], Config({}))
            runner = _make_runner(store, llm, {"probe": _probe_tool(fail=True)}, td)  # 无 web_search 可降级
            tid = runner.submit("s1", "w1", "失败节点任务")

            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed"))
            rec = store.get(tid)
            self.assertEqual(rec.status, "completed")
            self.assertEqual(rec.error, "")
            self.assertEqual(len(rec.steps), 1)
            step = rec.steps[0]
            self.assertIs(step["ok"], False, "失败步骤应持久化为失败节点")
            self.assertIn("RuntimeError", step["error"])
            self.assertIn("知识库炸了", step["error"])
            self.assertEqual(step["attempts"], 1, "max_retries=0 时不应重试")
            self.assertIs(step["degraded"], False)
            self.assertTrue(step["output"]["text"].startswith("工具执行失败"))
            self.assertEqual(rec.final_answer, "答案")
            self.assertEqual(rec.sources, [])

        print("✓ 步骤级失败节点持久化（ok=False/error/attempts=1/无降级），任务仍 completed")

    def test_task_level_failed_on_plan_error(self):
        """任务级失败：规划失败（fallback_direct=False + 坏 JSON）→ status=failed + error 落盘。"""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks")
            cfg = Config({"data_dir": td, "memory": {"embedding_backend": "tfidf"},
                          "tools": {"max_retries": 0}, "planner": {"fallback_direct": False}})
            llm = ScriptedLLM(["这不是JSON", "修复轮也不是JSON"], cfg)
            runner = _make_runner(store, llm, {"probe": _probe_tool()}, td)
            tid = runner.submit("s1", "w1", "规划失败任务")

            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status == "failed"), "规划失败应 failed")
            rec = store.get(tid)
            self.assertIn("解析", rec.error, f"error 应含解析失败信息: {rec.error!r}")
            self.assertEqual(rec.final_answer, "")
            self.assertEqual(rec.steps, [])

            # agent_provider 返回 None → failed 落盘（守护路径）
            store2 = TaskStore(Path(td) / "tasks2")
            runner2 = TaskRunner(store2, threading.Lock(), lambda: None)
            tid2 = runner2.submit("s1", "w1", "无 Agent 任务")
            self.assertTrue(_wait_until(lambda: (store2.get(tid2) or TaskRecord()).status == "failed"))
            self.assertIn("Agent", store2.get(tid2).error)

        print("✓ 任务级 failed（规划异常/Agent 不可用）→ failed + error 落盘，不产生答案")

    # ---------------- 序列化与存储 ----------------

    def test_plan_step_serialization(self):
        """Plan/StepResult 与持久化 dict 的往返；不可序列化的工具输出被安全化。"""
        plan = Plan(
            reasoning="r", plan_summary="s", fallback=True, rounds=2, reflections=["a"],
            steps=[PlanStep(action="probe", input={"query": "@step:1"}, purpose="p", step_id=1)],
        )
        back = plan_from_dict(plan_to_dict(plan))
        self.assertEqual(back.reasoning, "r")
        self.assertEqual(back.plan_summary, "s")
        self.assertTrue(back.fallback)
        self.assertEqual(back.rounds, 2)
        self.assertEqual(back.reflections, ["a"])
        self.assertEqual(back.steps[0].action, "probe")
        self.assertEqual(back.steps[0].input, {"query": "@step:1"})
        self.assertEqual(back.steps[0].step_id, 1)
        self.assertEqual(back.steps[0].purpose, "p")

        result = StepResult(
            step_id=1, action="probe", input={"query": "x"},
            output={"text": "t", "obj": object(), "nest": [1, {"k": Path("x")}]},
            latency_s=0.5, ok=True, attempts=2, degraded=True,
        )
        d = step_to_dict(result)
        json.dumps(d, ensure_ascii=False)  # 不抛异常即可序列化
        back_step = step_from_dict(d)
        self.assertEqual(back_step.step_id, 1)
        self.assertTrue(back_step.ok)
        self.assertEqual(back_step.attempts, 2)
        self.assertTrue(back_step.degraded)
        self.assertEqual(back_step.output["text"], "t")
        self.assertIsInstance(back_step.output["obj"], str)
        self.assertIsInstance(back_step.output["nest"][1]["k"], str)

        print("✓ plan/step 序列化往返，不可序列化输出安全化为字符串")

    def test_task_store_basics(self):
        """TaskStore：list 按 updated_at 倒序、roundtrip、update 原子落盘、非法 id 防护。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "tasks"
            store = TaskStore(root)
            self.assertEqual(store.list(), [], "空库 list 应为空")

            # 控制时钟验证排序：created/update 时间戳单调递增
            real_now = task_store_mod._now
            clock = {"n": 0}

            def fake_now():
                clock["n"] += 1
                return f"2026-08-01T00:00:{clock['n']:02d}+08:00"

            task_store_mod._now = fake_now
            try:
                id1 = store.create(TaskRecord(session_id="s", question="一"))
                id2 = store.create(TaskRecord(session_id="s", question="二"))
                id3 = store.create(TaskRecord(session_id="s", question="三"))
            finally:
                task_store_mod._now = real_now

            self.assertTrue(all(len(t) == 12 and all(c in "0123456789abcdef" for c in t) for t in (id1, id2, id3)))
            self.assertEqual([r.task_id for r in store.list()], [id3, id2, id1], "list 应按 updated_at 倒序")

            # roundtrip：文件内容反序列化后与 get 结果一致
            rec = store.get(id2)
            self.assertEqual(rec.question, "二")
            self.assertEqual(rec.status, "queued")
            self.assertEqual(rec.steps, [])
            blob = json.loads((root / f"{id2}.json").read_text(encoding="utf-8"))
            self.assertEqual(TaskRecord.from_dict(blob).to_dict(), rec.to_dict())

            # update：刷新 updated_at、保留字段、无 tmp 残留
            rec.status = "running"
            store.update(rec)
            updated = store.get(id2)
            self.assertEqual(updated.status, "running")
            self.assertGreaterEqual(updated.updated_at, rec.updated_at)
            self.assertFalse(list(root.glob("*.tmp")), "原子替换后不应残留 tmp 文件")

            # 非法/未知 id：get 返回 None，create 拒绝
            self.assertIsNone(store.get("../evil"))
            self.assertIsNone(store.get("0" * 12))
            try:
                store.create(TaskRecord(task_id="../../evil"))
                self.fail("非法 id 应被拒绝")
            except ValueError:
                pass

            # 终态任务不会被 recover_running 改动
            for task_id in (id1, id3):
                done = store.get(task_id)
                done.status = "completed"
                store.update(done)
            self.assertEqual(store.recover_running(), [id2], "仅 queued/running 需要恢复")

        print("✓ TaskStore 倒序/roundtrip/原子落盘/非法 id 防护/终态不恢复")

    # ---------------- T1 · ACC-T1-01 completed 写回会话消息 ----------------

    def test_acc_t1_01_completed_writeback(self):
        """ACC-T1-01：completed 任务 → WebStore 写回 user+assistant 两条消息，
        assistant.metrics.task_id==task_id、sources/plan 与 record 一致；查询事件 +1（kb_hit 口径）。"""
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks")
            web_store = WebStore(Path(td) / "webui.sqlite3")
            web_store.ensure_default(str(Path(td)))
            session_id = web_store.create_session("default")

            old_store = Handler.store
            Handler.store = web_store  # persist_task_result 以 Handler.store/lock 访问（classmethod）
            try:
                # kb 来源任务（probe → kb://q1.md，非 http → kb_hit=True）
                llm = ScriptedLLM([PLAN_Q1, "任务最终答案[1]"], Config({}))
                runner = _make_runner(
                    store, llm, {"probe": _probe_tool()}, td,
                    on_complete=lambda record: Handler.persist_task_result(record),
                )
                tid = runner.submit(session_id, "default", "写回任务")
                self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed"), "任务应完成")
                # 写回在工作线程异步发生：轮询等待两条消息出现
                self.assertTrue(_wait_until(lambda: len(web_store.messages(session_id)) >= 2), "应写回 user+assistant 两条消息")

                rec = store.get(tid)
                msgs = web_store.messages(session_id)
                self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
                self.assertEqual(msgs[0]["content"], "写回任务")
                self.assertEqual(msgs[1]["content"], rec.final_answer)
                self.assertEqual(rec.final_answer, "任务最终答案[1]")
                self.assertEqual(msgs[1]["sources"], rec.sources)
                self.assertEqual(rec.sources, ["kb://q1.md"])
                self.assertEqual(msgs[1]["metrics"]["task_id"], tid)
                self.assertEqual(msgs[1]["metrics"]["citations_valid"], rec.citations_valid)
                self.assertEqual(msgs[1]["metrics"]["citations_invalid"], rec.citations_invalid)
                self.assertEqual(msgs[1]["metrics"]["prompt_tokens"], rec.usage["prompt_tokens"])
                self.assertEqual(msgs[1]["metrics"]["completion_tokens"], rec.usage["completion_tokens"])
                self.assertEqual(msgs[1]["metrics"]["cost_yuan"], round(rec.usage["cost_yuan"], 4))
                self.assertIsInstance(msgs[1]["metrics"].get("latency_s"), (int, float), "应附任务耗时")
                self.assertEqual(msgs[1]["plan"], rec.plan, "assistant 消息 plan 应复用 record.plan")
                stats = web_store.stats("default")
                self.assertEqual(stats["today_queries"], 1, f"kb 来源应记 kb_hit: {stats}")
                self.assertEqual(stats["kb_hits"], 1, f"kb 来源应记 kb_hit: {stats}")

                # 仅 http 来源的任务：查询事件再 +1 但 kb_hits 不变（口径与 _persist_answer 一致）
                llm2 = ScriptedLLM([PLAN_WEB, "网页答案"], Config({}))
                runner2 = _make_runner(
                    store, llm2, {"web": _web_probe_tool()}, td,
                    on_complete=lambda record: Handler.persist_task_result(record),
                )
                tid2 = runner2.submit(session_id, "default", "网页任务")
                self.assertTrue(_wait_until(lambda: (store.get(tid2) or TaskRecord()).status == "completed"))
                self.assertTrue(_wait_until(lambda: len(web_store.messages(session_id)) >= 4))
                stats = web_store.stats("default")
                self.assertEqual(stats["today_queries"], 2)
                self.assertEqual(stats["kb_hits"], 1, f"http 来源不算 kb_hit: {stats}")
            finally:
                Handler.store = old_store

        print("✓ ACC-T1-01 completed 任务写回 user+assistant 消息（task_id/sources/plan/用量），查询事件 +1 且 kb_hit 口径正确")

    # ---------------- T1 · ACC-T1-02 非完成终态不回调 ----------------

    def test_acc_t1_02_failed_canceled_no_callback(self):
        """ACC-T1-02：failed/canceled/paused 任务不触发 on_complete；同 runner 的 completed 恰好回调一次。"""
        with tempfile.TemporaryDirectory() as td:
            calls: list[str] = []

            def on_complete(record: TaskRecord) -> None:
                calls.append(record.task_id)

            # 1) failed：规划失败（坏 JSON 两次 + fallback_direct=False）
            store = TaskStore(Path(td) / "tasks")
            cfg = Config({"data_dir": td, "memory": {"embedding_backend": "tfidf"},
                          "tools": {"max_retries": 0}, "planner": {"fallback_direct": False}})
            llm = ScriptedLLM(["这不是JSON", "修复轮也不是JSON"], cfg)
            runner = _make_runner(store, llm, {"probe": _probe_tool()}, td, on_complete=on_complete)
            tid_f = runner.submit("s1", "w1", "失败任务")
            self.assertTrue(_wait_until(lambda: (store.get(tid_f) or TaskRecord()).status == "failed"), "规划失败应 failed")
            self.assertEqual(calls, [], "failed 任务不应触发回调")

            # 2) canceled：运行中取消（工作线程阻塞在 q1 的工具调用里时发起 cancel）
            gate_q1, gate_q2 = threading.Event(), threading.Event()
            store2 = TaskStore(Path(td) / "tasks2")
            llm2 = ScriptedLLM([PLAN_Q1, PLAN_Q2, PLAN_Q1, "控制组答案"], Config({}))
            runner2 = _make_runner(
                store2, llm2, {"probe": _probe_tool({"q1": gate_q1, "q2": gate_q2})}, td, on_complete=on_complete
            )
            tid_c = runner2.submit("s1", "w1", "运行中取消")
            self.assertTrue(_wait_until(lambda: len((store2.get(tid_c).plan or {}).get("steps") or []) == 1))
            self.assertEqual(runner2.cancel(tid_c), "canceled")
            gate_q1.set()  # 放行在途步骤，让工作线程走到取消收尾
            self.assertTrue(
                _wait_until(
                    lambda: (r := store2.get(tid_c)) is not None and r.status == "canceled" and len(r.steps) == 1
                ),
                "取消收尾后应为 canceled",
            )
            self.assertEqual(calls, [], "canceled 任务不应触发回调")

            # 3) paused：工作线程阻塞在 q2 的工具调用里时暂停，步骤边界生效
            tid_p = runner2.submit("s1", "w1", "暂停任务")
            self.assertTrue(_wait_until(lambda: len((store2.get(tid_p).plan or {}).get("steps") or []) == 1))
            self.assertIn(runner2.pause(tid_p), ("running", "paused"))
            gate_q2.set()  # 放行在途步骤，让工作线程走到步骤边界检查点
            self.assertTrue(
                _wait_until(lambda: (r := store2.get(tid_p)) is not None and r.status == "paused"), "应进入 paused"
            )
            self.assertEqual(calls, [], "paused 任务不应触发回调")
            self.assertEqual(runner2.cancel(tid_p), "canceled")  # 清理：paused → canceled

            # 4) 控制组：同一 runner 的正常任务完成 → 回调恰好一次（证明计数机制有效）
            tid_ok = runner2.submit("s1", "w1", "正常完成")
            self.assertTrue(_wait_until(lambda: (store2.get(tid_ok) or TaskRecord()).status == "completed"))
            self.assertEqual(calls, [tid_ok], f"仅 completed 任务应回调一次: {calls}")

        print("✓ ACC-T1-02 failed/canceled/paused 任务不触发 on_complete，completed 恰好回调一次")

    # ---------------- T1 · ACC-T1-03 payload citations + 回调异常吞噬 ----------------

    def test_acc_t1_03_payload_citations_and_callback_error(self):
        """ACC-T1-03：_answer_payload metrics 含 citations_valid/invalid 且旧键不变；
        on_complete 回调抛异常被吞掉并告警，任务仍 completed、工作线程继续服务。"""
        handler = object.__new__(Handler)  # 跳过 socket 初始化的裸实例，仅调用 _answer_payload
        answer = Answer(
            final_answer="答案正文[1]", error="", sources=["kb://a.md"],
            plan=Plan(reasoning="r", plan_summary="摘要",
                      steps=[PlanStep(action="probe", input={"query": "q"}, purpose="p", step_id=1)]),
            steps=[StepResult(step_id=1, action="probe", input={"query": "q"}, output={"text": "t"}, ok=True)],
            citations_valid=1, citations_invalid=2,
            total_latency_s=1.23, llm_calls=2, prompt_tokens=11, completion_tokens=7, estimated_cost=0.00214,
        )
        payload = handler._answer_payload(answer)
        metrics = payload["metrics"]
        # W1：顶层新增 sources_detail 键（与 sources 下标对应的结构化详情，加法扩展）；旧键不变
        self.assertEqual(
            set(payload),
            {"answer", "error", "sources", "sources_detail", "plan", "metrics"},
            "payload 顶层键不变（W1 加 sources_detail）",
        )
        self.assertEqual(payload["sources_detail"], [], "旧 answer 无详情时回退空表")
        self.assertEqual(metrics["citations_valid"], 1)
        self.assertEqual(metrics["citations_invalid"], 2)
        self.assertEqual(
            set(metrics),
            {"latency_s", "llm_calls", "prompt_tokens", "completion_tokens",
             "cost_yuan", "citations_valid", "citations_invalid",
             "degraded", "web_used"},  # G3/ACC-U3-01：新增外发标记两键，旧键不变
            "旧 metrics 键一个不少",
        )
        self.assertIs(metrics["degraded"], False)   # G3：无降级步骤 → False
        self.assertIs(metrics["web_used"], False)   # G3：无 web_search 步骤 → False
        self.assertEqual(metrics["latency_s"], 1.2)
        self.assertEqual(metrics["llm_calls"], 2)
        self.assertEqual(metrics["prompt_tokens"], 11)
        self.assertEqual(metrics["completion_tokens"], 7)
        self.assertEqual(metrics["cost_yuan"], 0.0021)
        self.assertEqual(payload["answer"], "答案正文[1]")
        self.assertEqual(payload["sources"], ["kb://a.md"])
        self.assertEqual(payload["plan"]["summary"], "摘要")
        self.assertEqual(payload["plan"]["steps"][0]["action"], "probe")

        # 缺 citations 字段的旧 answer 形状对象 → 按 0 透出，不抛异常
        legacy = SimpleNamespace(
            final_answer="旧答案", error="", sources=[], plan=Plan(), steps=[],
            total_latency_s=0.5, llm_calls=1, prompt_tokens=1, completion_tokens=1, estimated_cost=0.0,
        )
        legacy_metrics = handler._answer_payload(legacy)["metrics"]
        self.assertEqual(legacy_metrics["citations_valid"], 0)
        self.assertEqual(legacy_metrics["citations_invalid"], 0)

        # 回调抛异常：runner 吞掉不崩，任务仍 completed，后续任务照常完成
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks")

            def boom(record: TaskRecord) -> None:
                raise RuntimeError("写回炸了")

            llm = ScriptedLLM([PLAN_Q1, "第一次答案", PLAN_Q1, "第二次答案"], Config({}))
            runner = _make_runner(store, llm, {"probe": _probe_tool()}, td, on_complete=boom)
            tid1 = runner.submit("s1", "w1", "回调异常任务一")
            self.assertTrue(_wait_until(lambda: (store.get(tid1) or TaskRecord()).status == "completed"), "任务应不受回调异常影响")
            rec = store.get(tid1)
            self.assertEqual(rec.final_answer, "第一次答案")
            self.assertEqual(rec.error, "", "回调异常不应改写任务终态")

            tid2 = runner.submit("s1", "w1", "回调异常任务二")
            self.assertTrue(_wait_until(lambda: (store.get(tid2) or TaskRecord()).status == "completed"))
            self.assertEqual(store.get(tid2).final_answer, "第二次答案", "工作线程应在回调异常后继续服务")

        print("✓ ACC-T1-03 metrics 透出 citations_valid/invalid 且旧键不变；回调异常被吞、任务仍 completed")


if __name__ == "__main__":
    unittest.main()
