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
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agents.agent import Agent
from agents.config import PROJECT_ROOT, load_config, save_runtime
from agents.llm import LLMClient, LLMError
from agents.logger import get_logger, setup_logging
from agents.web_store import WebStore

PAGE = PROJECT_ROOT / "scripts" / "webui.html"
VENDOR_DIR = PROJECT_ROOT / "scripts" / "vendor"
LOG = get_logger("web")

# 可从前端编辑的配置项白名单（深层覆盖）
CONFIG_FIELDS = {
    "llm": {"base_url", "chat_model", "temperature", "max_tokens"},
    "retrieval": {"top_k", "rerank", "rerank_candidates", "multi_query", "reranker_model"},
    "vision": {"base_url", "model", "api_key"},
}
MAX_BODY = 10 * 1024 * 1024        # 请求体上限 10MB
MAX_IMAGE_DATA_URL = 6 * 1024 * 1024  # 图片 data URL 上限 6MB
MAX_QUESTION_LEN = 8000            # 问题文本上限（防超大 prompt 打爆 LLM 额度/卡服务）
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


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


class Handler(BaseHTTPRequestHandler):
    agent = None  # type: ignore[assignment]
    store = None  # type: ignore[assignment]
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
            session_id, "assistant", payload["answer"], payload["sources"], payload["plan"], payload["metrics"]
        )
        kb_hit = bool(payload["sources"]) and any(not str(source).startswith("http") for source in payload["sources"])
        self.store.record_query(workspace_id, session_id, kb_hit)
        payload["message_id"] = message_id
        return payload

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
            from urllib.parse import parse_qs, urlparse
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
        if self.path.startswith("/api/logs"):
            self._send_json(self._logs_view())
            return
        # 聊天页面
        if PAGE.exists():
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json({"error": "webui.html 缺失"}, 500)

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
                answer = self.agent.ask(question)
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
            emit("stage", name="规划与检索")
            try:
                with self.lock:
                    self.agent.memory = self.store.memory(session_id)
                    answer = self.agent.ask(question)
                response = self._persist_answer(session_id, workspace["id"], question, answer)
                emit("meta", session_id=session_id, message_id=response["message_id"], sources=response["sources"], plan=response["plan"], metrics=response["metrics"])
                text = response["answer"]
                for start in range(0, len(text), 24):
                    emit("delta", text=text[start:start + 24])
                emit("done", error=response["error"])
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
                answer = self.agent.ask(full_question)
            LOG.info("图片问答 | q=%s | %ds | err=%s",
                     (question or "（无）")[:60], round(answer.total_latency_s, 1), answer.error or "-")
            response = self._persist_answer(session_id, workspace["id"], question or "图片问答", answer)
            self._send_json({"question": full_question, "image_description": description,
                             "session_id": session_id, **response})
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
            LOG.info("重置会话记忆")
            self._send_json({"ok": True, "memory_turns": 0})
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
            key = str(payload.get("llm", {}).get("api_key", "")).strip()
            if key and "****" not in key:
                overrides.setdefault("llm", {})["api_key"] = key
            if overrides:
                try:
                    with self.lock:
                        save_runtime(overrides)
                        _deep_update(self.agent.config._raw, overrides)
                        self.agent.reconfigure()
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("配置保存失败: %s", exc)
                    self._send_json({"error": f"配置保存失败: {exc}"}, 400)
                    return
            LOG.info("配置已更新: %s", {s: list(v.keys()) for s, v in overrides.items()})
            self._send_json({"ok": True, "config": self._config_view()})
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
                threading.Thread(target=rebuild, daemon=True).start()
            self._send_json({"ok": True, "building": True, "path": str(path), "md_count": md_count})
            return

        self._send_json({"error": "未知接口"}, 404)

    def _config_view(self) -> dict:
        cfg = self.agent.config
        return {
            "llm": {
                "base_url": cfg.llm_base_url,
                "chat_model": cfg.llm_chat_model,
                "temperature": cfg.llm_temperature,
                "max_tokens": cfg.llm_max_tokens,
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
    LOG.info("myAgents Web 启动中… port=%s", args.port)
    Handler.agent = build_agent()
    Handler.store = WebStore(config.data_dir / "webui.sqlite3")
    Handler.store.ensure_default(config.kb_path)
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
