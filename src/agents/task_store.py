"""任务状态持久化存储（S5）：每任务一个 JSON 文件、原子写入、崩溃恢复。

- TaskRecord：任务数据类，to_dict/from_dict 做 JSON 往返；
- TaskStore：任务持久化目录（web 为 data_dir/tasks），每任务 {task_id}.json；
  写入用「tmp 文件 + os.replace」原子替换，threading.Lock 防并发写交错；
  list() 读全部文件按 updated_at 倒序（规模小可接受）；
  recover_running() 把遗留 queued/running 任务改为 paused（服务启动时调用，
  崩溃/中断的任务可经 resume 恢复继续）。

状态机：queued → running → completed / failed / canceled；
queued/running → paused（暂停或启动恢复）→ resume 后回到 queued 重新入队。
canceled 与 completed/failed 为终态。
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .logger import get_logger

LOG = get_logger("task_store")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

# 终态：completed/failed/canceled 不允许再迁移
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED})

# task_id 为 uuid hex[:12]，兼作文件名安全校验（拒绝路径穿越等非法 id）
_TASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class TaskRecord:
    """单个任务的持久化状态（S5）：计划、逐步结果（含失败节点）、最终答案与用量。"""

    task_id: str = ""
    session_id: str = ""
    workspace_id: str = ""
    question: str = ""
    status: str = STATUS_QUEUED
    plan: dict = field(default_factory=dict)        # 序列化后的计划（task_runner.plan_to_dict）
    steps: list = field(default_factory=list)       # 各步骤结果 dict（含 attempts/degraded，失败节点即 ok=False 条目）
    final_answer: str = ""
    error: str = ""
    sources: list = field(default_factory=list)     # 最终答案引用的来源清单
    usage: dict = field(default_factory=dict)       # {prompt_tokens, completion_tokens, cost_yuan}
    citations_valid: int = 0                        # 引用校验通过数（T2：随答案写回会话展示）
    citations_invalid: int = 0                      # 被剔除的非法引用数
    writeback_id: str = ""                          # P0-5 写回幂等标识：写回成功后置为 task_id（旧文件缺失时兼容为空）
    heartbeat_at: str = ""                          # P1-2 心跳时间：每步骤边界刷新，供 watchdog 判定卡死
    current_step: str = ""                          # P1-2 当前步骤摘要（如 "step 2: web_search"）
    last_error_type: str = ""                       # P1-2 最近一次步骤失败的错误类型（成功步骤清空）
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        """容错反序列化：字段缺失/类型不符时回退默认值（兼容旧文件与损坏数据）。"""
        data = data if isinstance(data, dict) else {}
        plan = data.get("plan")
        usage = data.get("usage")
        steps = data.get("steps")
        sources = data.get("sources")
        try:
            valid = int(data.get("citations_valid", 0) or 0)
        except (TypeError, ValueError):
            valid = 0
        try:
            invalid = int(data.get("citations_invalid", 0) or 0)
        except (TypeError, ValueError):
            invalid = 0
        return cls(
            task_id=str(data.get("task_id", "")),
            session_id=str(data.get("session_id", "")),
            workspace_id=str(data.get("workspace_id", "")),
            question=str(data.get("question", "")),
            status=str(data.get("status", STATUS_QUEUED)),
            plan=plan if isinstance(plan, dict) else {},
            steps=[s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else [],
            final_answer=str(data.get("final_answer", "")),
            error=str(data.get("error", "")),
            sources=[str(s) for s in sources] if isinstance(sources, list) else [],
            usage=usage if isinstance(usage, dict) else {},
            citations_valid=valid,
            citations_invalid=invalid,
            writeback_id=str(data.get("writeback_id", "")),
            heartbeat_at=str(data.get("heartbeat_at", "")),
            current_step=str(data.get("current_step", "")),
            last_error_type=str(data.get("last_error_type", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )

    def summary(self) -> dict:
        """列表视图：含状态/问题/用量等标量字段，不含 plan 与逐步明细。"""
        payload = self.to_dict()
        payload.pop("plan", None)
        payload.pop("steps", None)
        return payload


class TaskStore:
    """任务 JSON 存储：每任务一个文件、原子写入、启动恢复。"""

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # 写锁：防止并发写交错

    # ---- 路径与校验 ----
    @staticmethod
    def valid_id(task_id: str) -> bool:
        return bool(_TASK_ID_RE.match(task_id or ""))

    def _path(self, task_id: str) -> Path | None:
        if not self.valid_id(task_id):
            return None
        return self.root_dir / f"{task_id}.json"

    # ---- CRUD ----
    def create(self, record: TaskRecord) -> str:
        """新任务落盘并返回 task_id（为空时自动生成 12 位 hex id）。"""
        if not record.task_id:
            record.task_id = uuid.uuid4().hex[:12]
        if not self.valid_id(record.task_id):
            raise ValueError(f"非法任务 id: {record.task_id!r}")
        now = _now()
        record.created_at = record.created_at or now
        record.updated_at = record.updated_at or now
        with self._lock:
            self._write(record)
        return record.task_id

    def get(self, task_id: str) -> TaskRecord | None:
        """按 id 读取任务；不存在 / id 非法 / 文件损坏 → None。"""
        path = self._path(task_id)
        if path is None or not path.exists():
            return None
        try:
            return TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            LOG.warning("读取任务 %s 失败: %s", task_id, exc)
            return None

    def list(self) -> list[TaskRecord]:
        """全部任务，按 updated_at 倒序；损坏文件跳过并记录日志。"""
        records: list[TaskRecord] = []
        for path in self.root_dir.glob("*.json"):
            try:
                records.append(TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError) as exc:
                LOG.warning("跳过不可解析的任务文件 %s: %s", path.name, exc)
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records

    def update(self, record: TaskRecord) -> None:
        """更新落盘：刷新 updated_at，tmp + os.replace 原子替换。"""
        if not self.valid_id(record.task_id):
            raise ValueError(f"非法任务 id: {record.task_id!r}")
        record.updated_at = _now()
        with self._lock:
            self._write(record)

    def recover_running(self) -> list[str]:
        """启动恢复：把遗留 queued/running 任务改为 paused 并落盘，返回改动 id 列表。"""
        recovered: list[str] = []
        for record in self.list():
            if record.status in (STATUS_QUEUED, STATUS_RUNNING):
                record.status = STATUS_PAUSED
                self.update(record)
                recovered.append(record.task_id)
        if recovered:
            LOG.info("启动恢复：%d 个未完成任务已转为 paused", len(recovered))
        return recovered

    @staticmethod
    def _parse_iso(ts: str) -> "datetime | None":
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None

    def stale_running_ids(self, threshold_s: float) -> list[str]:
        """P1-2 watchdog：心跳超过阈值未刷新的 running 任务 id 列表。

        心跳空缺（旧记录）时回退用 updated_at 比较，保证升级存量同样被看护。
        """
        # 时间戳为 tz-aware ISO（_now 用 astimezone），cutoff 须同样带本地时区
        cutoff = datetime.now().astimezone() - timedelta(seconds=max(0.0, float(threshold_s)))
        stale: list[str] = []
        for record in self.list():
            if record.status != STATUS_RUNNING:
                continue
            ts = self._parse_iso(record.heartbeat_at) or self._parse_iso(record.updated_at)
            if ts is None or ts < cutoff:
                stale.append(record.task_id)
        return stale

    # ---- 内部 ----
    def _write(self, record: TaskRecord) -> None:
        path = self.root_dir / f"{record.task_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
