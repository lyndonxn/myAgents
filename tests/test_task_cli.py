"""T3 CLI 任务模式测试（scripts/task.py）。

全部离线：LLM 用按脚本返回文本的假客户端（子类化 LLMClient 覆写 _chat，不发真实请求）；
Agent 不加载真实索引（注入假工具注册表，门控工具用 threading.Event 确定性放行）；
TaskStore 用 tempfile 临时目录（等价 --task-store 覆盖）；不启动子进程、不联网，
直接 import scripts.task 的 run_* 函数（main 只做 argparse 分发，单独测参数规则）。
等待一律用「事件/轮询 + 超时」，不依赖时序猜测。
覆盖 spec ACC-T3-01..03 与附加场景：无 Key 报错退出、参数互斥/缺参、Ctrl+C 转后台
提示、--watch 非终态超时、--cancel 存在任务的终态落盘、--resume 恢复后跑完。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.task as task_mod
from agents.agent import Agent
from agents.config import Config
from agents.llm import ChatResult, LLMClient
from agents.task_store import TERMINAL_STATUSES, TaskRecord, TaskStore
from agents.tools import Tool, ToolContext


# ---------------- 离线桩（与 tests/test_task_runner.py 同款写法） ----------------

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
) -> Tool:
    """可门控探针工具：gates[query] 未放行则阻塞（超时兜底），用于钉住中间状态。"""
    def probe(ctx, query=""):
        if exec_log is not None:
            exec_log.append(query)
        if gates is not None and query in gates:
            gates[query].wait(timeout=5)
        return {"text": f"{query} 的检索结果", "sources": [f"kb://{query}.md"]}

    return _make_tool("probe", probe, {"query": {"type": "string"}}, ["query"])


def _make_agent(td: str, llm: ScriptedLLM, tools: dict[str, Tool]) -> Agent:
    """离线 Agent：tmp data_dir、tfidf 记忆后端、不加载真实索引。"""
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


def _plan_json(steps: list[dict], summary: str = "测试计划") -> str:
    return json.dumps({"reasoning": "测试规划", "plan_summary": summary, "steps": steps}, ensure_ascii=False)


PLAN_2STEPS = _plan_json([
    {"action": "probe", "input": {"query": "q1"}, "purpose": "第一步"},
    {"action": "probe", "input": {"query": "q2"}, "purpose": "第二步"},
], "两步检索")
PLAN_Q1 = _plan_json([{"action": "probe", "input": {"query": "q1"}, "purpose": "p"}], "单步 q1")

SINGLE_STEP_PLAN_DICT = {
    "reasoning": "r", "plan_summary": "单步", "fallback": False, "rounds": 1, "reflections": [],
    "steps": [{"action": "probe", "input": {"query": "r1"}, "purpose": "p", "step_id": 1}],
}


def _capture(func, *args, **kwargs):
    """捕获 stdout/stderr 并返回 (退出码, out, err)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = func(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """轮询等待条件成立（超时返回当时判定结果），不依赖裸 sleep 猜时序。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TaskCliTests(unittest.TestCase):
    """spec ACC-T3-01..03 + 附加场景（无 Key / Ctrl+C / watch 超时 / cancel / resume / main 分发）。"""

    # ---------------- ACC-T3-01 --list ----------------

    def test_acc_t3_01_list(self):
        """ACC-T3-01：--list 列出 store 中预置任务（摘要 24 字截断、倒序）；空库打印「暂无任务」。"""
        with tempfile.TemporaryDirectory() as td:
            # 空库
            code, out, _ = _capture(task_mod.run_list, Path(td) / "empty")
            self.assertEqual(code, 0)
            self.assertIn("暂无任务", out)

            # 预置两个任务：显式给 updated_at 保证倒序可断言
            store = TaskStore(Path(td) / "tasks")
            long_q = "这是一个明显超过二十四个字符的问题，用来验证列表摘要截断是否生效"
            self.assertGreater(len(long_q), 24)
            tid_done = store.create(TaskRecord(
                session_id="s", workspace_id="w", question=long_q, status="completed",
                final_answer="答案", updated_at="2026-08-01T00:00:01+08:00",
            ))
            tid_run = store.create(TaskRecord(
                session_id="s", workspace_id="w", question="较短的问题", status="running",
                updated_at="2026-08-01T00:00:02+08:00",
            ))
            code, out, _ = _capture(task_mod.run_list, Path(td) / "tasks")
            self.assertEqual(code, 0)
            self.assertIn(tid_done, out)
            self.assertIn(tid_run, out)
            self.assertIn("已完成", out)
            self.assertIn("执行中", out)
            self.assertIn(long_q[:24], out)
            self.assertIn("…", out)
            self.assertNotIn(long_q, out, "列表不应打印问题全文")
            self.assertLess(out.index(tid_run), out.index(tid_done), "应按 updated_at 倒序")

        print("✓ ACC-T3-01 --list 列出预置任务（状态/摘要 24 字/倒序），空库打印「暂无任务」")

    # ---------------- ACC-T3-02 --question 全流程 ----------------

    def test_acc_t3_02_question_flow(self):
        """ACC-T3-02：--question 创建→轮询→completed，输出含状态行、✓ 步骤、最终答案与 usage。"""
        with tempfile.TemporaryDirectory() as td:
            store_dir = Path(td) / "tasks"
            # 门控第一步：确保轮询期间能观察到「执行中」（工作线程阻塞在 q1 的工具调用里）
            gates = {"q1": threading.Event()}
            agent = _make_agent(td, ScriptedLLM([PLAN_2STEPS, "这是最终答案[1][2]"], Config({})),
                                {"probe": _probe_tool(gates)})
            release = threading.Timer(0.2, gates["q1"].set)  # 延迟放行，让轮询先观察到中间状态
            release.start()
            try:
                code, out, _ = _capture(
                    task_mod.run_create, "两步任务",
                    store_dir=store_dir, agent=agent, poll_interval=0.01, timeout=10,
                )
            finally:
                release.join()
            self.assertEqual(code, 0, f"应正常完成: {out}")
            self.assertIn("任务已创建", out)
            self.assertIn("执行中", out, f"轮询应观察到 running 状态: {out}")
            self.assertIn("已完成", out)
            self.assertIn("✓", out)
            self.assertIn("probe", out)
            self.assertIn("这是最终答案[1][2]", out, "应打印最终答案全文")
            self.assertIn("参考来源", out)
            self.assertIn("kb://q1.md", out)
            self.assertIn("kb://q2.md", out)
            self.assertIn("tokens in=", out)
            self.assertIn("估算成本", out, "应打印 token/成本 usage")

            # 落盘结果与输出一致
            records = TaskStore(store_dir).list()
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec.status, "completed")
            self.assertEqual(rec.question, "两步任务")
            self.assertEqual(rec.final_answer, "这是最终答案[1][2]")
            self.assertEqual(rec.sources, ["kb://q1.md", "kb://q2.md"])
            self.assertIn("prompt_tokens", rec.usage)
            self.assertIn("completion_tokens", rec.usage)
            self.assertIn("cost_yuan", rec.usage)

        print("✓ ACC-T3-02 --question 创建→轮询→completed，输出状态/步骤/答案/来源/usage")

    # ---------------- ACC-T3-03 不存在 id / 非 paused 恢复 ----------------

    def test_acc_t3_03_missing_id_and_bad_resume(self):
        """ACC-T3-03：--watch/--resume/--cancel 对不存在 id 友好报错并退出 1；
        --resume 对非 paused 任务给出原因退出 1（均在触碰 runner/Agent 之前拦截）。"""
        with tempfile.TemporaryDirectory() as td:
            store_dir = Path(td) / "tasks"
            missing = "000000000000"  # 合法格式但不存在的 id
            for func in (task_mod.run_watch, task_mod.run_resume, task_mod.run_cancel):
                code, out, _ = _capture(func, missing, store_dir=store_dir)
                self.assertEqual(code, 1, f"{func.__name__} 对不存在 id 应返回 1")
                self.assertIn(f"任务不存在: {missing}", out, f"应打印友好错误: {out}")

            # 非法 id（含路径穿越）同样视为不存在
            code, out, _ = _capture(task_mod.run_watch, "../evil", store_dir=store_dir)
            self.assertEqual(code, 1)
            self.assertIn("任务不存在", out)

            # --resume 非 paused：completed 任务给出当前状态原因
            store = TaskStore(store_dir)
            tid = store.create(TaskRecord(
                session_id="s", workspace_id="w", question="已完成任务",
                status="completed", final_answer="答案",
            ))
            code, out, _ = _capture(task_mod.run_resume, tid, store_dir=store_dir)
            self.assertEqual(code, 1)
            self.assertIn("已完成", out, f"原因应包含当前状态: {out}")
            self.assertIn("paused", out)
            self.assertIn("恢复", out)

        print("✓ ACC-T3-03 watch/resume/cancel 不存在 id 报错退出 1；resume 非 paused 给出原因")

    # ---------------- 附加：--cancel 存在任务 ----------------

    def test_cancel_existing_task(self):
        """--cancel 存在的任务：经 runner.cancel 落盘 canceled 并打印终态；重复取消幂等提示。"""
        with tempfile.TemporaryDirectory() as td:
            store_dir = Path(td) / "tasks"
            store = TaskStore(store_dir)
            tid = store.create(TaskRecord(session_id="s", workspace_id="w", question="待取消任务", status="paused"))
            runner = task_mod._build_runner(store, lambda: None)  # 取消不执行步骤，无需真实 Agent
            code, out, _ = _capture(task_mod.run_cancel, tid, store_dir=store_dir, runner=runner)
            self.assertEqual(code, 0)
            self.assertEqual(store.get(tid).status, "canceled", "应经 runner.cancel 落盘 canceled 终态")
            self.assertIn("已取消", out, f"应打印终态: {out}")

            # 已是终态：提示无需取消并打印终态，退出 0
            code, out, _ = _capture(task_mod.run_cancel, tid, store_dir=store_dir, runner=runner)
            self.assertEqual(code, 0)
            self.assertIn("无需取消", out)
            self.assertIn("已取消", out)

        print("✓ --cancel 经 runner.cancel 落盘 canceled 并打印终态；终态任务提示无需取消")

    # ---------------- 附加：--resume 恢复后跑完 ----------------

    def test_resume_paused_task_flow(self):
        """--resume paused 任务：经真实 TaskRunner 恢复（沿用持久化计划，不重新规划）→ completed。"""
        with tempfile.TemporaryDirectory() as td:
            store_dir = Path(td) / "tasks"
            store = TaskStore(store_dir)
            # 遗留 running 任务（模拟上次中断）→ recover_running → paused（CLI resume 的前置状态）
            tid = store.create(TaskRecord(
                session_id="cli", workspace_id="default", question="恢复任务",
                status="running", plan=dict(SINGLE_STEP_PLAN_DICT),
            ))
            self.assertEqual(store.recover_running(), [tid])

            agent = _make_agent(td, ScriptedLLM(["恢复后的答案[1]"], Config({})), {"probe": _probe_tool()})
            runner = task_mod._build_runner(store, agent)
            code, out, _ = _capture(
                task_mod.run_resume, tid, store_dir=store_dir, runner=runner, poll_interval=0.01, timeout=10
            )
            self.assertEqual(code, 0, f"恢复后应完成: {out}")
            self.assertIn("已恢复执行", out)
            self.assertIn("已完成", out)
            self.assertIn("恢复后的答案[1]", out)
            rec = store.get(tid)
            self.assertEqual(rec.status, "completed")
            self.assertEqual(rec.final_answer, "恢复后的答案[1]")
            self.assertEqual(len(rec.steps), 1)
            self.assertTrue(rec.steps[0]["ok"], "持久化步骤应被执行并落盘")

        print("✓ --resume paused 任务经轮询到 completed，输出最终答案且步骤落盘")

    # ---------------- 附加：无 API Key ----------------

    def test_no_api_key_exit(self):
        """附加：config 无 api_key（且环境无 DEEPSEEK_API_KEY）时 run_create 打印中文错误并退出 1。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = Config({"data_dir": td, "memory": {"embedding_backend": "tfidf"}})  # 无 llm.api_key
            saved = os.environ.pop("DEEPSEEK_API_KEY", None)
            out_buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(out_buf):
                    try:
                        task_mod.run_create("问题", store_dir=Path(td) / "tasks", config=cfg)
                        self.fail("无 Key 时应报错退出")
                    except SystemExit as exc:
                        self.assertEqual(exc.code, 1, f"退出码应为 1: {exc.code}")
            finally:
                if saved is not None:
                    os.environ["DEEPSEEK_API_KEY"] = saved
            out = out_buf.getvalue()
            self.assertIn("错误", out, f"应打印中文错误提示: {out}")
            self.assertIn("API Key", out, f"应打印中文错误提示: {out}")
            self.assertFalse(list(Path(td).iterdir()), "失败发生在装配期，不应产生任何存储副作用")

        print("✓ 无 API Key 时打印中文错误并 exit 1（装配期拦截，无副作用）")

    # ---------------- 附加：Ctrl+C 转后台 ----------------

    def test_keyboard_interrupt_background_message(self):
        """--question 轮询中 Ctrl+C：打印「任务转入后台，可稍后 --watch 查看」并返回 0（daemon 线程）。"""
        with tempfile.TemporaryDirectory() as td:
            store_dir = Path(td) / "tasks"
            gate = threading.Event()
            agent = _make_agent(td, ScriptedLLM([PLAN_Q1, "门控答案"], Config({})), {"probe": _probe_tool({"q1": gate})})
            orig_sleep = time.sleep

            def interrupt_sleep(_seconds):
                raise KeyboardInterrupt  # 首次轮询等待即触发，模拟用户 Ctrl+C

            task_mod.time.sleep = interrupt_sleep
            try:
                code, out, _ = _capture(task_mod.run_create, "门控任务", store_dir=store_dir, agent=agent, poll_interval=0.01)
            finally:
                task_mod.time.sleep = orig_sleep
            self.assertEqual(code, 0, f"Ctrl+C 应返回 0: {out}")
            self.assertIn("任务已创建", out)
            self.assertIn("任务转入后台，可稍后 --watch 查看", out, f"应打印转后台提示: {out}")

            # 清理：放行被门控阻塞的工作线程，等待任务自然到终态（不遗留阻塞线程）
            gate.set()
            store = TaskStore(store_dir)
            tid = store.list()[0].task_id
            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status in TERMINAL_STATUSES))

        print("✓ Ctrl+C 打印「任务转入后台，可稍后 --watch 查看」并 exit 0（daemon 线程不阻塞）")

    # ---------------- 附加：--watch 非终态超时 ----------------

    def test_watch_timeout_returns_nonzero(self):
        """--watch 未到终态且超时：返回 1 并提示稍后继续（不打断执行中的任务）。"""
        with tempfile.TemporaryDirectory() as td:
            store_dir = Path(td) / "tasks"
            store = TaskStore(store_dir)
            gate = threading.Event()
            agent = _make_agent(td, ScriptedLLM([PLAN_Q1, "超时观察答案"], Config({})), {"probe": _probe_tool({"q1": gate})})
            runner = task_mod._build_runner(store, agent)
            tid = runner.submit("cli", "default", "超时观察任务")
            self.assertTrue(
                _wait_until(lambda: (store.get(tid) or TaskRecord()).status == "running"), "任务应进入 running"
            )

            code, out, _ = _capture(task_mod.run_watch, tid, store_dir=store_dir, timeout=0.3, poll_interval=0.01)
            self.assertEqual(code, 1, f"未到终态超时应返回 1: {out}")
            self.assertIn("等待超时", out)
            self.assertIn(tid, out)
            self.assertEqual(store.get(tid).status, "running", "超时观察不应打断任务执行")

            gate.set()  # 清理：放行工作线程，等待任务自然到终态
            self.assertTrue(_wait_until(lambda: (store.get(tid) or TaskRecord()).status in TERMINAL_STATUSES))

        print("✓ --watch 非终态超时返回 1 并提示，不打断任务执行")

    # ---------------- 附加：main() 参数规则与分发 ----------------

    def test_main_argparse_dispatch(self):
        """main() 分发：缺参打印用法退出 1；互斥多参退出 1；--list --task-store 正常分发退出 0。"""
        with tempfile.TemporaryDirectory() as td:
            saved_argv = sys.argv
            try:
                # 缺参：打印用法并退出 1
                sys.argv = ["task.py"]
                out_buf = io.StringIO()
                with contextlib.redirect_stdout(out_buf):
                    try:
                        task_mod.main()
                        self.fail("无参数应退出 1")
                    except SystemExit as exc:
                        self.assertEqual(exc.code, 1)
                self.assertIn("usage", out_buf.getvalue().lower(), "应打印用法")

                # 互斥：同时给多个动作参数 → 退出 1
                sys.argv = ["task.py", "--list", "--watch", "abc"]
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        task_mod.main()
                        self.fail("互斥参数应退出 1")
                    except SystemExit as exc:
                        self.assertEqual(exc.code, 1)

                # 正常分发：--list + 存储目录覆盖 → 退出 0（空库文案）
                sys.argv = ["task.py", "--list", "--task-store", str(Path(td) / "tasks")]
                out_buf = io.StringIO()
                with contextlib.redirect_stdout(out_buf):
                    try:
                        task_mod.main()
                        self.fail("--list 应退出 0")
                    except SystemExit as exc:
                        self.assertEqual(exc.code, 0)
                self.assertIn("暂无任务", out_buf.getvalue())
            finally:
                sys.argv = saved_argv

        print("✓ main() 分发：缺参/互斥退出 1（含用法），--list --task-store 退出 0")


if __name__ == "__main__":
    unittest.main()
