"""S5 任务状态持久化 + 暂停/恢复测试。

全部离线：LLM 用记录调用次数、按脚本返回文本的假客户端（子类化 LLMClient 覆写
_chat，不发真实请求）；Agent 不加载真实索引（注入假工具注册表）；工具用可门控
闭包——threading.Event 控制每步放行，确定性地观察每一步的中间持久化状态；
TaskStore 用 tempfile 临时目录。等待一律用「事件/轮询 + 超时」，不依赖时序猜测。
覆盖 spec ACC-S5-01..04，以及 failed 路径（步骤级失败节点 / 任务级失败）、
list() 排序、canceled 终态保护、序列化往返与 TaskStore 原子落盘。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.task_store as task_store_mod
from agents.agent import Agent
from agents.config import Config
from agents.executor import StepResult
from agents.llm import ChatResult, LLMClient
from agents.memory import SessionMemory
from agents.planner import Plan, PlanStep
from agents.task_runner import TaskRunner, plan_from_dict, plan_to_dict, step_from_dict, step_to_dict
from agents.task_store import TaskRecord, TaskStore
from agents.tools import Tool, ToolContext


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


def _make_runner(
    store: TaskStore,
    llm: ScriptedLLM,
    tools: dict[str, Tool],
    td: str,
    agent_holder: dict | None = None,
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


# ---------------- ACC-S5-01 逐步持久化 ----------------

def test_acc_s5_01_stepwise_persistence():
    """ACC-S5-01：3 步计划任务执行中，steps 长度 0→1→2→3 递增、status=running→completed。"""
    with tempfile.TemporaryDirectory() as td:
        store = TaskStore(Path(td) / "tasks")
        gates = {q: threading.Event() for q in ("q1", "q2", "q3")}
        exec_log: list[str] = []
        llm = ScriptedLLM([PLAN_3STEPS, "最终答案[1]"], Config({}))
        runner = _make_runner(store, llm, {"probe": _probe_tool(gates, exec_log)}, td)
        tid = runner.submit("s1", "w1", "三步任务")

        # 进入 running（执行开始对外可见）
        assert _wait_until(lambda: (store.get(tid) or TaskRecord()).status == "running"), "任务应进入 running"
        # 规划先于步骤持久化（门控未放行时工作线程阻塞在第一步的工具调用里）
        assert _wait_until(lambda: len((store.get(tid).plan or {}).get("steps") or []) == 3), "计划应先落盘"
        rec = store.get(tid)
        assert rec.steps == [], "第一步完成前 steps 应为空"

        gates["q1"].set()
        assert _wait_until(lambda: len(store.get(tid).steps) >= 1)
        rec = store.get(tid)
        assert rec.status == "running" and len(rec.steps) == 1, "第二步被门控阻塞，steps 应停在 1"
        assert rec.steps[0]["step_id"] == 1 and rec.steps[0]["ok"] is True

        gates["q2"].set()
        assert _wait_until(lambda: len(store.get(tid).steps) >= 2)
        rec = store.get(tid)
        assert rec.status == "running" and len(rec.steps) == 2

        gates["q3"].set()
        assert _wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed"), "任务应完成"
        rec = store.get(tid)
        assert len(rec.steps) == 3 and all(s["ok"] for s in rec.steps)
        assert rec.final_answer == "最终答案[1]"
        assert rec.sources == ["kb://q1.md", "kb://q2.md", "kb://q3.md"]
        assert rec.error == ""
        assert isinstance(rec.usage.get("prompt_tokens"), int) and rec.usage["prompt_tokens"] >= 0
        assert isinstance(rec.usage.get("completion_tokens"), int)
        assert "cost_yuan" in rec.usage
        assert exec_log == ["q1", "q2", "q3"], f"每步应恰好执行一次: {exec_log}"
        assert llm.calls == 2, f"规划+合成共 2 次 LLM 调用，实际 {llm.calls}"

    print("✓ ACC-S5-01 每完成一步 steps 递增落盘（0→1→2→3），status running→completed")


# ---------------- ACC-S5-02 暂停/恢复 ----------------

def test_acc_s5_02_pause_resume():
    """ACC-S5-02：pause 后 resume → completed、final_answer 非空、已完成步骤未重复执行。"""
    with tempfile.TemporaryDirectory() as td:
        store = TaskStore(Path(td) / "tasks")
        gates = {q: threading.Event() for q in ("q1", "q2", "q3")}
        exec_log: list[str] = []
        llm = ScriptedLLM([PLAN_3STEPS, "暂停恢复后的答案[1]"], Config({}))
        runner = _make_runner(store, llm, {"probe": _probe_tool(gates, exec_log)}, td)
        tid = runner.submit("s1", "w1", "暂停恢复任务")

        assert _wait_until(lambda: len((store.get(tid).plan or {}).get("steps") or []) == 3)
        gates["q1"].set()
        assert _wait_until(lambda: len(store.get(tid).steps) >= 1)

        runner.pause(tid)                      # 非阻塞：设置暂停事件
        gates["q2"].set()                      # 放行在途步骤，让工作线程走到步骤边界检查点
        gates["q3"].set()
        assert _wait_until(lambda: (store.get(tid) or TaskRecord()).status == "paused"), "暂停应在步骤边界生效"

        paused_rec = store.get(tid)
        paused_steps = [dict(s) for s in paused_rec.steps]
        assert 1 <= len(paused_steps) <= 2, f"暂停点应落在步骤边界: {len(paused_steps)}"
        assert len(paused_steps) == len(set(exec_log)), "暂停前每步应恰好执行一次且已持久化"

        assert runner.resume(tid) == "queued"
        assert _wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed"), "恢复后应完成"
        rec = store.get(tid)
        assert rec.final_answer and "暂停恢复后的答案" in rec.final_answer
        assert len(rec.steps) == 3 and all(s["ok"] for s in rec.steps)
        assert rec.error == ""
        # 已完成步骤未重复执行：每个 query 恰好执行一次；暂停前持久化的步骤结果原样保留
        for q in ("q1", "q2", "q3"):
            assert exec_log.count(q) == 1, f"{q} 不应重复执行: {exec_log}"
        for before in paused_steps:
            after = next(s for s in rec.steps if s["step_id"] == before["step_id"])
            assert after == before, f"步骤 {before['step_id']} 的持久化结果不应被改写"

    print("✓ ACC-S5-02 pause→resume：completed、答案非空、已完成步骤未重复且结果不变")


# ---------------- ACC-S5-03 崩溃恢复 ----------------

def test_acc_s5_03_recover_running():
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
        assert store.get(tid).status == "running"

        recovered = store.recover_running()
        assert set(recovered) == {tid, queued_id}, f"queued/running 均应被恢复: {recovered}"
        assert store.get(tid).status == "paused" and store.get(queued_id).status == "paused"

        # 恢复执行：沿用持久化计划，不重新规划（LLM 只有合成一次调用）
        llm = ScriptedLLM(["恢复完成的答案[1]"], Config({}))
        runner = _make_runner(store, llm, {"probe": _probe_tool()}, td)
        assert runner.resume(tid) == "queued"
        assert _wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed"), "恢复后应完成"
        rec = store.get(tid)
        assert rec.final_answer and "恢复完成" in rec.final_answer
        assert len(rec.steps) == 1 and rec.steps[0]["ok"] and rec.steps[0]["step_id"] == 1
        assert llm.calls == 1, f"恢复路径只应合成一次，实际 {llm.calls} 次调用"

        # 排队遗留任务同样可恢复完成
        assert runner.resume(queued_id) == "queued"
        assert _wait_until(lambda: (store.get(queued_id) or TaskRecord()).status == "completed")

    print("✓ ACC-S5-03 recover_running 把 running/queued 转 paused，resume 后正常完成且不重新规划")


# ---------------- ACC-S5-04 取消 ----------------

def test_acc_s5_04_cancel():
    """ACC-S5-04：cancel 运行中/排队任务 → status=canceled 且 resume 无效（终态保护）。"""
    with tempfile.TemporaryDirectory() as td:
        store = TaskStore(Path(td) / "tasks")
        gate_q1, gate_q2 = threading.Event(), threading.Event()
        exec_log: list[str] = []
        llm = ScriptedLLM([PLAN_Q1, PLAN_Q2], Config({}))
        runner = _make_runner(store, llm, {"probe": _probe_tool({"q1": gate_q1, "q2": gate_q2}, exec_log)}, td)

        # 1) 取消运行中任务：工作线程阻塞在 q1 的工具调用里时发起 cancel
        tid_a = runner.submit("s1", "w1", "运行中取消")
        assert _wait_until(lambda: len((store.get(tid_a).plan or {}).get("steps") or []) == 1)
        assert runner.cancel(tid_a) == "canceled"
        assert store.get(tid_a).status == "canceled", "cancel 应立即落盘 canceled"
        try:
            runner.resume(tid_a)
            raise AssertionError("canceled 任务不应可 resume")
        except ValueError:
            pass
        gate_q1.set()  # 放行在途步骤：工作线程持久化该步后进入取消收尾（不再合成）
        assert _wait_until(
            lambda: (r := store.get(tid_a)) is not None and r.status == "canceled" and len(r.steps) == 1
        ), "取消收尾后应回到 canceled 且记录在途步骤"
        final_a = store.get(tid_a)
        assert final_a.final_answer == "" and exec_log == ["q1"]
        try:
            runner.resume(tid_a)
            raise AssertionError("收尾完成后 resume 仍应被拒绝")
        except ValueError:
            pass
        assert store.get(tid_a).status == "canceled"

        # 2) 取消排队任务：任务 B 阻塞工作线程，任务 C 保持 queued
        tid_b = runner.submit("s1", "w1", "占位任务")
        assert _wait_until(lambda: len((store.get(tid_b).plan or {}).get("steps") or []) == 1)
        tid_c = runner.submit("s1", "w1", "排队取消")
        assert store.get(tid_c).status == "queued", "工作线程被 B 占用时 C 应保持 queued"
        assert runner.cancel(tid_c) == "canceled"
        assert store.get(tid_c).status == "canceled"
        try:
            runner.resume(tid_c)
            raise AssertionError("排队即取消的任务不应可 resume")
        except ValueError:
            pass
        gate_q2.set()  # 释放工作线程，让 B 正常完成
        assert _wait_until(lambda: (store.get(tid_b) or TaskRecord()).status == "completed")
        assert store.get(tid_c).status == "canceled", "B 完成后 C 不应被复活"
        assert "q3" not in exec_log, "被取消的排队任务不应执行任何步骤"

    print("✓ ACC-S5-04 cancel 运行中/排队任务 → canceled 终态，resume 无效、不被复活")


# ---------------- 失败路径 ----------------

def test_step_failure_node_persisted():
    """步骤级失败：工具必抛错且无降级 → 失败节点（ok=False/error/attempts）持久化，任务仍完成。"""
    with tempfile.TemporaryDirectory() as td:
        store = TaskStore(Path(td) / "tasks")
        llm = ScriptedLLM([PLAN_Q1, "答案"], Config({}))
        runner = _make_runner(store, llm, {"probe": _probe_tool(fail=True)}, td)  # 无 web_search 可降级
        tid = runner.submit("s1", "w1", "失败节点任务")

        assert _wait_until(lambda: (store.get(tid) or TaskRecord()).status == "completed")
        rec = store.get(tid)
        assert rec.status == "completed" and rec.error == ""
        assert len(rec.steps) == 1
        step = rec.steps[0]
        assert step["ok"] is False, "失败步骤应持久化为失败节点"
        assert "RuntimeError" in step["error"] and "知识库炸了" in step["error"]
        assert step["attempts"] == 1, "max_retries=0 时不应重试"
        assert step["degraded"] is False and step["output"]["text"].startswith("工具执行失败")
        assert rec.final_answer == "答案" and rec.sources == []

    print("✓ 步骤级失败节点持久化（ok=False/error/attempts=1/无降级），任务仍 completed")


def test_task_level_failed_on_plan_error():
    """任务级失败：规划失败（fallback_direct=False + 坏 JSON）→ status=failed + error 落盘。"""
    with tempfile.TemporaryDirectory() as td:
        store = TaskStore(Path(td) / "tasks")
        cfg = Config({"data_dir": td, "memory": {"embedding_backend": "tfidf"},
                      "tools": {"max_retries": 0}, "planner": {"fallback_direct": False}})
        llm = ScriptedLLM(["这不是JSON", "修复轮也不是JSON"], cfg)
        runner = _make_runner(store, llm, {"probe": _probe_tool()}, td)
        tid = runner.submit("s1", "w1", "规划失败任务")

        assert _wait_until(lambda: (store.get(tid) or TaskRecord()).status == "failed"), "规划失败应 failed"
        rec = store.get(tid)
        assert "解析" in rec.error, f"error 应含解析失败信息: {rec.error!r}"
        assert rec.final_answer == "" and rec.steps == []

        # agent_provider 返回 None → failed 落盘（守护路径）
        store2 = TaskStore(Path(td) / "tasks2")
        runner2 = TaskRunner(store2, threading.Lock(), lambda: None)
        tid2 = runner2.submit("s1", "w1", "无 Agent 任务")
        assert _wait_until(lambda: (store2.get(tid2) or TaskRecord()).status == "failed")
        assert "Agent" in store2.get(tid2).error

    print("✓ 任务级 failed（规划异常/Agent 不可用）→ failed + error 落盘，不产生答案")


# ---------------- 序列化与存储 ----------------

def test_plan_step_serialization():
    """Plan/StepResult 与持久化 dict 的往返；不可序列化的工具输出被安全化。"""
    plan = Plan(
        reasoning="r", plan_summary="s", fallback=True, rounds=2, reflections=["a"],
        steps=[PlanStep(action="probe", input={"query": "@step:1"}, purpose="p", step_id=1)],
    )
    back = plan_from_dict(plan_to_dict(plan))
    assert back.reasoning == "r" and back.plan_summary == "s" and back.fallback and back.rounds == 2
    assert back.reflections == ["a"]
    assert back.steps[0].action == "probe" and back.steps[0].input == {"query": "@step:1"}
    assert back.steps[0].step_id == 1 and back.steps[0].purpose == "p"

    result = StepResult(
        step_id=1, action="probe", input={"query": "x"},
        output={"text": "t", "obj": object(), "nest": [1, {"k": Path("x")}]},
        latency_s=0.5, ok=True, attempts=2, degraded=True,
    )
    d = step_to_dict(result)
    json.dumps(d, ensure_ascii=False)  # 不抛异常即可序列化
    back_step = step_from_dict(d)
    assert back_step.step_id == 1 and back_step.ok and back_step.attempts == 2 and back_step.degraded
    assert back_step.output["text"] == "t"
    assert isinstance(back_step.output["obj"], str) and isinstance(back_step.output["nest"][1]["k"], str)

    print("✓ plan/step 序列化往返，不可序列化输出安全化为字符串")


def test_task_store_basics():
    """TaskStore：list 按 updated_at 倒序、roundtrip、update 原子落盘、非法 id 防护。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "tasks"
        store = TaskStore(root)
        assert store.list() == [], "空库 list 应为空"

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

        assert all(len(t) == 12 and all(c in "0123456789abcdef" for c in t) for t in (id1, id2, id3))
        assert [r.task_id for r in store.list()] == [id3, id2, id1], "list 应按 updated_at 倒序"

        # roundtrip：文件内容反序列化后与 get 结果一致
        rec = store.get(id2)
        assert rec.question == "二" and rec.status == "queued" and rec.steps == []
        blob = json.loads((root / f"{id2}.json").read_text(encoding="utf-8"))
        assert TaskRecord.from_dict(blob).to_dict() == rec.to_dict()

        # update：刷新 updated_at、保留字段、无 tmp 残留
        rec.status = "running"
        store.update(rec)
        updated = store.get(id2)
        assert updated.status == "running" and updated.updated_at >= rec.updated_at
        assert not list(root.glob("*.tmp")), "原子替换后不应残留 tmp 文件"

        # 非法/未知 id：get 返回 None，create 拒绝
        assert store.get("../evil") is None
        assert store.get("0" * 12) is None
        try:
            store.create(TaskRecord(task_id="../../evil"))
            raise AssertionError("非法 id 应被拒绝")
        except ValueError:
            pass

        # 终态任务不会被 recover_running 改动
        for task_id in (id1, id3):
            done = store.get(task_id)
            done.status = "completed"
            store.update(done)
        assert store.recover_running() == [id2], "仅 queued/running 需要恢复"

    print("✓ TaskStore 倒序/roundtrip/原子落盘/非法 id 防护/终态不恢复")


if __name__ == "__main__":
    test_acc_s5_01_stepwise_persistence()
    test_acc_s5_02_pause_resume()
    test_acc_s5_03_recover_running()
    test_acc_s5_04_cancel()
    test_step_failure_node_persisted()
    test_task_level_failed_on_plan_error()
    test_plan_step_serialization()
    test_task_store_basics()
    print("\nS5 任务状态持久化与暂停/恢复测试全部通过 ✅")
