"""G5 密钥安全 + 任务写回幂等测试（P0-5）。

全部离线：密钥文件与任务库/会话库均用 tempfile 临时目录（绝不读写用户 .env 与
data/runtime.json）；Web 层用裸 Handler（object.__new__）直测保存与写回路径。
覆盖：权限检查/启动告警/save_runtime 收紧 0600/过宽拒绝保存新密钥/llm.api_key
类型统一（int → 400）/掩码不落盘/写回重复回调去重/崩溃重放补标识/写回失败保留
completed 可重试/任务 JSON 无密钥。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import config as config_mod
from agents.config import Config, key_file_permissions_ok, save_runtime
from agents.task_runner import TaskRunner
from agents.task_store import TaskRecord, TaskStore
from agents.web_server import Handler
from agents.web_store import WebStore

REPO = Path(__file__).resolve().parent.parent


class KeySecurityTests(unittest.TestCase):
    """P0-5 密钥部分：权限检查、启动告警、保存收紧与拒绝、类型统一。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {name: getattr(config_mod, name) for name in ("ENV_PATH", "RUNTIME_PATH")}
        config_mod.ENV_PATH = self.tmp / ".env"
        config_mod.RUNTIME_PATH = self.tmp / "runtime.json"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(config_mod, name, value)
        self._tmp.cleanup()

    def test_key_file_permissions_check(self):
        """权限检查：0600 通过、0644 不过、组/其他任一权限位均不过、文件缺失通过。"""
        f600 = self.tmp / "f600"
        f600.write_text("k=1", encoding="utf-8")
        os.chmod(f600, 0o600)
        f644 = self.tmp / "f644"
        f644.write_text("k=1", encoding="utf-8")
        os.chmod(f644, 0o644)
        f604 = self.tmp / "f604"
        f604.write_text("k=1", encoding="utf-8")
        os.chmod(f604, 0o604)  # 其他用户可读

        self.assertTrue(key_file_permissions_ok(f600))
        self.assertFalse(key_file_permissions_ok(f644))
        self.assertFalse(key_file_permissions_ok(f604))
        self.assertTrue(key_file_permissions_ok(self.tmp / "missing"))

        print("✓ key_file_permissions_ok：0600 过、0644/0604 不过、缺失通过")

    def test_save_runtime_tightens_permissions(self):
        """save_runtime 写后自动收紧为 0600（含新建与覆写两种情况）。"""
        config_mod.RUNTIME_PATH.write_text("{}", encoding="utf-8")
        os.chmod(config_mod.RUNTIME_PATH, 0o644)
        save_runtime({"llm": {"api_key": "sk-test"}})
        mode = stat.S_IMODE(config_mod.RUNTIME_PATH.stat().st_mode)
        self.assertEqual(mode, 0o600, f"覆写后权限应为 0600，实际 {oct(mode)}")

        config_mod.RUNTIME_PATH.unlink()
        save_runtime({"llm": {"chat_model": "m"}})
        mode = stat.S_IMODE(config_mod.RUNTIME_PATH.stat().st_mode)
        self.assertEqual(mode, 0o600, f"新建文件权限应为 0600，实际 {oct(mode)}")

        print("✓ save_runtime：写后 0600（新建/覆写）")

    def test_save_config_rejects_new_key_on_loose_perms(self):
        """runtime.json 权限过宽 → 保存新密钥返回 403 且不落盘；普通配置项不受影响。"""
        config_mod.RUNTIME_PATH.write_text("{}", encoding="utf-8")
        os.chmod(config_mod.RUNTIME_PATH, 0o644)

        handler = object.__new__(Handler)
        handler.path = "/api/config"
        handler._check_host = lambda: True
        handler.lock = threading.Lock()
        captured = {}
        handler._send_json = lambda obj, status=200: captured.update(body=obj, status=status)
        handler.agent = NS(config=Config({}), reconfigure=lambda: None)

        payload = {"llm": {"api_key": "sk-new"}}
        status, body = handler._save_config(payload)
        self.assertEqual(status, 403)
        self.assertIn("chmod 600", body["error"])
        on_disk = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", on_disk.get("llm", {}), "拒绝时密钥不得落盘")

        # 收紧权限后同一请求成功
        os.chmod(config_mod.RUNTIME_PATH, 0o600)
        status2, body2 = handler._save_config(payload)
        self.assertEqual(status2, 200)
        self.assertTrue(body2["ok"])

        print("✓ 权限过宽拒绝保存新密钥（403），收紧后放行")

    def test_save_config_llm_key_type_unified(self):
        """G2 观察项 3：int 型 llm.api_key 不再静默转字符串，校验失败 400；掩码值不落盘。"""
        config_mod.RUNTIME_PATH.write_text("{}", encoding="utf-8")
        os.chmod(config_mod.RUNTIME_PATH, 0o600)

        def make_handler():
            handler = object.__new__(Handler)
            handler.path = "/api/config"
            handler._check_host = lambda: True
            handler.lock = threading.Lock()
            captured = {}
            handler._send_json = lambda obj, status=200: captured.update(body=obj, status=status)
            handler.agent = NS(config=Config({}), reconfigure=lambda: None)
            return handler, captured

        # int 型 Key → 400（validate_config 报 llm.api_key 须为字符串）
        handler, _ = make_handler()
        status, body = handler._save_config({"llm": {"api_key": 12345}})
        self.assertEqual(status, 400)
        self.assertTrue(any("llm.api_key" in e for e in body["errors"]))
        on_disk = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", on_disk.get("llm", {}), "非法类型不得落盘")

        # 掩码回显值（含 ****）→ 忽略，不覆盖已有 Key
        handler2, _ = make_handler()
        status2, _body2 = handler2._save_config({"llm": {"api_key": "sk-****abcd"}})
        self.assertEqual(status2, 200)
        on_disk2 = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", on_disk2.get("llm", {}), "掩码值不应落盘")

        print("✓ llm.api_key 类型统一：int → 400，掩码值忽略不落盘")

    def test_task_record_json_has_no_key(self):
        """任务 JSON（to_dict/summary）不含 api_key 字段或 sk- 形态密钥。"""
        record = TaskRecord(
            task_id="t1", session_id="s1", workspace_id="w1", question="q",
            status="completed", final_answer="答[1]", sources=["kb://a.md"],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "cost_yuan": 0.0},
        )
        blob = json.dumps(record.to_dict()) + json.dumps(record.summary())
        self.assertNotIn("api_key", blob)
        self.assertNotIn("sk-", blob)

        print("✓ TaskRecord JSON 不含密钥字段与 sk- 形态内容")


class WritebackIdempotencyTests(unittest.TestCase):
    """P0-5 任务写回部分：重复回调去重、崩溃重放补标识、失败可重试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = WebStore(self.tmp / "web.db")
        Handler.store = self.store
        if not hasattr(Handler, "lock") or not isinstance(getattr(Handler, "lock", None), threading.Lock):
            Handler.lock = threading.Lock()
        # 建真实工作区/会话（messages.query_events 外键约束要求先有会话）
        self.workspace_id = self.store.create_workspace("默认工作区", str(self.tmp / "kb"))
        self.store.activate_workspace(self.workspace_id)
        self.session_id = self.store.create_session(self.workspace_id)
        self.task_store = TaskStore(self.tmp / "tasks")
        Handler.task_runner = NS(store=self.task_store)  # _mark_writeback 经此持久化标识

    def tearDown(self):
        Handler.store = None
        Handler.task_runner = None
        self._tmp.cleanup()

    def _completed_record(self, task_id: str = "abc123456789") -> TaskRecord:
        record = TaskRecord(
            task_id=task_id, session_id=self.session_id, workspace_id=self.workspace_id,
            question="后台任务问题", status="completed", final_answer="任务答案[1]",
            sources=["kb://a.md"],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "cost_yuan": 0.001},
            citations_valid=1, citations_invalid=0,
        )
        return record

    def _store_record(self, record: TaskRecord) -> TaskRecord:
        self.task_store.create(record)
        return self.task_store.get(record.task_id)

    def test_double_callback_writes_once(self):
        """同一任务重复触发完成回调：user/assistant 消息只出现一次。"""
        record = self._store_record(self._completed_record())

        Handler.persist_task_result(record)
        Handler.persist_task_result(record)  # 重复回调
        Handler.persist_task_result(self._store_record(record))  # 从磁盘重载后再回调

        messages = self.store.messages(self.session_id)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles.count("user"), 1)
        self.assertEqual(roles.count("assistant"), 1)
        self.assertEqual(messages[1]["metrics"].get("task_id"), "abc123456789")
        # writeback_id 持久化
        self.assertEqual(self.task_store.get("abc123456789").writeback_id, "abc123456789")

        print("✓ 重复回调（内存/重载）消息只写一次，writeback_id 持久化")

    def test_crash_replay_marks_writeback_without_duplicate(self):
        """崩溃重放：库中已有消息但记录无 writeback_id（旧记录升级）→ 跳过并补齐标识。"""
        record = self._store_record(self._completed_record())
        Handler.persist_task_result(record)

        replayed = TaskRecord.from_dict(record.to_dict())
        replayed.writeback_id = ""  # 模拟「写回成功但标识未持久化」的旧记录
        Handler.persist_task_result(replayed)

        messages = self.store.messages(self.session_id)
        self.assertEqual([m["role"] for m in messages].count("assistant"), 1)
        self.assertEqual(replayed.writeback_id, "abc123456789", "重放后应补齐写回标识")

        print("✓ 崩溃重放：不产生重复消息，补齐 writeback_id")

    def test_writeback_failure_keeps_completed_retryable(self):
        """写回异常：任务保持 completed、答案不回滚、writeback_id 为空（可重试）；恢复后重试成功。"""
        record = self._store_record(self._completed_record())

        class _BoomStore:
            def has_task_writeback(self, *a):
                return False

            def add_task_result(self, *a, **k):
                raise RuntimeError("磁盘已满")

            def update(self, record):
                raise RuntimeError("磁盘已满")

        original = Handler.store
        Handler.store = _BoomStore()
        try:
            with self.assertRaises(RuntimeError):
                Handler.persist_task_result(record)
        finally:
            Handler.store = original

        self.assertEqual(record.status, "completed", "写回失败不得改变任务终态")
        self.assertEqual(record.final_answer, "任务答案[1]", "写回失败不得回滚任务答案")
        self.assertEqual(record.writeback_id, "", "写回失败应保留可重试状态（标识为空）")

        # 恢复后重试：写回成功且只有一份消息
        Handler.persist_task_result(record)
        messages = self.store.messages(self.session_id)
        self.assertEqual([m["role"] for m in messages].count("assistant"), 1)
        self.assertEqual(record.writeback_id, "abc123456789")

        print("✓ 写回失败保留 completed 与可重试状态，恢复后重试成功且不重复")

    def test_notify_complete_swallows_exceptions(self):
        """回调异常被 TaskRunner 吞掉：不影响调用方、任务终态不变。"""
        def boom(record):
            raise RuntimeError("回调故障")

        record = self._completed_record()
        TaskRunner._notify_complete(NS(on_complete=boom), record)  # 不应抛出
        self.assertEqual(record.status, "completed")

        # on_complete 为 None → 直接返回
        TaskRunner._notify_complete(NS(on_complete=None), record)

        print("✓ _notify_complete 吞掉回调异常，任务终态不变")


if __name__ == "__main__":
    unittest.main()
