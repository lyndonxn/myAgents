"""审计轨迹（G6/P0-5 配套，spec ACC-U6-01..04）。

分层记录操作级事件，默认**不记录问题与答案正文**；正文记录需显式开启
（config.audit.log_content，默认关）。API Key 与 .env 内容永不记录。

- 存储：JSONL 按天分文件（data/audit/YYYY-MM-DD.jsonl），只追加；
- 保留：retention_days 可配，写入时惰性清理过期文件；
- 查询：query(date, type, limit) 供 GET /api/audit 使用；
- 埋点单点：工具调用 executor._run_step 终态、LLM 调用 llm.chat/chat_json、
  ask 事件 agent.ask 终态、管理操作（配置保存/记忆删除/索引重建/清空）在各写操作处。

全局单例：web_server.main()（生产入口）经 set_logger 装配；未装配时所有
埋点为 no-op，离线测试零副作用。会话上下文经 threading.local 传递
（agent.ask / task_runner 执行期 set_current_session）。
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .logger import get_logger

LOG = get_logger("audit")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_local = threading.local()

_logger: "AuditLogger | None" = None


def set_logger(logger: "AuditLogger | None") -> None:
    """装配全局审计器（生产入口调用）；传 None 卸载。"""
    global _logger
    _logger = logger


def get() -> "AuditLogger | None":
    """取当前审计器；未装配返回 None（所有埋点须容忍 None）。"""
    return _logger


def set_current_session(session_id: str) -> None:
    """标记当前线程正在服务的会话（ask/任务执行期）。"""
    _local.session_id = session_id or ""


def current_session() -> str:
    """当前线程会话 id；未标记返回 ""。"""
    return getattr(_local, "session_id", "")


class AuditLogger:
    """JSONL 按天分文件的只追加审计器。"""

    def __init__(self, audit_dir: Path, retention_days: int = 30, log_content: bool = False):
        self.audit_dir = Path(audit_dir)
        self.retention_days = max(1, int(retention_days))
        self.log_content = bool(log_content)

    # ---- 基础 ----
    def _path_for(self, date: str) -> Path:
        return self.audit_dir / f"{date}.jsonl"

    def log_event(self, event_type: str, **fields) -> None:
        """追加一条事件；字段值由调用方保证不含密钥与正文（本模块做最后兜底）。"""
        try:
            record = {
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "type": str(event_type),
                "session_id": str(fields.pop("session_id", "") or current_session()),
            }
            for key, value in fields.items():
                if isinstance(value, (dict, list)):
                    record[key] = value
                else:
                    record[key] = value
            self._cleanup_if_needed()
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now().strftime("%Y-%m-%d")
            with open(self._path_for(today), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # noqa: BLE001 - 审计失败不影响业务
            LOG.warning("审计事件写入失败: %s", exc)

    # ---- 事件类型 ----
    def log_ask(
        self, ok: bool, session_id: str = "", latency_s: float = 0.0, llm_calls: int = 0,
        prompt_tokens: int = 0, completion_tokens: int = 0, cost_yuan: float = 0.0,
        degraded: bool = False, web_used: bool = False, task_id: str = "",
        question: str | None = None, answer: str | None = None, error_type: str = "",
    ) -> None:
        """ask 终态事件；question/answer 仅在显式开启正文记录时写入（ACC-U6-02）。"""
        fields = dict(
            ok=bool(ok), latency_s=round(float(latency_s), 3), llm_calls=int(llm_calls),
            prompt_tokens=int(prompt_tokens), completion_tokens=int(completion_tokens),
            cost_yuan=round(float(cost_yuan), 6), degraded=bool(degraded), web_used=bool(web_used),
            task_id=str(task_id), error_type=str(error_type),
        )
        if self.log_content:
            fields["question"] = str(question or "")
            fields["answer"] = str(answer or "")
        self.log_event("ask", session_id=session_id, **fields)

    def log_tool_call(
        self, action: str, ok: bool, session_id: str = "", error_type: str = "",
        latency_s: float = 0.0, attempts: int = 1, degraded: bool = False,
        input_keys: list | None = None,
    ) -> None:
        """工具调用事件：参数只记键名摘要，不记值（避免间接记录提问内容）。"""
        self.log_event(
            "tool_call", session_id=session_id, action=str(action), ok=bool(ok),
            error_type=str(error_type), latency_s=round(float(latency_s), 3),
            attempts=int(attempts), degraded=bool(degraded),
            input_keys=list(input_keys or []),
        )

    def log_llm_call(
        self, ok: bool, latency_s: float = 0.0, prompt_tokens: int = 0,
        completion_tokens: int = 0, error_type: str = "", model: str = "",
    ) -> None:
        """LLM 调用事件：不含 messages 内容与 Key。"""
        self.log_event(
            "llm_call", ok=bool(ok), latency_s=round(float(latency_s), 3),
            prompt_tokens=int(prompt_tokens), completion_tokens=int(completion_tokens),
            error_type=str(error_type), model=str(model),
        )

    def log_admin(self, action: str, ok: bool, session_id: str = "", detail: str = "") -> None:
        """管理操作事件（配置保存/记忆删除/索引重建/清空等）；detail 只含动作摘要。"""
        self.log_event("admin", session_id=session_id, action=str(action), ok=bool(ok), detail=str(detail))

    # ---- 查询（GET /api/audit） ----
    def query(self, date: str = "", event_type: str = "", limit: int = 200) -> list[dict]:
        """读取审计事件：date 缺省取当天；type 过滤；limit 截断（新→旧=文件内倒序）。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        path = self._path_for(date)
        if not path.exists():
            return []
        events: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event_type and event.get("type") != event_type:
                        continue
                    events.append(event)
        except OSError as exc:
            LOG.warning("审计文件读取失败: %s", exc)
            return []
        events.reverse()  # 新→旧
        return events[: max(0, int(limit))]

    # ---- 保留策略 ----
    _last_cleanup_date: str = ""

    def _cleanup_if_needed(self) -> None:
        """惰性清理：每天首次写入时删除超过保留期的审计文件。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if AuditLogger._last_cleanup_date == today:
            return
        AuditLogger._last_cleanup_date = today
        try:
            if not self.audit_dir.exists():
                return
            cutoff = (datetime.now() - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
            for f in self.audit_dir.glob("*.jsonl"):
                if _DATE_RE.match(f.stem) and f.stem < cutoff:
                    f.unlink(missing_ok=True)
        except OSError as exc:
            LOG.warning("审计过期文件清理失败: %s", exc)
