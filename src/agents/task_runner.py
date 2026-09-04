"""任务运行器（S5）：后台单线程执行「规划 → 逐步执行 → 合成」长任务，支持暂停/恢复/取消。

- 状态持久化：TaskRecord 每次状态变化即落盘；每步结果（含 attempts/degraded/
  失败节点）完成即写盘（ACC-S5-01 子任务进度/中间结果/失败节点）。
- 暂停/恢复：pause 对 running 任务设置 per-task threading.Event，工作线程在步骤
  边界检查后置 paused 并退出（合成阶段前同样设检查点，合成阶段亦可暂停）；
  resume 只对 paused 任务生效——清暂停事件、重新入队，工作线程从持久化 steps
  恢复 Executor._history（重建 StepResult），跳过已 ok 步骤（失败步骤重试执行），
  不做反思，直接执行剩余步骤后合成（ACC-S5-02/03）。
- 取消：对 queued/running/paused 均有效；canceled 为终态，不可 resume（ACC-S5-04）。
- 与 /api/ask 互斥：「重建会话记忆 + 规划」「每一步执行」「合成」都在与 ask 相同的
  锁（lock）内执行，会话记忆经 memory_provider 在锁内重建，防记忆串线。
- 复用 Agent 现有件：规划用 agent.plan_only，合成用 agent.finish_task（agent.py
  纯新增拆解件，ask 行为不变）；执行器/规划器签名不变。
- 完成回调（T1）：可选 on_complete(record) 在任务到达 completed 终态（落盘后）于
  工作线程调用一次，供 web 层把答案写回会话消息；回调抛任何异常都被吞掉并告警，
  不影响任务终态（paused/failed/canceled 不回调）。回调内部如需与 /api/ask 互斥，
  由调用方的回调自行加锁，runner 不代持。

工作线程为单个 daemon 线程，从 queue.Queue 取任务；submit 落盘（queued）并入队后
立即返回 task_id。服务启动时由调用方先执行 TaskStore.recover_running()。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable

from . import audit as audit_mod
from .agent import Agent
from .executor import Executor, StepResult
from .logger import get_logger
from .memory import SessionMemory
from .planner import Plan, PlanStep
from .task_store import (
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TaskRecord,
    TaskStore,
    _now,
)

LOG = get_logger("task_runner")


# ---------------- Plan/StepResult 与持久化 dict 的互转 ----------------

def _json_safe(value):
    """递归把任意工具输出转换为可 JSON 序列化的结构（不可序列化对象转字符串）。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        return str(value)


def plan_to_dict(plan: Plan) -> dict:
    """序列化 Plan（存入 TaskRecord.plan）。"""
    return {
        "reasoning": plan.reasoning,
        "plan_summary": plan.plan_summary,
        "fallback": plan.fallback,
        "rounds": plan.rounds,
        "reflections": list(plan.reflections),
        "steps": [
            {"action": s.action, "input": dict(s.input or {}), "purpose": s.purpose, "step_id": s.step_id}
            for s in plan.steps
        ],
    }


def plan_from_dict(data: dict) -> Plan:
    """从 TaskRecord.plan 反序列化 Plan（恢复路径用）。"""
    data = data if isinstance(data, dict) else {}
    steps: list[PlanStep] = []
    for raw in data.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        try:
            step_id = int(raw.get("step_id", 0))
        except (TypeError, ValueError):
            step_id = 0
        steps.append(
            PlanStep(
                action=str(raw.get("action", "") or "none"),
                input=dict(raw.get("input") or {}),
                purpose=str(raw.get("purpose", "")),
                step_id=step_id,
            )
        )
    try:
        rounds = int(data.get("rounds", 1))
    except (TypeError, ValueError):
        rounds = 1
    return Plan(
        reasoning=str(data.get("reasoning", "")),
        steps=steps,
        plan_summary=str(data.get("plan_summary", "")),
        fallback=bool(data.get("fallback", False)),
        reflections=[str(r) for r in (data.get("reflections") or [])],
        rounds=rounds,
    )


def step_to_dict(result: StepResult) -> dict:
    """序列化 StepResult（含 attempts/degraded；output 经 JSON 安全化）。"""
    return {
        "step_id": result.step_id,
        "action": result.action,
        "input": _json_safe(dict(result.input or {})),
        "purpose": result.purpose,
        "output": _json_safe(result.output),
        "error": result.error,
        "latency_s": result.latency_s,
        "ok": result.ok,
        "attempts": result.attempts,
        "degraded": result.degraded,
    }


def step_from_dict(data: dict) -> StepResult:
    """从持久化步骤 dict 重建 StepResult（恢复路径重建 Executor._history 用）。"""
    data = data if isinstance(data, dict) else {}

    def _int(key: str, default: int) -> int:
        try:
            return int(data.get(key, default))
        except (TypeError, ValueError):
            return default

    try:
        latency = float(data.get("latency_s", 0.0))
    except (TypeError, ValueError):
        latency = 0.0
    return StepResult(
        step_id=_int("step_id", 0),
        action=str(data.get("action", "")),
        input=dict(data.get("input") or {}),
        purpose=str(data.get("purpose", "")),
        output=data.get("output"),
        error=str(data.get("error", "")),
        latency_s=latency,
        ok=bool(data.get("ok", True)),
        attempts=max(1, _int("attempts", 1)),
        degraded=bool(data.get("degraded", False)),
    )


class TaskRunner:
    """后台任务运行器：单工作线程 + 待办队列 + 每任务暂停/取消事件。

    store: 任务持久化存储；lock: 与 /api/ask 相同的互斥锁（web 传 Handler.lock）；
    agent_provider: 返回执行用 Agent 的 callable（web 为 Handler.agent，测试注入桩）；
    memory_provider: session_id -> SessionMemory（web 为 WebStore.memory，含摘要恢复；
    为 None 或会话为空时用全新空会话记忆，保证任务路径不串用其他会话记忆）。
    on_complete: 可选完成回调（T1），completed 终态落盘后在工作线程调用一次，
    异常被吞掉并告警；paused/failed/canceled 不调用。
    """

    def __init__(
        self,
        store: TaskStore,
        lock: threading.Lock,
        agent_provider: Callable[[], Agent | None],
        memory_provider: Callable[[str], SessionMemory | None] | None = None,
        on_complete: Callable[[TaskRecord], None] | None = None,
        step_timeout_s: float = 600.0,
        total_timeout_s: float = 3600.0,
        watchdog_interval_s: float = 30.0,
    ):
        self.store = store
        self.lock = lock
        self.agent_provider = agent_provider
        self.memory_provider = memory_provider
        self.on_complete = on_complete
        # P1-2 看门狗阈值：步骤级（心跳停滞判定卡死）与任务级总时长；watchdog 扫描间隔
        self.step_timeout_s = max(1.0, float(step_timeout_s))
        self.total_timeout_s = max(1.0, float(total_timeout_s))
        self.watchdog_interval_s = max(1.0, float(watchdog_interval_s))
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._ctrl_lock = threading.Lock()
        self._pause_events: dict[str, threading.Event] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        # G3：按 task_id 挂本次任务的联网/记忆许可（submit 透传，不新增任务记录字段；
        # 终态清理。进程重启后丢失——恢复路径按持久化计划执行，不再重新规划）
        self._egress: dict[str, dict] = {}
        self._worker = threading.Thread(target=self._loop, name="task-runner", daemon=True)
        self._worker.start()

    # ---- 外部 API ----

    def submit(
        self,
        session_id: str,
        workspace_id: str,
        question: str,
        remember: bool | None = None,
        allow_web: bool | None = None,
    ) -> str:
        """提交任务：以 queued 落盘并入队，返回 task_id。

        G3：remember/allow_web 为本次任务的请求级外发许可（缺省 None → 按配置），
        仅在运行器内存侧按 task_id 透传到规划与执行阶段：
        - allow_web → plan_only 的规划工具清单 + Executor 的 KB→Web 降级许可；
        - remember 透传保存（任务路径本身不写长期记忆，供记忆治理切片对接）。
        """
        record = TaskRecord(session_id=session_id, workspace_id=workspace_id, question=question, status=STATUS_QUEUED)
        task_id = self.store.create(record)
        with self._ctrl_lock:
            self._pause_events[task_id] = threading.Event()
            self._cancel_events[task_id] = threading.Event()
            self._egress[task_id] = {"remember": remember, "allow_web": allow_web}
        self._queue.put(task_id)
        LOG.info("任务已提交: %s | q=%s", task_id, question[:60].replace("\n", " "))
        return task_id

    def pause(self, task_id: str) -> str:
        """暂停：running 任务设置事件、在下一/当前步骤边界生效；queued 任务直接置 paused。

        返回调用时刻的任务状态（running 任务的落盘状态会在步骤边界变为 paused，
        调用方可用 GET /api/tasks/{id} 轮询确认）。终态任务不可暂停。
        """
        record = self._require(task_id, allow=(STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED))
        if record.status == STATUS_PAUSED:
            return STATUS_PAUSED
        self._pause_event(task_id).set()
        if record.status == STATUS_QUEUED:
            # 尚未被工作线程拾取：直接置 paused（工作线程出队时跳过；resume 重新入队）
            record.status = STATUS_PAUSED
            self.store.update(record)
            LOG.info("任务已暂停（排队中）: %s", task_id)
            return STATUS_PAUSED
        latest = self.store.get(task_id)
        LOG.info("暂停已受理（步骤边界生效）: %s", task_id)
        return latest.status if latest else record.status

    def resume(self, task_id: str) -> str:
        """恢复：仅对 paused 任务生效——清暂停事件、置 queued 重新入队。

        工作线程会从持久化 steps 恢复：跳过已 ok 步骤、重建执行历史保留
        @step:N 引用能力，只执行剩余步骤后直接合成。canceled 等终态不可恢复。
        """
        record = self._require(task_id, allow=(STATUS_PAUSED,))
        with self._ctrl_lock:
            self._pause_events.setdefault(task_id, threading.Event()).clear()
            self._cancel_events.setdefault(task_id, threading.Event())
        record.status = STATUS_QUEUED
        self.store.update(record)
        self._queue.put(task_id)
        LOG.info("任务已恢复并重新入队: %s", task_id)
        return STATUS_QUEUED

    def cancel(self, task_id: str) -> str:
        """取消：queued/running/paused 均有效，立即落盘 canceled（终态，不可 resume）。

        running 任务的在途步骤会先执行完，工作线程在下一检查点进入取消收尾
        （不再合成）；重复取消幂等返回 canceled。
        """
        record = self._require(task_id, allow=(STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED, STATUS_CANCELED))
        self._cancel_event(task_id).set()
        if record.status != STATUS_CANCELED:
            record.status = STATUS_CANCELED
            self.store.update(record)
            LOG.info("任务已取消: %s", task_id)
        self._cleanup_events(task_id)  # 工作线程持有事件本地引用，不受影响
        return STATUS_CANCELED

    # ---- 控制事件 ----

    def _pause_event(self, task_id: str) -> threading.Event:
        with self._ctrl_lock:
            return self._pause_events.setdefault(task_id, threading.Event())

    def _cancel_event(self, task_id: str) -> threading.Event:
        with self._ctrl_lock:
            return self._cancel_events.setdefault(task_id, threading.Event())

    def _cleanup_events(self, task_id: str) -> None:
        with self._ctrl_lock:
            self._pause_events.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
            self._egress.pop(task_id, None)

    def _require(self, task_id: str, allow: tuple[str, ...]) -> TaskRecord:
        """读取任务并校验状态是否允许该操作：不存在抛 KeyError，状态不符抛 ValueError。"""
        record = self.store.get(task_id)
        if record is None:
            raise KeyError(f"任务不存在: {task_id}")
        if record.status not in allow:
            raise ValueError(f"任务当前状态为 {record.status}，不允许该操作")
        return record

    # ---- 工作线程 ----

    def _loop(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                self._run(task_id)
            except Exception as exc:  # noqa: BLE001 - 工作线程绝不能死
                LOG.exception("任务 %s 执行异常", task_id)
                self._mark_failed(task_id, f"{type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()

    def _mark_failed(self, task_id: str, error: str) -> None:
        """兜底失败落盘：仅当任务仍在 queued/running（未被并发迁移）时改写。"""
        record = self.store.get(task_id)
        if record is not None and record.status in (STATUS_QUEUED, STATUS_RUNNING):
            record.status = STATUS_FAILED
            record.error = error
            record.last_error_type = error.split(":", 1)[0] if error else "Error"
            self.store.update(record)
        self._cleanup_events(task_id)

    # ---- 看门狗（P1-2） ----

    def _heartbeat(self, record: TaskRecord, current_step: str) -> None:
        """刷新心跳时间与当前步骤摘要（调用方随后自行 store.update 或由本方法落盘）。"""
        record.heartbeat_at = _now()
        record.current_step = str(current_step or "")[:120]

    def start_watchdog(self) -> None:
        """启动看门狗 daemon 线程：周期扫描把心跳停滞/总超时的 running 任务转为 paused。

        paused 而非 failed：卡死多源于 worker 线程被杀/进程冻结等可恢复场景，
        用户可 resume 从持久化步骤继续（幂等：已有 paused/终态不会被二次改写）。
        """
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="task-watchdog", daemon=True)
        self._watchdog.start()
        LOG.info(
            "任务看门狗已启动: 间隔 %ss / 步骤超时 %ss / 总超时 %ss",
            self.watchdog_interval_s, self.step_timeout_s, self.total_timeout_s,
        )

    def stop_watchdog(self) -> None:
        """停止看门狗线程（测试与关停用）。"""
        self._watchdog_stop.set()

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self.watchdog_interval_s):
            try:
                self.sweep_once()
            except Exception:  # noqa: BLE001 - 看护异常不中断扫描
                LOG.exception("任务看门狗扫描异常")

    def sweep_once(self) -> list[str]:
        """单次扫描：返回本次被转为 paused 的任务 id。

        判定口径：running 且 (heartbeat_at 缺失则回退 updated_at) 停滞超过
        step_timeout_s。同时该任务从创建起超过 total_timeout_s 时也一并收容。
        """
        swept: list[str] = []
        for task_id in self.store.stale_running_ids(self.step_timeout_s):
            record = self.store.get(task_id)
            if record is None or record.status != STATUS_RUNNING:
                continue
            record.status = STATUS_PAUSED
            record.error = (
                f"看门狗：步骤心跳停滞超过 {int(self.step_timeout_s)}s，已暂停可恢复"
                f"（current_step={record.current_step or '未知'}）"
            )
            record.last_error_type = record.last_error_type or "WatchdogStall"
            self.store.update(record)
            self._cleanup_events(task_id)
            swept.append(task_id)
            LOG.warning("看门狗把卡死任务转为 paused: %s | step=%s", task_id, record.current_step)
            audit = audit_mod.get()
            if audit is not None:
                try:
                    audit.log_admin("task_watchdog_pause", ok=True, session_id=record.session_id,
                                    detail=f"task={task_id}")
                except Exception:  # noqa: BLE001
                    pass
        return swept

    def _notify_complete(self, record: TaskRecord) -> None:
        """completed 终态（已落盘）后触发 on_complete 回调（T1）。

        在工作线程调用且不持有任何锁；回调抛任何异常都吞掉并告警，不影响任务终态
        与工作线程存活。paused/failed/canceled 等其他路径不会走到这里。
        """
        if self.on_complete is None:
            return
        try:
            self.on_complete(record)
        except Exception as exc:  # noqa: BLE001 - 回调失败不能影响任务终态
            LOG.warning("任务结果写回失败: %s | task=%s", exc, record.task_id)

    def _run(self, task_id: str) -> None:
        """处理一个队列条目：按落盘状态决定执行/跳过，终态时清理控制事件。"""
        record = self.store.get(task_id)
        if record is None:
            LOG.warning("任务 %s 不存在，跳过执行", task_id)
            return
        if record.status == STATUS_CANCELED:
            self._cleanup_events(task_id)
            return
        if record.status == STATUS_PAUSED:
            # 仍在暂停（resume 会重新入队）：不执行，保留事件供 resume 清除
            return
        if record.status != STATUS_QUEUED:
            # 终态记录的陈旧队列条目（如恢复后重复入队）：跳过
            self._cleanup_events(task_id)
            return

        pause_event = self._pause_events.get(task_id)
        cancel_event = self._cancel_events.get(task_id)
        if cancel_event is None:
            # 出队前一刻取消已收尾（cancel 会清理事件）：canceled 是终态，不执行
            return
        if pause_event is None:
            pause_event = threading.Event()  # 无暂停请求时的空事件

        # 排队期间被暂停/取消：直接落盘状态，不进入执行
        if cancel_event.is_set():
            record.status = STATUS_CANCELED
            self.store.update(record)
            self._cleanup_events(task_id)
            return
        if pause_event.is_set():
            record.status = STATUS_PAUSED
            self.store.update(record)
            return

        # 标记 running（ACC-S5-01：执行开始即对外可见）+ 心跳起点（P1-2）
        record.status = STATUS_RUNNING
        self._heartbeat(record, "规划")
        self.store.update(record)
        try:
            self._execute(record, pause_event, cancel_event)
        except Exception as exc:  # noqa: BLE001 - 规划/执行/合成异常 → failed 落盘
            LOG.exception("任务 %s 执行失败", task_id)
            record.status = STATUS_FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            self.store.update(record)
            self._cleanup_events(task_id)

    def _execute(self, record: TaskRecord, pause_event: threading.Event, cancel_event: threading.Event) -> None:
        """执行主体：规划（持锁）→ 逐步执行（持锁、步间检查点）→ 合成（持锁）。"""
        agent = self.agent_provider()
        if agent is None:
            raise RuntimeError("Agent 不可用")
        # G6：标记当前线程的会话上下文，使 tool_call/llm_call 审计事件关联会话
        audit_mod.set_current_session(record.session_id)
        # G3：本次任务的请求级联网许可（submit 透传；None → 按配置）
        allow_web_flag = (self._egress.get(record.task_id) or {}).get("allow_web")
        effective_allow_web = agent.resolve_allow_web(allow_web_flag)

        # 用量基线：任务增量 = 结束时 − 开始时（Agent 计数器跨调用累计）
        base_agent = (agent.prompt_tokens, agent.completion_tokens, agent.estimated_cost)
        base_record = dict(record.usage or {})

        def sync_usage() -> None:
            record.usage = {
                "prompt_tokens": int(base_record.get("prompt_tokens", 0)) + agent.prompt_tokens - base_agent[0],
                "completion_tokens": int(base_record.get("completion_tokens", 0)) + agent.completion_tokens - base_agent[1],
                "cost_yuan": round(
                    float(base_record.get("cost_yuan", 0.0)) + agent.estimated_cost - base_agent[2], 6
                ),
            }

        # ---- 阶段 1：重建会话记忆 + 规划（持锁，与 /api/ask 同粒度，防记忆串线） ----
        history_text = ""
        with self.lock:
            memory = self.memory_provider(record.session_id) if self.memory_provider else None
            agent.memory = memory if memory is not None else SessionMemory()
            if record.plan.get("steps"):
                # 恢复路径：沿用已持久化的计划，不重新规划；历史文本从当前会话记忆重建
                plan = plan_from_dict(record.plan)
                history_text = agent.memory.as_text()
            else:
                plan, _q_work, history_text = agent.plan_only(
                    record.question, record.session_id, allow_web=allow_web_flag
                )
                record.plan = plan_to_dict(plan)
                self.store.update(record)  # 计划先于步骤落盘，崩溃后可恢复

        # ---- 阶段 2：逐步执行（每步完成即持久化；步间检查暂停/取消） ----
        executor = Executor(agent.config, agent.tools, agent._ctx, web_fallback=effective_allow_web)
        for step_dict in record.steps:
            restored = step_from_dict(step_dict)
            executor._history[restored.step_id] = restored  # 保留 @step:N 占位符引用能力
        done_ok = {sid for sid, r in executor._history.items() if r.ok}

        paused = canceled = False
        paused_reason = ""
        t_start = time.monotonic()
        for step in plan.steps:
            if cancel_event.is_set():
                canceled = True
                break
            if pause_event.is_set():
                paused = True
                break
            # P1-2 任务级总超时：超出后停止推进剩余步骤（转为 paused 可恢复）
            if (time.monotonic() - t_start) > self.total_timeout_s:
                paused = True
                paused_reason = f"任务总超时（>{int(self.total_timeout_s)}s），已暂停可恢复"
                LOG.warning("任务 %s 触发总超时看护", record.task_id)
                break
            if step.step_id in done_ok:
                continue  # 恢复路径：跳过已 ok 步骤（失败步骤重试执行）
            with self.lock:
                # 拿到锁后再查一次，缩小「步骤进行中收到暂停/取消」的窗口
                if cancel_event.is_set():
                    canceled = True
                    break
                if pause_event.is_set():
                    paused = True
                    break
                # P1-2：外部（watchdog/cancel/pause）已把任务迁出运行口径 → 停止推进且不覆盖状态
                latest_status = (self.store.get(record.task_id) or record).status
                if latest_status not in (STATUS_RUNNING, STATUS_QUEUED):
                    paused = True
                    paused_reason = f"执行期间任务被外部迁移为 {latest_status}"
                    break
                # P1-2 心跳：步骤边界刷新，供 watchdog 判定卡死
                self._heartbeat(record, f"step {step.step_id}: {step.action}")
                result = executor._run_step(step.step_id, step, executor._history)
            executor._history[step.step_id] = result
            if not result.ok:
                record.last_error_type = str(result.error).split(":", 1)[0] or "ToolError"
            else:
                record.last_error_type = ""
            self._append_step(record, step_to_dict(result))
            sync_usage()
            self._heartbeat(record, f"step {step.step_id} done")  # 步骤完成即随落盘刷新心跳
            self.store.update(record)  # 子任务进度/中间结果/失败节点落盘

        # ---- 步骤后/合成前检查点：暂停与取消在合成阶段同样生效 ----
        if not canceled:
            if cancel_event.is_set():
                canceled = True
            elif pause_event.is_set():
                paused = True

        if canceled:
            record.status = STATUS_CANCELED
            sync_usage()
            self.store.update(record)
            self._cleanup_events(record.task_id)
            return
        if paused:
            record.status = STATUS_PAUSED
            if paused_reason:
                record.error = paused_reason
            sync_usage()
            self.store.update(record)
            return  # 保留事件：resume 会清除暂停事件

        # ---- 阶段 3：合成 + 引用校验（持锁；任务路径不做反思，步骤完成后直接合成） ----
        ordered = [executor._history[s.step_id] for s in plan.steps if s.step_id in executor._history]
        with self.lock:
            final_answer, sources, report = agent.finish_task(record.question, plan, ordered, history_text)
        record.final_answer = final_answer
        record.sources = list(sources)
        record.citations_valid = report.valid_count  # T2：随记录持久化，写回会话时展示
        record.citations_invalid = report.invalid_count
        record.status = STATUS_COMPLETED
        sync_usage()
        self.store.update(record)
        self._audit_task(record)
        self._cleanup_events(record.task_id)
        self._notify_complete(record)  # T1：completed 落盘后回调（锁外，回调方自行加锁）

    def _audit_task(self, record: TaskRecord) -> None:
        """任务 ask 终态审计事件（G6）：带 task_id 关联；正文按 audit.log_content。"""
        audit = audit_mod.get()
        if audit is None:
            return
        try:
            usage = record.usage if isinstance(record.usage, dict) else {}
            audit.log_ask(
                ok=True, session_id=record.session_id, latency_s=self._task_latency(record),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                cost_yuan=float(usage.get("cost_yuan", 0.0)),
                degraded=any(bool(s.get("degraded")) for s in record.steps),
                web_used=any(s.get("action") == "web_search" and bool(s.get("ok")) for s in record.steps),
                task_id=record.task_id,
                question=record.question if audit.log_content else None,
                answer=record.final_answer if audit.log_content else None,
            )
        except Exception:  # noqa: BLE001 - 审计失败不影响业务
            pass

    @staticmethod
    def _task_latency(record: TaskRecord) -> float:
        """任务耗时（秒）：updated_at − created_at；解析失败返回 0.0。"""
        try:
            from datetime import datetime

            fmt = "%Y-%m-%dT%H:%M:%S"
            t0 = datetime.strptime(record.created_at[:19], fmt)
            t1 = datetime.strptime(record.updated_at[:19], fmt)
            return max(0.0, (t1 - t0).total_seconds())
        except (ValueError, TypeError, AttributeError):
            return 0.0

    @staticmethod
    def _append_step(record: TaskRecord, step_dict: dict) -> None:
        """追加步骤结果；同 step_id 已存在（恢复后重试的失败步骤）时原位覆盖。"""
        sid = step_dict.get("step_id")
        for i, existing in enumerate(record.steps):
            if existing.get("step_id") == sid:
                record.steps[i] = step_dict
                return
        record.steps.append(step_dict)
