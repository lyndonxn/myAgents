"""Web 服务核心（模块化封装，含安全加固与操作日志）。

安全机制：
- 仅绑定 127.0.0.1；Host 头白名单校验（防 DNS rebinding）
- /vendor/ 静态文件路径遍历防护（resolve + 前缀校验）
- 请求体大小上限（10MB，防内存耗尽）
- 全局异常兜底：统一 500 JSON，不泄漏堆栈
- API Key 永不回传明文（仅脱敏）

日志：data/logs/myagents.log（RotatingFileHandler，1MB×5 轮转）
"""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from agents import audit as audit_mod
from agents import config as config_mod
from agents.agent import Agent
from agents.audit import AuditLogger
from agents.config import (
    PROJECT_ROOT,
    key_file_permissions_ok,
    load_config,
    normalize_config,
    save_runtime,
    validate_config,
)
from agents.llm import LLMClient, LLMError
from agents.logger import get_logger, setup_logging
from agents.task_runner import TaskRunner
from agents.task_store import TaskStore
from agents.web_store import WebStore

# W6-S8：legacy 单文件页已退役为备份副本（webui.html 移除，git 历史/标签 webui-html-final 可回溯）
PAGE = PROJECT_ROOT / "scripts" / "webui.legacy.html"
# W6：Next.js 静态导出目录（frontend/out）。存在则优先伺服；缺失回退 legacy 单文件页
EXPORT_DIR = PROJECT_ROOT / "frontend" / "out"
VENDOR_DIR = PROJECT_ROOT / "scripts" / "vendor"
LOG = get_logger("web")

# 可从前端编辑的配置项白名单（深层覆盖）
CONFIG_FIELDS = {
    "llm": {"base_url", "chat_model", "temperature", "max_tokens", "mode"},
    "retrieval": {"top_k", "rerank", "rerank_candidates", "multi_query", "reranker_model"},
    "vision": {"base_url", "model", "api_key"},
    "audit": {"retention_days", "log_content"},
    "tasks": {"step_timeout_s", "total_timeout_s", "watchdog_interval_s"},
    "synthesis": {"max_tokens", "evidence_compression"},
    # G3：外发/记忆三开关进设置面板（保存路径不再丢弃，前端可读回显）
    "tools": {"kb_fallback_web"},
    "memory": {"long_term_enabled", "entities_enabled"},
}
MAX_BODY = 10 * 1024 * 1024        # 请求体上限 10MB
MAX_IMAGE_DATA_URL = 6 * 1024 * 1024  # 图片 data URL 上限 6MB
MAX_QUESTION_LEN = 8000            # 问题文本上限（防超大 prompt 打爆 LLM 额度/卡服务）
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
_AUDIT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # /api/audit date 参数格式


def build_agent():
    config = load_config()
    try:
        llm = LLMClient(config)
    except Exception:  # noqa: BLE001 - 无 Key 也能先加载索引，问答时再提示
        llm = None
    agent = Agent(config, llm=llm)
    agent.load_index()
    return agent


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


def parse_opt_bool(value) -> tuple[bool | None, str | None]:
    """解析可选布尔请求字段（G3：allow_web / remember）。

    缺省/None → (None, None) 表示"用配置默认"；bool 原样接受；字符串
    true/false/1/0/yes/no（大小写不敏感）归一化为真布尔；其他类型/取值返回
    错误消息（调用方回 400），避免静默吞掉非法输入改变外发语义。
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return value, None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True, None
        if lowered in ("false", "0", "no"):
            return False, None
    return None, "须为布尔值（true/false）"


def _egress_flags(steps: list) -> dict:
    """外发标记（G3/ACC-U3-03）：degraded=任一步 KB→Web 降级；web_used=存在 ok 的 web_search 步骤。

    steps 元素可为 StepResult（内存问答路径）或持久化步骤 dict（任务记录路径），
    供 _answer_payload、任务写回与任务详情共用同一口径。
    """
    degraded = False
    web_used = False
    for step in steps or []:
        if isinstance(step, dict):
            degraded = degraded or bool(step.get("degraded"))
            web_used = web_used or (step.get("action") == "web_search" and bool(step.get("ok")))
        else:
            degraded = degraded or bool(getattr(step, "degraded", False))
            web_used = web_used or (
                getattr(step, "action", "") == "web_search" and bool(getattr(step, "ok", False))
            )
    return {"degraded": degraded, "web_used": web_used}


def _task_latency_s(record) -> float | None:
    """后台任务耗时（秒）：updated_at − created_at（秒级精度，含排队/暂停时长）。

    任务路径没有单独的延迟计时，用落盘时间戳估算；解析失败返回 None（metrics 省略该键）。
    """
    try:
        created = datetime.fromisoformat(record.created_at)
        updated = datetime.fromisoformat(record.updated_at)
    except (TypeError, ValueError, AttributeError):
        return None
    return max(0.0, round((updated - created).total_seconds(), 1))


class Handler(BaseHTTPRequestHandler):
    agent = None  # type: ignore[assignment]
    store = None  # type: ignore[assignment]
    task_runner = None  # type: ignore[assignment]  # S5：后台任务运行器（main 装配）
    lock = threading.Lock()
    building = False
    build_error = ""
    server_ref = None
    last_heartbeat = 0.0
    heartbeat_seen = False

    # ---- 基础 ----
    def log_message(self, fmt, *args):  # 静默底层访问日志（用业务日志替代）
        pass

    def _check_host(self) -> bool:
        """Host 白名单校验：仅接受本机访问（防 DNS rebinding / 伪造 Host）。"""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ALLOWED_HOSTS

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            return None
        if length > MAX_BODY:
            LOG.warning("拒绝超大请求体: %d bytes", length)
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def _answer_payload(self, answer) -> dict:
        return {
            "answer": answer.final_answer or "",
            "error": answer.error or "",
            "sources": answer.sources,
            # W1：结构化来源详情（与 sources 下标一一对应；旧 answer 对象缺失时回退空表）
            "sources_detail": list(getattr(answer, "sources_detail", []) or []),
            "plan": {
                "summary": answer.plan.plan_summary,
                "steps": [
                    {"action": s.action, "input": s.input, "ok": s.ok, "error": s.error}
                    for s in answer.steps
                ],
                "fallback": answer.plan.fallback,
                "rounds": answer.plan.rounds,
                "reflections": len(answer.plan.reflections),
            },
            "metrics": {
                "latency_s": round(answer.total_latency_s, 1),
                "llm_calls": answer.llm_calls,
                "prompt_tokens": answer.prompt_tokens,
                "completion_tokens": answer.completion_tokens,
                "cost_yuan": round(answer.estimated_cost, 4),
                # S3 引用校验计数（T1 透出）：旧 answer 对象缺失时按 0 处理
                "citations_valid": getattr(answer, "citations_valid", 0),
                "citations_invalid": getattr(answer, "citations_invalid", 0),
                # G3 状态披露（ACC-U3-01）：degraded=任一步 KB→Web 降级；web_used=发生了 web_search 外发
                **_egress_flags(answer.steps),
            },
        }

    def _active_workspace(self) -> dict:
        workspace = self.store.active_workspace()
        if not workspace:
            raise RuntimeError("没有可用工作区")
        return workspace

    def _persist_answer(self, session_id: str, workspace_id: str, question: str, answer) -> dict:
        payload = self._answer_payload(answer)
        self.store.add_message(session_id, "user", question)
        message_id = self.store.add_message(
            session_id, "assistant", payload["answer"], payload["sources"], payload["plan"], payload["metrics"],
            sources_detail=payload.get("sources_detail", []),  # W1：证据卡详情跨重启复用
        )
        # 持久化会话记忆滚动摘要（S4）：ask 内 maybe_compress 已更新，落库跨重启复用
        summary = getattr(self.agent.memory, "summary", "")
        if summary:
            self.store.set_summary(session_id, summary)
        kb_hit = bool(payload["sources"]) and any(not str(source).startswith("http") for source in payload["sources"])
        self.store.record_query(workspace_id, session_id, kb_hit)
        payload["message_id"] = message_id
        return payload

    @classmethod
    def persist_task_result(cls, record) -> None:
        """后台任务结果写回会话消息（T1）：TaskRunner 的 on_complete 回调。

        仅 completed 任务写回（paused/failed/canceled 不写）：question 作为 user 消息、
        final_answer 作为 assistant 消息落 WebStore——sources/plan 复用 record，
        metrics 附 task_id、耗时与用量（口径对齐 _persist_answer，record 没有的键省略），
        并按相同 kb_hit 口径记录查询事件。回调在 TaskRunner 工作线程执行且不持锁，
        与 /api/ask 的互斥由本方法自行持有 Handler.lock 保证。

        P0-5 写回幂等：writeback_id 已置位（重复回调）或 WebStore 已存在同 task_id 的
        assistant 消息（写回后崩溃重放 / 旧记录升级）→ 跳过并补齐标识，同一任务的消息
        只出现一次。写回成功才置 writeback_id 并持久化；失败保持 completed 与空
        writeback_id（可重试），不回滚任务答案。
        """
        if getattr(record, "status", "") != "completed":
            return
        if getattr(record, "writeback_id", ""):
            LOG.info("任务结果已写回过，跳过重复回调 | task=%s", record.task_id)
            return
        store = cls.store
        if store is None:
            LOG.warning("任务结果写回失败: WebStore 未就绪 | task=%s", record.task_id)
            return
        # 幂等双保险：库中已有该任务的写回消息（如写回后未置标识即崩溃）→ 只补标识
        if store.has_task_writeback(record.session_id, record.task_id):
            cls._mark_writeback(record)
            LOG.info("检测到任务结果已在会话中，补齐写回标识 | task=%s", record.task_id)
            return
        usage = record.usage if isinstance(record.usage, dict) else {}
        metrics: dict = {}
        latency = _task_latency_s(record)
        if latency is not None:
            metrics["latency_s"] = latency
        metrics["task_id"] = record.task_id
        metrics["prompt_tokens"] = int(usage.get("prompt_tokens", 0))
        metrics["completion_tokens"] = int(usage.get("completion_tokens", 0))
        metrics["citations_valid"] = int(getattr(record, "citations_valid", 0) or 0)
        metrics["citations_invalid"] = int(getattr(record, "citations_invalid", 0) or 0)
        metrics["cost_yuan"] = round(float(usage.get("cost_yuan", 0.0)), 4)
        # G3 状态披露：任务写回消息同样带外发标记（与 /api/ask 的 metrics 口径一致）
        metrics.update(_egress_flags(record.steps))
        with cls.lock:
            store.add_task_result(
                record.session_id, record.workspace_id, record.question, record.final_answer,
                record.sources, record.plan, metrics,
            )
        cls._mark_writeback(record)
        LOG.info("任务结果已写回会话: %s | session=%s", record.task_id, record.session_id)

    @classmethod
    def _mark_writeback(cls, record) -> None:
        """写回成功后置幂等标识并持久化到任务库（task_runner 未装配时仅内存置位）。"""
        record.writeback_id = record.task_id
        task_store = cls.task_runner.store if cls.task_runner is not None else None
        if task_store is not None:
            task_store.update(record)

    def _audit_admin(self, action: str, ok: bool, session_id: str = "", detail: str = "") -> None:
        """管理操作审计（G6）：配置保存/记忆删除/索引重建/清空等；detail 只含动作摘要。"""
        audit = audit_mod.get()
        if audit is None:
            return
        try:
            audit.log_admin(action, ok=ok, session_id=session_id, detail=detail)
        except Exception:  # noqa: BLE001 - 审计失败不影响业务
            pass

    def _session_for_workspace(self, session_id: str, workspace_id: str) -> str:
        if not session_id:
            return self.store.create_session(workspace_id)
        if not self.store.session_belongs_to(session_id, workspace_id):
            raise ValueError("会话不属于当前工作区")
        return session_id

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        try:
            self._do_get()
        except Exception as exc:  # noqa: BLE001 - 兜底：不泄漏堆栈
            LOG.exception("GET %s 处理异常", self.path)
            try:
                self._send_json({"error": "服务器内部错误"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _do_get(self):
        if not self._check_host():
            self._send_json({"error": "非法访问"}, 403)
            return
        # 静态资源：/vendor/* → scripts/vendor/*（路径遍历防护）
        if self.path.startswith("/vendor/"):
            rel = self.path[len("/vendor/"):]
            base = VENDOR_DIR.resolve()
            f = (base / rel).resolve()
            if base in f.parents and f.is_file():
                body = f.read_bytes()
                ctype = "application/javascript; charset=utf-8" if f.suffix == ".js" else "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_json({"error": "not found"}, 404)
            return
        if self.path == "/api/status":
            agent = self.agent
            self._send_json(
                {
                    "api_version": 4,
                    "parents": len(agent.chunks),
                    "leaves": len(agent.leaves),
                    "backend": agent.vector_store.backend_name,
                    "dim": agent.vector_store.dim,
                    "reranker": agent.reranker.model_name if agent.reranker else None,
                    "memory_turns": agent.memory.count,
                    "kb_path": agent.config.kb_path,
                    "building": self.building,
                    "vision": agent.config.vision_configured,
                    "llm_configured": bool(agent.config.llm_api_key),
                    "context": {
                        "prompt_tokens": agent.prompt_tokens,
                        "completion_tokens": agent.completion_tokens,
                        "total_tokens": agent.prompt_tokens + agent.completion_tokens,
                        "max_context": 128000,
                    },
                }
            )
            return
        if self.path == "/api/workspaces":
            self._send_json({"workspaces": self.store.workspaces(), "active": self._active_workspace()["id"]})
            return
        if self.path.startswith("/api/sessions"):
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 3 and parts[2]:
                workspace_id = self._active_workspace()["id"]
                if not self.store.session_belongs_to(parts[2], workspace_id):
                    self._send_json({"error": "会话不存在或不属于当前工作区"}, 404)
                    return
                self._send_json({"messages": self.store.messages(parts[2])})
            else:
                workspace_id = parse_qs(parsed.query).get("workspace_id", [self._active_workspace()["id"]])[0]
                self._send_json({"sessions": self.store.sessions(workspace_id)})
            return
        if self.path == "/api/stats":
            self._send_json(self.store.stats(self._active_workspace()["id"]))
            return
        # G4/P0-2 记忆治理：查看长期记忆条目（GET /api/memory?session_id=&q=&limit=）
        # 与实体事实（GET /api/memory/entities）
        if self.path.startswith("/api/memory"):
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 3 and parts[2] == "entities":
                em = getattr(self.agent, "entity_memory", None)
                self._send_json({"entities": dict(em.entities) if em is not None else {}})
                return
            if parsed.path == "/api/memory":
                params = parse_qs(parsed.query)
                lm = getattr(self.agent, "long_memory", None)
                if lm is None:
                    self._send_json({"episodes": [], "total": 0, "disabled": True})
                else:
                    self._send_json(lm.list_episodes(
                        session_id=params.get("session_id", [""])[0],
                        q=params.get("q", [""])[0],
                        limit=params.get("limit", ["50"])[0],
                    ))
                return
            self._send_json({"error": "not found"}, 404)
            return
        # S5 任务列表：摘要视图（不含 plan/steps 明细），按 updated_at 倒序
        if self.path == "/api/tasks":
            records = self.task_runner.store.list() if self.task_runner else []
            self._send_json({"tasks": [r.summary() for r in records]})
            return
        # S5 任务详情：完整 record JSON（G3：附 degraded/web_used 外发标记 metrics）
        if self.path.startswith("/api/tasks/"):
            task_id = self.path[len("/api/tasks/"):].strip("/")
            record = self.task_runner.store.get(task_id) if self.task_runner else None
            if record is None:
                self._send_json({"error": "任务不存在"}, 404)
            else:
                detail = record.to_dict()
                detail["metrics"] = _egress_flags(record.steps)
                self._send_json(detail)
            return
        if self.path == "/api/config":
            self._send_json(self._config_view())
            return
        if self.path == "/api/kb":
            cfg = self.agent.config
            kb = Path(cfg.kb_path)
            self._send_json(
                {
                    "path": cfg.kb_path,
                    "exists": kb.exists(),
                    "md_count": len(list(kb.rglob("*.md"))) if kb.exists() else 0,
                    "building": self.building,
                    "build_error": self.build_error,
                }
            )
            return
        # G6：审计事件查询（GET /api/audit?date=&type=&limit=，非法参数 400）
        if self.path.startswith("/api/audit"):
            parsed = urlsplit(self.path)
            if parsed.path != "/api/audit":
                self._send_json({"error": "not found"}, 404)
                return
            params = parse_qs(parsed.query)
            date = params.get("date", [""])[0].strip()
            event_type = params.get("type", [""])[0].strip()
            try:
                limit = int(params.get("limit", ["200"])[0])
            except ValueError:
                self._send_json({"error": "limit 须为整数"}, 400)
                return
            if date and not _AUDIT_DATE_RE.match(date):
                self._send_json({"error": "date 须为 YYYY-MM-DD 格式"}, 400)
                return
            if not 1 <= limit <= 1000:
                self._send_json({"error": "limit 须在 1-1000 之间"}, 400)
                return
            audit = audit_mod.get()
            events = audit.query(date=date, event_type=event_type, limit=limit) if audit is not None else []
            self._send_json({
                "events": events,
                "date": date or datetime.now().strftime("%Y-%m-%d"),
                "total": len(events),
            })
            return
        if self.path.startswith("/api/logs"):
            self._send_json(self._logs_view())
            return
        # 聊天页面：优先 Next 静态导出（W6，frontend/out），缺失回退 legacy 单文件页
        static_asset = self._static_export(self.path)
        if static_asset is not None:
            body, mime = static_asset
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", "/index.html") and PAGE.exists():
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json({"error": "webui.html 缺失"}, 500)

    def _static_export(self, path: str):
        """W6：伺服 Next 静态导出（frontend/out）。

        返回 (body, content_type) 或 None（导出目录缺失/文件不存在/路径穿越）。
        路径解析后强制约束在导出目录内，拒绝 `..` 等穿越；文本类型补 charset。
        """
        export_dir = EXPORT_DIR
        if not export_dir.is_dir():
            return None
        clean = path.split("?", 1)[0]
        rel = "index.html" if clean in ("/", "/index.html") else clean.lstrip("/")
        if not rel:
            return None
        candidate = (export_dir / rel).resolve()
        try:
            candidate.relative_to(export_dir.resolve())
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "application/json"):
            mime = f"{mime}; charset=utf-8"
        return candidate.read_bytes(), mime

    def _logs_view(self) -> dict:
        """读取日志文件尾部（默认最近 300 行），供设置面板展示。"""
        from urllib.parse import parse_qs, urlparse

        try:
            lines = int(parse_qs(urlparse(self.path).query).get("lines", ["300"])[0])
            lines = max(20, min(lines, 2000))
        except (ValueError, KeyError):
            lines = 300
        log_file = self.agent.config.data_dir / "logs" / "myagents.log"
        if not log_file.exists():
            return {"logs": [], "path": str(log_file), "error": "日志文件尚未生成"}
        try:
            # 从尾部读取最后 N 行（大文件高效）
            with open(log_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = b""
                pos = max(0, size - 200_000)
                f.seek(pos)
                tail = f.read().decode("utf-8", errors="replace")
            all_lines = tail.splitlines()
            logs = all_lines[-lines:]
            return {"logs": logs, "path": str(log_file), "total": len(all_lines), "truncated": size > 200_000}
        except OSError as exc:
            return {"logs": [], "path": str(log_file), "error": str(exc)}

    # ---- DELETE ----
    def do_DELETE(self):  # noqa: N802
        try:
            self._do_delete()
        except Exception as exc:  # noqa: BLE001 - 兜底：不泄漏堆栈
            LOG.exception("DELETE %s 处理异常", self.path)
            try:
                self._send_json({"error": "服务器内部错误"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _do_delete(self):
        if not self._check_host():
            self._send_json({"error": "非法访问"}, 403)
            return
        parsed = urlsplit(self.path)
        # G4/P0-2：删除指定会话产生的全部长期记忆（DELETE /api/memory?session_id=）
        if parsed.path == "/api/memory":
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            lm = getattr(self.agent, "long_memory", None)
            removed = lm.delete_by_session(session_id) if lm is not None else 0
            LOG.info("删除会话长期记忆 | session=%s | removed=%d", session_id, removed)
            self._audit_admin("memory_delete_session", True, session_id=session_id, detail=f"removed={removed}")
            self._send_json({"ok": True, "removed": removed})
            return
        # G4/P0-2：删除单条长期记忆（DELETE /api/memory/{episode_id}）
        if parsed.path.startswith("/api/memory/"):
            episode_id = parsed.path[len("/api/memory/"):].strip("/")
            lm = getattr(self.agent, "long_memory", None)
            deleted = lm.delete_episode(episode_id) if lm is not None else False
            if deleted:
                LOG.info("删除长期记忆条目 | episode=%s", episode_id)
                self._audit_admin("memory_delete", True, detail=f"episode={episode_id}")
                self._send_json({"ok": True})
            else:
                self._audit_admin("memory_delete", False, detail=f"episode={episode_id}")
                self._send_json({"error": "记忆条目不存在"}, 404)
            return
        self._send_json({"error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        try:
            self._do_post()
        except Exception as exc:  # noqa: BLE001 - 兜底：不泄漏堆栈
            LOG.exception("POST %s 处理异常", self.path)
            try:
                self._send_json({"error": "服务器内部错误"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _do_post(self):
        if not self._check_host():
            self._send_json({"error": "非法访问"}, 403)
            return
        payload = self._read_payload()

        if self.path == "/api/heartbeat":
            type(self).last_heartbeat = time.monotonic()
            type(self).heartbeat_seen = True
            self._send_json({"ok": True})
            return

        if self.path == "/api/shutdown":
            self._send_json({"ok": True, "message": "服务正在关闭"})
            server = type(self).server_ref
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()
            return

        if payload is not None:
            question = str(payload.get("question", "")).strip()
            if len(question) > MAX_QUESTION_LEN:
                LOG.warning("拒绝超长问题: %d 字符", len(question))
                self._send_json({"error": f"问题过长（上限 {MAX_QUESTION_LEN} 字符）"}, 400)
                return

        if self.path == "/api/ask":
            if payload is None:
                self._send_json({"error": "请求格式错误"}, 400)
                return
            question = str(payload.get("question", "")).strip()
            session_id = str(payload.get("session_id", "")).strip()
            if not question:
                self._send_json({"error": "问题不能为空"}, 400)
                return
            # G3 可选字段：本次问答的联网许可与记忆许可（缺省 None → 按配置）
            allow_web, web_err = parse_opt_bool(payload.get("allow_web"))
            remember, mem_err = parse_opt_bool(payload.get("remember"))
            if web_err or mem_err:
                self._send_json({"error": f"{'allow_web' if web_err else 'remember'} {web_err or mem_err}"}, 400)
                return
            if not self.agent.config.llm_api_key:
                self._send_json({"error": "请先在设置中配置模型 API Key", "code": "MODEL_NOT_CONFIGURED"}, 428)
                return
            workspace = self._active_workspace()
            try:
                session_id = self._session_for_workspace(session_id, workspace["id"])
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            with self.lock:  # 串行化问答，保护共享 Agent 与会话记忆
                self.agent.memory = self.store.memory(session_id)
                answer = self.agent.ask(  # S4：长期记忆按会话读写；G3：remember/allow_web 请求级许可
                    question, session_id=session_id, remember=remember, allow_web=allow_web
                )
            LOG.info("问答 | q=%s | %ds | tokens in=%d out=%d | err=%s",
                     question[:60], round(answer.total_latency_s, 1),
                     answer.prompt_tokens, answer.completion_tokens, answer.error or "-")
            response = self._persist_answer(session_id, workspace["id"], question, answer)
            self._send_json({"question": question, "session_id": session_id, **response})
            return

        if self.path == "/api/ask_stream":
            if payload is None:
                self._send_json({"error": "请求格式错误"}, 400)
                return
            question = str(payload.get("question", "")).strip()
            session_id = str(payload.get("session_id", "")).strip()
            if not question:
                self._send_json({"error": "问题不能为空"}, 400)
                return
            # G3 可选字段：与 /api/ask 同口径（缺省 None → 按配置）
            allow_web, web_err = parse_opt_bool(payload.get("allow_web"))
            remember, mem_err = parse_opt_bool(payload.get("remember"))
            if web_err or mem_err:
                self._send_json({"error": f"{'allow_web' if web_err else 'remember'} {web_err or mem_err}"}, 400)
                return
            if not self.agent.config.llm_api_key:
                self._send_json({"error": "请先在设置中配置模型 API Key", "code": "MODEL_NOT_CONFIGURED"}, 428)
                return
            workspace = self._active_workspace()
            try:
                session_id = self._session_for_workspace(session_id, workspace["id"])
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            def emit(event, **data):
                self.wfile.write((json.dumps({"event": event, **data}, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            # W1：ask 内部真阶段事件经 on_stage 转发（检索知识库→生成答案）；首条覆盖 ask 启动前的空窗
            def emit_stage(name):
                emit("stage", name=name)
            emit_stage("规划与检索")
            try:
                with self.lock:
                    self.agent.memory = self.store.memory(session_id)
                    answer = self.agent.ask(  # S4：长期记忆按会话读写；G3：remember/allow_web 请求级许可
                        question, session_id=session_id, remember=remember, allow_web=allow_web,
                        on_stage=emit_stage,  # W1：阶段回调异常在 ask 内部吞掉，不影响生成
                    )
                response = self._persist_answer(session_id, workspace["id"], question, answer)
                emit("meta", session_id=session_id, message_id=response["message_id"], sources=response["sources"], sources_detail=response.get("sources_detail", []), plan=response["plan"], metrics=response["metrics"])
                text = response["answer"]
                for start in range(0, len(text), 24):
                    emit("delta", text=text[start:start + 24])
                emit("done", error=response["error"])
                # W3：追问建议（LLM 生成）。放在 done 之后：不拖慢正文与操作行的呈现；
                # 生成失败/未配置时静默省略该事件，前端隐藏追问区。
                if not response["error"]:
                    try:
                        items = self.agent.suggest_followups(question, response["answer"])
                    except Exception:  # noqa: BLE001 - 追问是增值信息，绝不影响流式收尾
                        LOG.debug("追问建议生成失败", exc_info=True)
                        items = []
                    if items:
                        emit("followups", items=items)
            except Exception:  # noqa: BLE001 - headers already sent; keep the NDJSON stream valid
                LOG.exception("流式问答生成失败")
                emit("done", error="回答生成失败，请检查模型配置或查看日志")
            return

        if self.path == "/api/ask_image":
            if payload is None:
                self._send_json({"error": "请求格式错误"}, 400)
                return
            image_data_url = str(payload.get("image_data_url", ""))
            question = str(payload.get("question", "")).strip()
            session_id = str(payload.get("session_id", "")).strip()
            if not self.agent.config.llm_api_key:
                self._send_json({"error": "请先在设置中配置模型 API Key", "code": "MODEL_NOT_CONFIGURED"}, 428)
                return
            if not image_data_url.startswith("data:image/"):
                self._send_json({"error": "图片数据无效"}, 400)
                return
            if len(image_data_url) > MAX_IMAGE_DATA_URL:
                self._send_json({"error": "图片过大（上限约 4MB）"}, 400)
                return
            try:
                vision = LLMClient.for_vision(self.agent.config)
            except LLMError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            try:
                description = vision.describe_image(image_data_url, question)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("视觉模型调用失败: %s", exc)
                self._send_json({"error": f"视觉模型调用失败: {exc}"}, 502)
                return
            full_question = (
                f"用户问题：{question}\n图片内容描述：{description}" if question else f"图片内容描述：{description}"
            )
            workspace = self._active_workspace()
            try:
                session_id = self._session_for_workspace(session_id, workspace["id"])
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            with self.lock:
                self.agent.memory = self.store.memory(session_id)
                answer = self.agent.ask(full_question, session_id=session_id)  # S4：长期记忆按会话读写
            LOG.info("图片问答 | q=%s | %ds | err=%s",
                     (question or "（无）")[:60], round(answer.total_latency_s, 1), answer.error or "-")
            response = self._persist_answer(session_id, workspace["id"], question or "图片问答", answer)
            self._send_json({"question": full_question, "image_description": description,
                             "session_id": session_id, **response})
            return

        # S5 创建任务：校验与 /api/ask 同口径，落盘 queued 并入队后立即返回
        if self.path == "/api/tasks":
            if payload is None:
                self._send_json({"error": "请求格式错误"}, 400)
                return
            question = str(payload.get("question", "")).strip()
            session_id = str(payload.get("session_id", "")).strip()
            if not question:
                self._send_json({"error": "问题不能为空"}, 400)
                return
            # G3 可选字段：本次任务的联网/记忆许可透传给执行（缺省 None → 按配置）
            allow_web, web_err = parse_opt_bool(payload.get("allow_web"))
            remember, mem_err = parse_opt_bool(payload.get("remember"))
            if web_err or mem_err:
                self._send_json({"error": f"{'allow_web' if web_err else 'remember'} {web_err or mem_err}"}, 400)
                return
            if not self.agent.config.llm_api_key:
                self._send_json({"error": "请先在设置中配置模型 API Key", "code": "MODEL_NOT_CONFIGURED"}, 428)
                return
            workspace = self._active_workspace()
            try:
                session_id = self._session_for_workspace(session_id, workspace["id"])
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            task_id = self.task_runner.submit(
                session_id, workspace["id"], question, remember=remember, allow_web=allow_web
            )
            LOG.info("任务已创建: %s | q=%s", task_id, question[:60])
            self._send_json({"ok": True, "task_id": task_id, "status": "queued"})
            return

        # S5 任务控制：/api/tasks/{id}/pause|resume|cancel
        # 路径段为 [api, tasks, {id}, action]，共 4 段
        if self.path.startswith("/api/tasks/"):
            parts = self.path.strip("/").split("/")
            action = parts[3] if len(parts) == 4 else ""
            if action not in ("pause", "resume", "cancel"):
                self._send_json({"error": "未知任务操作"}, 404)
                return
            try:
                if action == "pause":
                    status = self.task_runner.pause(parts[2])
                elif action == "resume":
                    status = self.task_runner.resume(parts[2])
                else:
                    status = self.task_runner.cancel(parts[2])
            except KeyError:
                self._send_json({"error": "任务不存在"}, 404)
                return
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 409)
                return
            self._send_json({"ok": True, "status": status})
            return

        if self.path == "/api/reset":
            session_id = str((payload or {}).get("session_id", "")).strip()
            workspace = self._active_workspace()
            if not session_id or not self.store.session_belongs_to(session_id, workspace["id"]):
                self._send_json({"error": "会话不存在或不属于当前工作区"}, 404)
                return
            self.store.clear_session(session_id)
            with self.lock:
                self.agent.reset_memory()
                # G4/P0-2：清空会话同步删除该会话产生的长期记忆（长期记忆关闭时跳过）
                lm = getattr(self.agent, "long_memory", None)
                removed = lm.delete_by_session(session_id) if lm is not None else 0
            LOG.info("重置会话记忆（同步删除长期记忆 %d 条）", removed)
            self._audit_admin("session_reset", True, session_id=session_id, detail=f"long_term_removed={removed}")
            self._send_json({"ok": True, "memory_turns": 0, "long_term_removed": removed})
            return

        # G4/P0-2：清空全部长期记忆与实体记忆
        if self.path == "/api/memory/clear":
            with self.lock:
                lm = getattr(self.agent, "long_memory", None)
                em = getattr(self.agent, "entity_memory", None)
                episodes = lm.clear() if lm is not None else 0
                entities = em.clear() if em is not None else 0
            LOG.info("清空长期记忆与实体记忆（episodes=%d，entities=%d）", episodes, entities)
            self._audit_admin("memory_clear", True, detail=f"episodes={episodes},entities={entities}")
            self._send_json({"ok": True, "episodes": episodes, "entities": entities})
            return

        if self.path == "/api/sessions":
            if str((payload or {}).get("action", "")) == "delete":
                session_id = str((payload or {}).get("session_id", "")).strip()
                workspace_id = self._active_workspace()["id"]
                if not session_id or not self.store.session_belongs_to(session_id, workspace_id):
                    self._send_json({"error": "会话不存在或不属于当前工作区"}, 404)
                    return
                self.store.delete_session(session_id)
                self._send_json({"ok": True})
                return
            workspace_id = str((payload or {}).get("workspace_id") or self._active_workspace()["id"])
            if workspace_id != self._active_workspace()["id"]:
                self._send_json({"error": "只能在当前工作区创建会话"}, 409)
                return
            session_id = self.store.create_session(workspace_id, str((payload or {}).get("title") or "新的会话"))
            self._send_json({"ok": True, "session_id": session_id})
            return

        if self.path == "/api/feedback":
            try:
                self.store.feedback(int((payload or {}).get("message_id", 0)), str((payload or {}).get("value", "")))
            except (TypeError, ValueError):
                self._send_json({"error": "反馈参数无效"}, 400)
                return
            self._send_json({"ok": True})
            return

        if self.path == "/api/workspaces":
            action = str((payload or {}).get("action", ""))
            if action == "create":
                name, path = str(payload.get("name", "")).strip(), str(payload.get("kb_path", "")).strip()
                if not name or not Path(path).expanduser().is_dir():
                    self._send_json({"error": "名称或知识库目录无效"}, 400)
                    return
                wid = self.store.create_workspace(name, str(Path(path).expanduser()))
                self._send_json({"ok": True, "workspace_id": wid})
                return
            if action == "switch":
                workspace_id = str(payload.get("workspace_id", ""))
                workspaces = {item["id"]: item for item in self.store.workspaces()}
                workspace = workspaces.get(workspace_id)
                if not workspace:
                    self._send_json({"error": "工作区不存在"}, 404)
                    return
                try:
                    path = Path(workspace["kb_path"])
                    if workspace["kb_path"] != self.agent.config.kb_path:
                        cfg = load_config()
                        cfg._raw["kb_path"] = str(path)
                        new_agent = Agent(cfg)
                        new_agent.build_index()
                        with self.lock:
                            type(self).agent = new_agent
                    self.store.activate_workspace(workspace_id)
                    save_runtime({"kb_path": str(path)})
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"error": f"工作区索引加载失败: {exc}"}, 500)
                    return
                self._send_json({"ok": True, "workspace": workspace})
                return
            self._send_json({"error": "工作区操作无效"}, 400)
            return

        if self.path == "/api/logs":
            if str((payload or {}).get("action")) != "clear":
                self._send_json({"error": "日志操作无效"}, 400)
                return
            log_file = self.agent.config.data_dir / "logs" / "myagents.log"
            try:
                log_file.write_text("", encoding="utf-8")
            except OSError as exc:
                self._send_json({"error": str(exc)}, 500)
                return
            self._send_json({"ok": True})
            return

        if self.path == "/api/config":
            if payload is None:
                self._send_json({"error": "请求格式错误"}, 400)
                return
            status, body = self._save_config(payload)
            self._send_json(body, status)
            return

        if self.path == "/api/kb":
            if payload is None:
                self._send_json({"error": "请求格式错误"}, 400)
                return
            path = str(payload.get("path", "")).strip()
            path = Path(path).expanduser()
            if not path.exists() or not path.is_dir():
                self._send_json({"error": f"目录不存在: {path}"}, 400)
                return
            md_count = len(list(path.rglob("*.md")))
            if md_count == 0:
                self._send_json({"error": f"目录中没有 Markdown 文件: {path}"}, 400)
                return

            def rebuild() -> None:
                try:
                    cfg = self.agent.config
                    old_path = cfg.kb_path
                    cfg._raw["kb_path"] = str(path)
                    save_runtime({"kb_path": str(path)})
                    new_agent = Agent(cfg)
                    try:
                        new_agent.build_index()
                    except Exception:
                        cfg._raw["kb_path"] = old_path
                        raise
                    with self.lock:
                        type(self).agent = new_agent
                    self.build_error = ""
                    LOG.info("知识库切换成功: %s", path)
                except Exception as exc:  # noqa: BLE001
                    self.build_error = f"{type(exc).__name__}: {exc}"
                    LOG.error("知识库重建失败 %s: %s", path, exc)
                finally:
                    self.building = False

            if not self.building:
                self.building = True
                self.build_error = ""
                LOG.info("开始重建知识库索引: %s", path)
                self._audit_admin("kb_rebuild", True, detail=str(path))
                threading.Thread(target=rebuild, daemon=True).start()
            self._send_json({"ok": True, "building": True, "path": str(path), "md_count": md_count})
            return

        self._send_json({"error": "未知接口"}, 404)

    def _save_config(self, payload: dict) -> tuple[int, dict]:
        """POST /api/config 处理体（G2 配置校验）。

        构建 overrides 后，把当前生效配置与 overrides 深合并出「拟生效配置」先行
        validate_config，失败返回 (400, {"error", "errors"}) 且不落盘、不热更新；
        通过则归一化 overrides（布尔字符串/枚举小写，落盘与内存口径一致）后
        save_runtime + _deep_update + reconfigure，返回 (200, {"ok", "config"})，
        既有成功响应结构不变。错误消息含完整 dotted 路径且绝不回显 Key 值。
        """
        overrides: dict = {}
        for section, fields in CONFIG_FIELDS.items():
            if section not in payload:
                continue
            section_dict = overrides.setdefault(section, {})
            for key, value in payload[section].items():
                if key not in fields:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                section_dict[key] = value
        key = payload.get("llm", {}).get("api_key", "")
        # P0-5 统一口径（G2 观察项 3）：与 vision.api_key 一致只接受字符串；掩码回显值不落盘。
        # 非 str（int/float 等）原样进入 overrides，由 validate_config 报类型错误 → 400。
        if isinstance(key, str):
            key = key.strip()
            if key and "****" not in key:
                overrides.setdefault("llm", {})["api_key"] = key
        elif key not in ("", None):
            overrides.setdefault("llm", {})["api_key"] = key
        # P0-5 密钥安全：runtime.json 权限过宽时拒绝保存新密钥（普通配置项不受影响）
        contains_new_key = (
            isinstance(overrides.get("llm", {}).get("api_key"), str)
            or isinstance(overrides.get("vision", {}).get("api_key"), str)
        )
        if contains_new_key and not key_file_permissions_ok(config_mod.RUNTIME_PATH):
            LOG.warning("runtime.json 权限过宽，拒绝保存新密钥")
            return 403, {
                "error": "runtime.json 权限过宽（组/其他用户可读），已拒绝保存新密钥。"
                         "请先执行 chmod 600 data/runtime.json 收紧权限后重试。"
            }
        if overrides:
            proposed = copy.deepcopy(self.agent.config._raw)
            _deep_update(proposed, overrides)
            errors = validate_config(proposed)
            if errors:
                LOG.warning("配置校验失败: %d 处错误，拒绝保存", len(errors))
                return 400, {"error": "配置校验失败", "errors": errors}
            normalize_config(overrides)
            try:
                with self.lock:
                    save_runtime(overrides)
                    _deep_update(self.agent.config._raw, overrides)
                    self.agent.reconfigure()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("配置保存失败: %s", exc)
                return 400, {"error": f"配置保存失败: {exc}"}
        LOG.info("配置已更新: %s", {s: list(v.keys()) for s, v in overrides.items()})
        # G6：配置保存管理事件（只记键名摘要，绝不记值）
        self._audit_admin(
            "config_save", True,
            detail=",".join(sorted(f"{s}.{k}" for s, keys in overrides.items() for k in keys)),
        )
        return 200, {"ok": True, "config": self._config_view()}

    def _config_view(self) -> dict:
        cfg = self.agent.config
        return {
            "llm": {
                "base_url": cfg.llm_base_url,
                "chat_model": cfg.llm_chat_model,
                "temperature": cfg.llm_temperature,
                "max_tokens": cfg.llm_max_tokens,
                "mode": cfg.llm_mode,
                "api_key_masked": mask_key(cfg.llm_api_key),
            },
            "retrieval": {
                "top_k": cfg.top_k,
                "rerank": cfg.rerank_mode,
                "rerank_candidates": cfg.rerank_candidates,
                "multi_query": cfg.multi_query,
                "reranker_model": cfg.reranker_model,
            },
            "vision": {
                "base_url": cfg.vision_base_url,
                "model": cfg.vision_model,
                "api_key_masked": mask_key(cfg.vision_api_key),
                "configured": cfg.vision_configured,
            },
            # G3：外发/记忆三开关只读透出（前端回填下拉，不改既有键）
            "tools": {
                "kb_fallback_web": cfg.kb_fallback_web,
            },
            "memory": {
                "long_term_enabled": cfg.memory_long_term_enabled,
                "entities_enabled": cfg.memory_entities_enabled,
            },
        }


def _deep_update(target: dict, src: dict) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="myAgents Web 前端")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅本机）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    config = load_config()
    setup_logging(config.data_dir / "logs")
    # G6：装配审计轨迹（audit.enabled=false 时装配 no-op 空审计器，埋点统一入口）
    if config.audit_enabled:
        audit_mod.set_logger(AuditLogger(
            config.data_dir / "audit",
            retention_days=config.audit_retention_days,
            log_content=config.audit_log_content,
        ))
        LOG.info("审计轨迹已启用: data/audit/（保留 %d 天，正文记录=%s）",
                 config.audit_retention_days, config.audit_log_content)
    LOG.info("myAgents Web 启动中… port=%s", args.port)
    Handler.agent = build_agent()
    Handler.store = WebStore(config.data_dir / "webui.sqlite3")
    Handler.store.ensure_default(config.kb_path)
    # S5：任务状态存储与后台运行器。启动时把遗留 queued/running 任务恢复为 paused，
    # 崩溃/中断的任务可经 POST /api/tasks/{id}/resume 继续。
    task_store = TaskStore(config.data_dir / "tasks")
    recovered = task_store.recover_running()
    if recovered:
        LOG.info("启动恢复：%d 个未完成任务转为 paused: %s", len(recovered), ", ".join(recovered))
    Handler.task_runner = TaskRunner(
        task_store,
        Handler.lock,
        agent_provider=lambda: Handler.agent,
        memory_provider=lambda session_id: Handler.store.memory(session_id),
        # T1：completed 任务把答案写回会话消息（回调在工作线程执行，persist_task_result 自行持锁）
        on_complete=lambda record: Handler.persist_task_result(record),
        # P1-2 看门狗阈值（config.yaml tasks.*）
        step_timeout_s=config.tasks_step_timeout_s,
        total_timeout_s=config.tasks_total_timeout_s,
        watchdog_interval_s=config.tasks_watchdog_interval_s,
    )
    Handler.task_runner.start_watchdog()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    Handler.server_ref = server
    Handler.last_heartbeat = time.monotonic()
    Handler.heartbeat_seen = False

    def stop_when_browser_closes() -> None:
        while True:
            time.sleep(5)
            if Handler.heartbeat_seen and time.monotonic() - Handler.last_heartbeat > 15:
                LOG.info("页面心跳已停止，自动关闭本地服务")
                server.shutdown()
                return

    threading.Thread(target=stop_when_browser_closes, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    LOG.info("前端已启动: %s", url)
    print(f"[web] 前端已启动: {url}（日志: {config.data_dir}/logs/myagents.log）")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Web 服务已退出")
        print("\n[web] 已退出")


if __name__ == "__main__":
    main()
