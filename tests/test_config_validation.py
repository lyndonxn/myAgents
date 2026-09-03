"""G2 配置校验测试：validate_config 规则表 / ConfigError / load_config 校验 / /api/config 保存校验。

全部离线（无 LLM、无网络）：CONFIG_PATH / RUNTIME_PATH / ENV_PATH 在 setUp 统一
monkeypatch 到 tempfile 临时目录、tearDown 恢复，绝不读写用户的 data/runtime.json、
.env 与 knowledge_base/。规则基线为 spec/upgrade-2026-09.md「G2 补充：校验规则表」，
验收来源为 spec/p0-p1-improvement.md P0-4。POST /api/config 用裸 Handler 实例驱动
（object.__new__(Handler)，同 tests/test_task_runner.py 的用法）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.config as config_mod
from agents.config import PROJECT_ROOT, Config, ConfigError, validate_config
from agents.web_server import Handler

# 仓库自带 config.yaml（只读，用作「现有配置必须合法」的基线样本）
REPO_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# 临时 config.yaml 的合法基线内容（与仓库 config.yaml 同口径的最小全量样本）
MINIMAL_CONFIG = """\
kb_path: "knowledge_base"
data_dir: "data"
chunking:
  min_chars: 80
  max_chars: 1200
embedding:
  backend: "auto"
  model: "BAAI/bge-small-zh-v1.5"
  hash_dim: 1024
retrieval:
  top_k: 6
  vector_weight: 0.7
  keyword_weight: 0.3
  fusion_mode: "rrf"
  rerank: "auto"
  rerank_candidates: 24
  rerank_blend: 0.7
  multi_query: false
  reranker_model: "BAAI/bge-reranker-base"
llm:
  base_url: "https://api.deepseek.com"
  chat_model: "deepseek-chat"
  temperature: 0.3
  max_tokens: 2048
  timeout: 60
  max_retries: 3
  json_repair_rounds: 1
planner:
  model: "deepseek-chat"
  temperature: 0.2
  max_steps: 5
  fallback_direct: true
  reflect: true
  max_reflections: 1
tools:
  search_default_top_k: 5
  max_retries: 1
  kb_fallback_web: true
  calculator_enabled: true
  time_enabled: true
  topics_enabled: true
  web_search_enabled: true
  web_search:
    timeout: 15
    cache_ttl: 300
memory:
  long_term_enabled: true
  entities_enabled: true
  max_episodes: 200
  embedding_backend: auto
"""


class ConfigValidationTests(unittest.TestCase):
    """G2 规则表逐条验证。每个用例对应任务清单一条（编号见各 docstring）。"""

    # ---------------- 夹具：路径全部指向临时目录，杜绝触碰用户文件 ----------------

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {
            name: getattr(config_mod, name) for name in ("CONFIG_PATH", "RUNTIME_PATH", "ENV_PATH")
        }
        config_mod.CONFIG_PATH = self.tmp / "config.yaml"
        config_mod.RUNTIME_PATH = self.tmp / "runtime.json"
        config_mod.ENV_PATH = self.tmp / ".env"

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(config_mod, name, value)
        self._tmp.cleanup()

    def write_minimal_config(self) -> None:
        """向临时 CONFIG_PATH 写入合法基线配置。"""
        config_mod.CONFIG_PATH.write_text(MINIMAL_CONFIG, encoding="utf-8")

    def write_runtime(self, data: dict) -> None:
        """向临时 RUNTIME_PATH 写入运行时覆盖。"""
        config_mod.RUNTIME_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def bare_handler(self) -> tuple[Handler, Config]:
        """裸 Handler 实例（跳过 socket 初始化）+ 最小 fake agent（config/reconfigure 桩）。"""
        handler = object.__new__(Handler)
        cfg = Config(yaml.safe_load(MINIMAL_CONFIG))
        handler.agent = SimpleNamespace(config=cfg, reconfigure=lambda: None)
        return handler, cfg

    # ---------------- 1) 仓库自带 config.yaml + 空 runtime → 空 errors ----------------

    def test_01_repo_config_yaml_is_valid(self):
        """现有配置必须合法：仓库自带 config.yaml 经 load_config 与 validate_config 均通过。

        CONFIG_PATH 指回仓库真实 config.yaml（只读）；RUNTIME_PATH/ENV_PATH 仍指向
        临时目录（不存在 → 空 runtime），不触碰用户 data/runtime.json。
        """
        config_mod.CONFIG_PATH = REPO_CONFIG_PATH
        cfg = config_mod.load_config()  # 不抛 ConfigError 即通过
        self.assertIsInstance(cfg, Config)
        raw = yaml.safe_load(REPO_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(validate_config(raw), [], "仓库自带 config.yaml 应零错误")

    # ---------------- 2) 整数规则：范围闭区间、bool 不算 int ----------------

    def test_02_integer_rules_top_k(self):
        """整数规则：top_k 0/101/"5"/True 各报错且消息含路径与期望；边界 1 与 100 通过。"""
        self.assertEqual(validate_config({"retrieval": {"top_k": 1}}), [], "下边界 1 应通过")
        self.assertEqual(validate_config({"retrieval": {"top_k": 100}}), [], "上边界 100 应通过")
        for bad in (0, 101, "5", True):
            errors = validate_config({"retrieval": {"top_k": bad}})
            self.assertEqual(len(errors), 1, f"top_k={bad!r} 应恰好 1 条错误: {errors}")
            self.assertIn("retrieval.top_k", errors[0], "错误消息应含完整 dotted 路径")
            self.assertIn("1–100", errors[0], "错误消息应含中文期望")
        self.assertIn("实际类型: str", validate_config({"retrieval": {"top_k": "5"}})[0])
        self.assertIn("实际类型: bool", validate_config({"retrieval": {"top_k": True}})[0],
                      "bool 不算 int，应报类型错误")

    # ---------------- 3) 浮点规则：int/float 均可、闭区间边界 ----------------

    def test_03_float_rules_temperature_and_blend(self):
        """浮点规则：temperature 2.5、rerank_blend 1.5 报错；0/2 与 0/1 边界通过。"""
        self.assertEqual(validate_config({"llm": {"temperature": 0}}), [], "int 0 应可当浮点边界")
        self.assertEqual(validate_config({"llm": {"temperature": 2}}), [])
        self.assertEqual(validate_config({"retrieval": {"rerank_blend": 0}}), [])
        self.assertEqual(validate_config({"retrieval": {"rerank_blend": 1}}), [])
        t_errors = validate_config({"llm": {"temperature": 2.5}})
        self.assertEqual(len(t_errors), 1)
        self.assertIn("llm.temperature", t_errors[0])
        self.assertIn("0–2", t_errors[0])
        b_errors = validate_config({"retrieval": {"rerank_blend": 1.5}})
        self.assertEqual(len(b_errors), 1)
        self.assertIn("retrieval.rerank_blend", b_errors[0])
        self.assertIn("0–1", b_errors[0])
        # bool 不算数值
        self.assertEqual(len(validate_config({"llm": {"temperature": True}})), 1)

    # ---------------- 4) 枚举规则：大小写不敏感、归一化、rerank 布尔兼容 ----------------

    def test_04_enum_rules(self):
        """枚举：非法值报错；"RRF" 大写通过并归一化小写；rerank 布尔 true/false 向后兼容。"""
        for raw, path in (
            ({"retrieval": {"rerank": "aggressive"}}, "retrieval.rerank"),
            ({"retrieval": {"fusion_mode": "bmx25"}}, "retrieval.fusion_mode"),
            ({"embedding": {"backend": "bert"}}, "embedding.backend"),
            ({"memory": {"embedding_backend": "bert"}}, "memory.embedding_backend"),
        ):
            errors = validate_config(raw)
            self.assertEqual(len(errors), 1, f"{raw} 应 1 条错误: {errors}")
            self.assertIn(path, errors[0])
        # 大小写不敏感：validate 通过，normalize 归一化小写
        self.assertEqual(validate_config({"retrieval": {"fusion_mode": "RRF"}}), [])
        self.assertEqual(validate_config({"retrieval": {"rerank": "Auto"}}), [])
        raw = {"retrieval": {"fusion_mode": "RRF", "rerank": "Auto"}}
        config_mod.normalize_config(raw)
        self.assertEqual(raw["retrieval"]["fusion_mode"], "rrf")
        self.assertEqual(raw["retrieval"]["rerank"], "auto")
        # 经 load_config 端到端：大写 RRF 归一化为 rrf
        self.write_minimal_config()
        config_mod.CONFIG_PATH.write_text(
            MINIMAL_CONFIG.replace('fusion_mode: "rrf"', 'fusion_mode: "RRF"'), encoding="utf-8"
        )
        cfg = config_mod.load_config()
        self.assertEqual(cfg.get("retrieval.fusion_mode"), "rrf")
        # rerank 布尔 true/false 向后兼容通过，且归一化不改写布尔
        for value in (True, False):
            raw = {"retrieval": {"rerank": value}}
            self.assertEqual(validate_config(raw), [])
            config_mod.normalize_config(raw)
            self.assertIs(raw["retrieval"]["rerank"], value)

    # ---------------- 5) 布尔规则：字符串归一化为真 bool ----------------

    def test_05_boolean_string_normalization(self):
        """布尔：kb_fallback_web="yes"/"1"/true 通过并归一化为真 bool；"maybe" 报错。"""
        self.write_minimal_config()
        for raw_value in ("yes", "1", "true", True):  # 前三个为字符串，末个为原生 bool
            self.write_runtime({"tools": {"kb_fallback_web": raw_value}})
            cfg = config_mod.load_config()
            self.assertIs(cfg.get("tools.kb_fallback_web"), True,
                          f"runtime 值 {raw_value!r} 应归一化为真布尔 True")
            self.assertIs(cfg.kb_fallback_web, True, "Config 属性应读到真布尔")
        self.write_runtime({"tools": {"kb_fallback_web": "no"}})
        cfg = config_mod.load_config()
        self.assertIs(cfg.get("tools.kb_fallback_web"), False)
        # 非法字符串：validate 直查报错，load_config 抛 ConfigError
        errors = validate_config({"tools": {"kb_fallback_web": "maybe"}})
        self.assertEqual(len(errors), 1)
        self.assertIn("tools.kb_fallback_web", errors[0])
        self.assertIn("true/false/1/0/yes/no", errors[0])
        self.write_runtime({"tools": {"kb_fallback_web": "maybe"}})
        with self.assertRaises(ConfigError):
            config_mod.load_config()

    # ---------------- 6) 字符串/格式规则：URL、非空、无空白、api_key 只查类型 ----------------

    def test_06_string_format_rules(self):
        """字符串/格式：base_url="ftp://x"、空 kb_path、chat_model="deep seek"、api_key=12345 报错且不回显值。"""
        # URL 前缀（非敏感键可回显实际值）
        errors = validate_config({"llm": {"base_url": "ftp://x"}})
        self.assertEqual(len(errors), 1)
        self.assertIn("llm.base_url", errors[0])
        self.assertIn("http", errors[0])
        self.assertIn("ftp://x", errors[0])
        # vision.base_url 非空时才校验；空串放行
        self.assertEqual(validate_config({"vision": {"base_url": ""}}), [])
        self.assertEqual(len(validate_config({"vision": {"base_url": "ftp://y"}})), 1)
        # kb_path 非空
        errors = validate_config({"kb_path": ""})
        self.assertEqual(len(errors), 1)
        self.assertIn("kb_path", errors[0])
        self.assertEqual(validate_config({"kb_path": "knowledge_base"}), [])
        # 模型名无空白
        errors = validate_config({"llm": {"chat_model": "deep seek"}})
        self.assertEqual(len(errors), 1)
        self.assertIn("llm.chat_model", errors[0])
        self.assertIn("deep seek", errors[0])
        self.assertEqual(validate_config({"llm": {"chat_model": "deepseek-chat"}}), [])
        # api_key 只查类型为 str：int 报错且错误消息绝不含实际值
        for dotted_raw in ({"llm": {"api_key": 12345}}, {"vision": {"api_key": 12345}}):
            errors = validate_config(dotted_raw)
            self.assertEqual(len(errors), 1, f"{dotted_raw} 应 1 条错误: {errors}")
            self.assertIn("api_key: 应为字符串", errors[0])
            self.assertIn("实际类型: int", errors[0])
            self.assertNotIn("12345", errors[0], "敏感键错误消息绝不能回显实际值")
        # 合法字符串（哪怕长得像密钥）只查类型即放行
        self.assertEqual(validate_config({"llm": {"api_key": "sk-anything-goes"}}), [])

    # ---------------- 7) 跨字段：chunking.max_chars > chunking.min_chars ----------------

    def test_07_cross_field_chunking(self):
        """跨字段：max_chars=100 < min_chars=200 报错；相等报错；正常顺序通过。"""
        errors = validate_config({"chunking": {"max_chars": 100, "min_chars": 200}})
        self.assertEqual(len(errors), 1)
        self.assertIn("chunking.max_chars", errors[0], "跨字段错误应挂在 max_chars 路径下")
        self.assertIn("chunking.min_chars", errors[0])
        self.assertEqual(len(validate_config({"chunking": {"max_chars": 200, "min_chars": 200}})), 1,
                         "相等也应报错（必须严格大于）")
        self.assertEqual(validate_config({"chunking": {"max_chars": 1200, "min_chars": 80}}), [])

    # ---------------- 8) 多错误一次全收集（不 fail-fast） ----------------

    def test_08_collects_all_errors_at_once(self):
        """多错误一次全收集：3 处错误 → list 长度 3 且各路径都出现。"""
        raw = {
            "retrieval": {"top_k": 0},
            "llm": {"temperature": 2.5, "chat_model": "deep seek"},
        }
        errors = validate_config(raw)
        self.assertEqual(len(errors), 3, f"应一次收集全部 3 处错误: {errors}")
        joined = "\n".join(errors)
        self.assertIn("retrieval.top_k", joined)
        self.assertIn("llm.temperature", joined)
        self.assertIn("llm.chat_model", joined)

    # ---------------- 9) load_config：非法 runtime → ConfigError 含全部错误、不泄 Key ----------------

    def test_09_load_config_raises_config_error(self):
        """load_config：含非法 top_k 的临时 runtime.json → 抛 ConfigError，
        消息逐行列出全部错误，且绝不含 api_key 值。"""
        self.write_minimal_config()
        self.write_runtime({
            "retrieval": {"top_k": 0},
            "llm": {"temperature": 9.9, "api_key": 12345},
        })
        with self.assertRaises(ConfigError) as ctx:
            config_mod.load_config()
        errors = ctx.exception.errors
        self.assertEqual(len(errors), 3, f"应收集全部 3 处错误: {errors}")
        self.assertEqual(str(ctx.exception), "\n".join(errors), "异常消息应为全部错误逐行拼接")
        joined = "\n".join(errors)
        self.assertIn("retrieval.top_k", joined)
        self.assertIn("llm.temperature", joined)
        self.assertIn("llm.api_key", joined)
        self.assertNotIn("12345", joined, "ConfigError 消息绝不能包含 api_key 值")

    # ---------------- 10) 保存路径：非法 overrides → 400 + errors，不落盘 ----------------

    def test_10_save_config_invalid_rejected(self):
        """保存路径：裸 Handler _save_config 带非法 overrides → (400, errors)，
        不写 runtime.json、不热更新内存配置，api_key 类型错误不回显值。"""
        handler, cfg = self.bare_handler()
        status, body = handler._save_config({"retrieval": {"top_k": 101}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "配置校验失败")
        self.assertTrue(body["errors"], "400 响应应含 errors 列表")
        self.assertIn("retrieval.top_k", body["errors"][0])
        self.assertFalse(config_mod.RUNTIME_PATH.exists(), "校验失败不得写 runtime.json")
        self.assertEqual(cfg.get("retrieval.top_k"), 6, "校验失败不得热更新内存配置")
        # api_key 类型错误经保存路径同样不回显值
        status, body = handler._save_config({"vision": {"api_key": 12345}})
        self.assertEqual(status, 400)
        joined = "\n".join(body["errors"])
        self.assertIn("vision.api_key", joined)
        self.assertNotIn("12345", joined)

    # ---------------- 10b) 保存路径：合法 overrides → 200、落盘、重启仍生效 ----------------

    def test_10b_save_config_valid_persists_and_reloads(self):
        """保存路径：合法 overrides → (200, ok)，临时 runtime.json 已写入，
        重新 load_config（patch 后）读到新值（「重启仍生效」模拟），
        布尔字符串 overrides 在落盘前归一化为真 bool。"""
        self.write_minimal_config()
        handler, cfg = self.bare_handler()
        status, body = handler._save_config({
            "retrieval": {"top_k": 9, "multi_query": "yes"},
            "llm": {"chat_model": "deepseek-reasoner"},
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(config_mod.RUNTIME_PATH.exists(), "合法保存应写入 runtime.json")
        saved = json.loads(config_mod.RUNTIME_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["retrieval"]["top_k"], 9)
        self.assertIs(saved["retrieval"]["multi_query"], True, "落盘前应归一化为真 bool")
        self.assertEqual(saved["llm"]["chat_model"], "deepseek-reasoner")
        self.assertIs(cfg.get("retrieval.multi_query"), True, "内存配置应同步归一化热更新")
        self.assertEqual(cfg.get("retrieval.top_k"), 9)
        # 「重启仍生效」：重新 load_config（CONFIG_PATH/RUNTIME_PATH 均已 patch 到临时目录）
        cfg2 = config_mod.load_config()
        self.assertEqual(cfg2.get("retrieval.top_k"), 9)
        self.assertIs(cfg2.get("retrieval.multi_query"), True)
        self.assertEqual(cfg2.get("llm.chat_model"), "deepseek-reasoner")

    # ---------------- 11) 兼容：空 payload → 不写文件、返回 200 ----------------

    def test_11_save_config_empty_payload_noop(self):
        """兼容：不传任何 overrides 的空 payload → 不写文件、返回 200，行为不变。"""
        handler, _cfg = self.bare_handler()
        status, body = handler._save_config({})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(config_mod.RUNTIME_PATH.exists(), "空 payload 不得写 runtime.json")
        self.assertIn("config", body, "成功响应结构应保持不变")


if __name__ == "__main__":
    unittest.main()
