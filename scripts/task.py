"""CLI 后台任务入口（T3）：创建/列出/观察/恢复/取消后台长任务。

用法：
  python -m scripts.task --question "RAG 的完整流程是什么？"    # 创建任务并跟踪到终态
  python -m scripts.task --list                               # 列出历史任务
  python -m scripts.task --watch <task_id>                    # 观察既有任务直到终态
  python -m scripts.task --resume <task_id>                   # 恢复已暂停（paused）的任务
  python -m scripts.task --cancel <task_id>                   # 取消任务
  python -m scripts.task --list --task-store /tmp/tasks       # 覆盖存储目录（默认 data_dir/tasks）

与 Web 前端共享同一任务存储（data_dir/tasks），直连本地模块执行、不走 HTTP：
- --question 装配 load_config + LLMClient（无 Key 打印中文错误并退出 1）+ Agent +
  TaskRunner（工作线程为 daemon，构造即启动），submit 后每 0.5s 轮询 store.get(task_id)：
  状态变化打印一行（[时间] 状态）、新增步骤打印 ✓/× 行；到达终态打印最终答案全文 +
  参考来源列表 + token/成本（usage）。
- Ctrl+C 中断轮询：不强制询问，直接提示「任务转入后台，可稍后 --watch 查看」并退出 0；
  进程退出后被中断的任务由下次服务启动的 recover_running 恢复为 paused，可 --resume 继续。
- --resume 仅对 paused 任务生效（与 TaskRunner.resume 的恢复语义一致，不绕过其公开 API）。
- --list / --watch 只读存储；--cancel 经 runner.cancel 落盘终态，均无需加载真实索引。

五个动作参数互斥且必须恰好提供一个（否则打印用法并退出 1）。可测试逻辑拆分为
run_* 系列函数（返回进程退出码），main() 只做 argparse 分发。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.agent import Agent  # noqa: E402
from agents.config import load_config  # noqa: E402
from agents.llm import LLMClient, LLMError  # noqa: E402
from agents.memory import SessionMemory  # noqa: E402
from agents.task_runner import TaskRunner  # noqa: E402
from agents.task_store import (  # noqa: E402
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    TERMINAL_STATUSES,
    TaskStore,
)

POLL_INTERVAL_S = 0.5  # 默认轮询间隔（秒）

# 任务状态中文标签（列表与轮询展示用）
STATUS_LABELS = {
    "queued": "排队中",
    "running": "执行中",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}


# ---------------- 小工具 ----------------

def _now_hms() -> str:
    """当前时刻（轮询状态行的 [时间] 部分）。"""
    return datetime.now().strftime("%H:%M:%S")


def _shorten(text: str, limit: int) -> str:
    """单行摘要：换行折空格，超长截断加省略号。"""
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _default_store_dir() -> Path:
    """默认任务存储目录：data_dir/tasks（与 Web 前端一致）。"""
    return Path(load_config().data_dir) / "tasks"


def _open_store(store_dir=None) -> TaskStore:
    """打开任务存储；store_dir 为空时回落到默认目录。"""
    return TaskStore(Path(store_dir)) if store_dir else TaskStore(_default_store_dir())


# ---------------- 装配 ----------------

def _build_agent(config=None) -> Agent:
    """装配真实 Agent：load_config + LLMClient（无 Key 报错退出 1）+ Agent + 加载索引。"""
    config = config or load_config()
    try:
        llm = LLMClient(config)
    except LLMError as exc:
        print(f"错误：{exc}")
        raise SystemExit(1)
    agent = Agent(config, llm=llm)
    agent.load_index()
    return agent


def _build_runner(store: TaskStore, agent_provider) -> TaskRunner:
    """装配 TaskRunner：agent_provider 可为 Agent 实例或返回 Agent 的 callable。

    CLI 无 /api/ask 并发，lock 用独立互斥锁；会话记忆用全新空会话（任务路径
    plan_only/finish_task 只读不写记忆，与 Web 端 memory_provider 语义对齐）。
    TaskRunner 构造时即启动 daemon 工作线程，--watch/--cancel 等只读动作同样安全。
    """
    provider = agent_provider if callable(agent_provider) else (lambda: agent_provider)
    return TaskRunner(
        store,
        threading.Lock(),
        agent_provider=provider,
        memory_provider=lambda session_id: SessionMemory(),
    )


# ---------------- 展示 ----------------

def _step_line(step: dict) -> str:
    """步骤行：✓/× + step_id + action + 输入摘要（失败步附错误摘要）。"""
    mark = "✓" if step.get("ok") else "×"
    try:
        input_text = json.dumps(step.get("input") or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        input_text = str(step.get("input"))
    line = f"  {mark} step{step.get('step_id', '?')} {step.get('action', '?')} {_shorten(input_text, 48)}"
    if not step.get("ok") and step.get("error"):
        line += f" — {_shorten(str(step['error']), 80)}"
    return line


def _usage_line(usage: dict) -> str:
    """token/成本行（字段缺失或类型异常时按 0 处理，不崩溃）。"""
    def _num(key: str) -> int:
        try:
            return int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    try:
        cost = float(usage.get("cost_yuan", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return f"tokens in={_num('prompt_tokens')} out={_num('completion_tokens')} | 估算成本 ¥{cost:.4f}"


def render_final(record) -> str:
    """终态记录的结果块：最终答案全文 + 参考来源列表 + token/成本（usage）。"""
    lines = ["=" * 60, f"任务 {record.task_id} · {_status_label(record.status)}", "-" * 60]
    if record.status == STATUS_COMPLETED:
        lines += ["最终答案：", record.final_answer or "(无答案)"]
        if record.sources:
            lines.append("-" * 60)
            lines.append("参考来源：")
            for i, src in enumerate(record.sources, 1):
                lines.append(f"  [{i}] {src}")
        lines.append("-" * 60)
        lines.append(_usage_line(record.usage or {}))
        if record.citations_valid or record.citations_invalid:
            lines.append(f"引用校验：有效 {record.citations_valid} / 剔除 {record.citations_invalid}")
    elif record.status == STATUS_FAILED:
        lines.append(f"任务失败：{record.error or '(未知错误)'}")
    elif record.status == STATUS_CANCELED:
        lines.append("任务已取消（未生成最终答案）")
    else:
        lines.append(f"任务处于「{_status_label(record.status)}」状态")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------- 轮询 ----------------

def _poll_until_terminal(
    store: TaskStore,
    task_id: str,
    poll_interval: float = POLL_INTERVAL_S,
    timeout: float | None = None,
    interrupt_message: str = "",
) -> int:
    """轮询任务直到终态：状态变化打印一行、新增步骤打印 ✓/× 行、终态打印结果块。

    timeout 仅作兜底（超时返回 1 并提示稍后继续，不打断执行中的任务）；
    Ctrl+C 打印 interrupt_message（--question/--resume 为「转入后台」提示）并返回 0。
    """
    last_status = None
    seen_steps = 0
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            record = store.get(task_id)
            if record is None:
                print(f"任务不存在: {task_id}")
                return 1
            if record.status != last_status:
                print(f"[{_now_hms()}] {_status_label(record.status)}")
                last_status = record.status
            for step in record.steps[seen_steps:]:
                print(_step_line(step))
            seen_steps = len(record.steps)
            if record.status in TERMINAL_STATUSES:
                print(render_final(record))
                return 0
            if deadline is not None and time.monotonic() >= deadline:
                print(f"等待超时（{timeout:.0f}s）：任务尚未到达终态，可稍后 --watch {task_id} 继续")
                return 1
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print()
        print(interrupt_message or f"已停止观察；可稍后 --watch {task_id} 查看")
        return 0


# ---------------- 动作（可测试入口，返回退出码） ----------------

def run_create(
    question: str,
    store_dir=None,
    agent=None,
    runner: TaskRunner | None = None,
    config=None,
    poll_interval: float = POLL_INTERVAL_S,
    timeout: float | None = None,
) -> int:
    """--question：创建任务并跟踪执行直到终态。

    agent/runner 供测试注入打桩；缺省时装配真实 Agent（无 Key 报错退出 1）与
    TaskRunner（store_dir 为空时用 data_dir/tasks）。
    """
    if runner is None:
        if agent is None:
            agent = _build_agent(config)
        store = _open_store(store_dir)
        runner = _build_runner(store, agent)
    try:
        task_id = runner.submit(session_id="cli", workspace_id="default", question=question)
    except Exception as exc:  # noqa: BLE001 - 落盘失败等创建期错误
        print(f"错误：任务创建失败: {exc}")
        return 1
    print(f"任务已创建: {task_id}（问题：{_shorten(question, 40)}）")
    print("提示：Ctrl+C 可转入后台，稍后 --watch 查看")
    return _poll_until_terminal(
        runner.store,
        task_id,
        poll_interval=poll_interval,
        timeout=timeout,
        interrupt_message=f"[{task_id}] 任务转入后台，可稍后 --watch 查看",
    )


def run_list(store_dir=None) -> int:
    """--list：列出存储中的全部任务（id/状态/时间/问题摘要 24 字，按更新时间倒序）。"""
    records = _open_store(store_dir).list()
    if not records:
        print("暂无任务")
        return 0
    print(f"共 {len(records)} 个任务（按更新时间倒序）：")
    for record in records:
        ts = (record.updated_at or record.created_at or "")[:19].replace("T", " ")
        print(f"  {record.task_id}  {_status_label(record.status)}  {ts}  {_shorten(record.question, 24)}")
    return 0


def run_watch(task_id: str, store_dir=None, timeout: float | None = None, poll_interval: float = POLL_INTERVAL_S) -> int:
    """--watch：观察既有任务直到终态（不创建新任务）。"""
    store = _open_store(store_dir)
    record = store.get(task_id)
    if record is None:
        print(f"任务不存在: {task_id}")
        return 1
    hint = f"（任务已暂停，可 --resume {task_id} 恢复）" if record.status == STATUS_PAUSED else ""
    print(f"观察任务 {task_id}：{_shorten(record.question, 40)}{hint}")
    return _poll_until_terminal(store, task_id, poll_interval=poll_interval, timeout=timeout)


def run_resume(
    task_id: str,
    store_dir=None,
    runner: TaskRunner | None = None,
    poll_interval: float = POLL_INTERVAL_S,
    timeout: float | None = None,
) -> int:
    """--resume：校验存在且 status==paused 后经 runner.resume 恢复，随后进入轮询展示。"""
    store = _open_store(store_dir)
    record = store.get(task_id)
    if record is None:
        print(f"任务不存在: {task_id}")
        return 1
    if record.status != STATUS_PAUSED:
        print(f"任务 {task_id} 当前状态为「{_status_label(record.status)}」，仅已暂停（paused）的任务可以恢复")
        return 1
    if runner is None:
        runner = _build_runner(store, _build_agent())
    runner.resume(task_id)
    print(f"任务 {task_id} 已恢复执行")
    return _poll_until_terminal(
        store,
        task_id,
        poll_interval=poll_interval,
        timeout=timeout,
        interrupt_message=f"[{task_id}] 任务转入后台，可稍后 --watch 查看",
    )


def run_cancel(task_id: str, store_dir=None, runner: TaskRunner | None = None) -> int:
    """--cancel：存在则经 runner.cancel 取消并打印终态；已是终态则提示无需取消。"""
    store = _open_store(store_dir)
    record = store.get(task_id)
    if record is None:
        print(f"任务不存在: {task_id}")
        return 1
    if record.status in TERMINAL_STATUSES:
        print(f"任务 {task_id} 已是终态（{_status_label(record.status)}），无需取消")
        print(render_final(record))
        return 0
    if runner is None:
        runner = _build_runner(store, lambda: None)  # 取消不执行步骤，无需真实 Agent
    runner.cancel(task_id)
    print(f"[{_now_hms()}] 已取消")
    print(render_final(store.get(task_id) or record))
    return 0


# ---------------- CLI 分发 ----------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="myAgents 后台任务 CLI：创建/列出/观察/恢复/取消长任务（直连本地模块）"
    )
    parser.add_argument("--question", "-q", metavar="TEXT", help="创建一个后台任务并跟踪执行到终态")
    parser.add_argument("--list", action="store_true", help="列出历史任务")
    parser.add_argument("--watch", metavar="ID", help="观察既有任务直到终态")
    parser.add_argument("--resume", metavar="ID", help="恢复已暂停（paused）的任务并跟踪到终态")
    parser.add_argument("--cancel", metavar="ID", help="取消任务并打印终态")
    parser.add_argument("--task-store", metavar="PATH", help="任务存储目录（默认 data_dir/tasks）")
    args = parser.parse_args()

    chosen = [
        name for name, value in (
            ("--question", args.question),
            ("--list", args.list),
            ("--watch", args.watch),
            ("--resume", args.resume),
            ("--cancel", args.cancel),
        ) if value
    ]
    if not chosen:
        parser.print_usage()
        print("错误：必须提供 --question/--list/--watch/--resume/--cancel 之一")
        raise SystemExit(1)
    if len(chosen) > 1:
        print(f"错误：{'、'.join(chosen)} 互斥，请只提供一个")
        raise SystemExit(1)

    store_dir = args.task_store
    if args.question:
        code = run_create(args.question, store_dir=store_dir)
    elif args.list:
        code = run_list(store_dir)
    elif args.watch:
        code = run_watch(args.watch, store_dir=store_dir)
    elif args.resume:
        code = run_resume(args.resume, store_dir=store_dir)
    else:
        code = run_cancel(args.cancel, store_dir=store_dir)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
