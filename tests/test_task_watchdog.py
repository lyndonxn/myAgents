"""G7 任务看门狗测试（P1-2）。

全部离线：任务库用 tempfile；stub agent（脚本化 plan_only/finish_task + 假工具）。
覆盖：心跳字段刷新、watchdog 心跳停滞转 paused、活跃任务不被误扫、恢复可行、
canceled 不可 resume、非法状态迁移明确报错、任务级总超时停止推进、stale 判定
（心跳缺失回退 updated_at）。
"""
from __future__ import annotations

import queue
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import task_runner as task_runner_mod
from agents.agent import Agent
from agents.config import Config
from agents.memory import SessionMemory
from agents.planner import Plan, PlanStep
from agents.task_runner import TaskRunner
from agents.task_store import STATUS_PAUSED, STATUS_RUNNING, TaskRecord, TaskStore
from agents.tools import Tool, ToolContext


def kb_tool(func):
    return Tool(
        name="search_knowledge_base", description="测试 KB 工具",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        func=func,
    )


def make_stub(kb_func=None):
    """脚本化 stub agent：默认 KB 工具命中，可注入自定义工具实现。"""
    tool = kb_tool(kb_func or (lambda ctx, query="": {"text": f"检索 {query} 的片段", "sources": ["kb://a.md"], "hit_count": 2}))

    def fake_plan_one_step(question, session_id="", allow_web=None):
        return (Plan(plan_summary="检索一步",
                     steps=[PlanStep(action="search_knowledge_base", input={"query": "RAG"}, step_id=1)]),
                question, "")

    def fake_finish(question, plan, steps, history_text=""):
        return "任务答案[1]", ["kb://a.md"], NS(valid_count=1, invalid_count=0)

    stub = NS(
        config=Config({"tools": {"max_retries": 0}}),
        tools={"search_knowledge_base": tool},
        _ctx=ToolContext(),
        memory=SessionMemory(),
        prompt_tokens=0, completion_tokens=0, estimated_cost=0.0,
        plan_only=fake_plan_one_step,
        finish_task=fake_finish,
    )
    stub.resolve_allow_web = MethodType(Agent.resolve_allow_web, stub)
    return stub


def make_runner(store: TaskStore, stub=None, **kwargs) -> TaskRunner:
    """离线 runner：测试直接调 _execute/sweep_once，替换 _queue 摘除异步消费。"""
    runner = TaskRunner(store, threading.Lock(), agent_provider=lambda: stub or make_stub(),
                        memory_provider=lambda sid: SessionMemory(), **kwargs)
    runner._queue = queue.Queue()
    return runner


class WatchdogTests(unittest.TestCase):
    """P1-2 验收：超时收敛、可恢复、状态机明确。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = TaskStore(self.tmp / "tasks")

    def tearDown(self):
        self._tmp.cleanup()

    def _running_record(self, heartbeat_at: str = "") -> TaskRecord:
        record = TaskRecord(task_id="", session_id="s1", workspace_id="w1", question="卡死任务",
                            status=STATUS_RUNNING, heartbeat_at=heartbeat_at)
        self.store.create(record)
        return record

    def test_stale_running_swept_to_paused_and_resumable(self):
        """心跳停滞的 running → watchdog 转 paused（不永久 running）；resume 可恢复。"""
        stale_ts = (datetime.now().astimezone() - timedelta(seconds=120)).isoformat(timespec="seconds")
        record = self._running_record(heartbeat_at=stale_ts)

        runner = make_runner(self.store, step_timeout_s=60, total_timeout_s=3600, watchdog_interval_s=5)
        self.assertEqual(runner.sweep_once(), [record.task_id])

        after = self.store.get(record.task_id)
        self.assertEqual(after.status, STATUS_PAUSED)
        self.assertIn("看门狗", after.error)

        # 恢复可行：paused → queued（队列已摘除，不会被消费执行）
        self.assertEqual(runner.resume(record.task_id), "queued")

        print("✓ 心跳停滞任务被 watchdog 转 paused，且可 resume 恢复")

    def test_fresh_heartbeat_not_swept(self):
        """心跳新鲜的 running 任务不被看门狗误扫。"""
        fresh_ts = datetime.now().astimezone().isoformat(timespec="seconds")
        self._running_record(heartbeat_at=fresh_ts)

        runner = make_runner(self.store, step_timeout_s=60)
        self.assertEqual(runner.sweep_once(), [])

        print("✓ 心跳新鲜的 running 任务不被误扫")

    def test_stale_fallback_to_updated_at(self):
        """心跳缺失（旧记录）时回退 updated_at 判定（兼容升级存量）。"""
        stale_ts = (datetime.now().astimezone() - timedelta(seconds=3600)).isoformat(timespec="seconds")
        record = TaskRecord(task_id="", session_id="s1", workspace_id="w1", question="旧记录",
                            status=STATUS_RUNNING, updated_at=stale_ts, created_at=stale_ts)
        self.store.create(record)
        self.assertEqual(self.store.stale_running_ids(60), [record.task_id])

        print("✓ 心跳缺失回退 updated_at 判定")

    def test_cancel_not_revivable_by_resume(self):
        """取消是终态：resume 抛出明确 ValueError，不会被复活。"""
        runner = make_runner(self.store)
        tid = runner.submit("s1", "w1", "要取消的任务")
        runner.cancel(tid)

        with self.assertRaises(ValueError) as ctx:
            runner.resume(tid)
        self.assertIn("不允许该操作", str(ctx.exception))

        print("✓ cancel 后 resume 被明确拒绝（终态不复活）")

    def test_illegal_transition_clear_error(self):
        """非法状态迁移返回明确错误：completed 任务不可 pause。"""
        runner = make_runner(self.store)
        tid = runner.submit("s1", "w1", "任务")
        record = self.store.get(tid)
        record.status = "completed"
        self.store.update(record)

        with self.assertRaises(ValueError) as ctx:
            runner.pause(tid)
        self.assertIn("completed", str(ctx.exception))

        print("✓ 非法状态迁移报错含当前状态")

    def test_heartbeat_fields_during_execution(self):
        """执行期间心跳字段刷新：heartbeat_at/current_step 落盘；失败步骤记录 last_error_type。"""
        def broken_kb(ctx, query=""):
            raise RuntimeError("索引损坏")

        stub = make_stub(kb_func=broken_kb)
        runner = make_runner(self.store, stub=stub)

        record_obj = TaskRecord(task_id="", session_id="s1", workspace_id="w1", question="任务问题",
                                status=STATUS_RUNNING)
        self.store.create(record_obj)
        record = self.store.get(record_obj.task_id)
        runner._execute(record, threading.Event(), threading.Event())

        after = self.store.get(record.task_id)
        self.assertTrue(after.heartbeat_at, "执行后心跳时间应已刷新")
        self.assertTrue(after.current_step, "当前步骤摘要应已记录")
        self.assertEqual(after.last_error_type, "RuntimeError", "失败步骤应记录错误类型")
        self.assertEqual(after.status, "completed", "工具失败不中断任务（任务级状态仍完成）")

        print("✓ 执行期间心跳/步骤摘要落盘，失败步骤记录 last_error_type")

    def test_total_timeout_stops_progress(self):
        """任务级总超时：超出后不再推进剩余步骤，转 paused 且原因可读。"""
        stub = make_stub()

        def fake_plan_two_steps(question, session_id="", allow_web=None):
            return (Plan(plan_summary="两步",
                         steps=[PlanStep(action="search_knowledge_base", input={"query": "a"}, step_id=1),
                                PlanStep(action="search_knowledge_base", input={"query": "b"}, step_id=2)]),
                    question, "")

        stub.plan_only = fake_plan_two_steps
        runner = make_runner(self.store, stub=stub, total_timeout_s=10)

        record_obj = TaskRecord(task_id="", session_id="s1", workspace_id="w1", question="超时任务",
                                status=STATUS_RUNNING)
        self.store.create(record_obj)
        record = self.store.get(record_obj.task_id)

        base = task_runner_mod.time.monotonic()
        clock = [base] + [base + 100] * 50  # 首次取起点，其后一律已超时
        with mock.patch.object(task_runner_mod.time, "monotonic", side_effect=clock):
            runner._execute(record, threading.Event(), threading.Event())

        after = self.store.get(record.task_id)
        self.assertEqual(after.status, STATUS_PAUSED)
        self.assertIn("总超时", after.error)
        self.assertEqual(len(after.steps), 0, "超时后不应执行任何步骤")

        print("✓ 任务级总超时：停止推进、转 paused、原因可读")


if __name__ == "__main__":
    unittest.main()
