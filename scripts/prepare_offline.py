"""G11 本地 LLM 离线档位一键准备/体检脚本。

用法：
  python -m scripts.prepare_offline --check                 # 只读体检（默认动作）
  python -m scripts.prepare_offline --check --config X.yaml # 指定配置文件（测试隔离用）
  python -m scripts.prepare_offline --prepare               # 打印准备指引（不做真实下载）

--check：只读检查并输出中文报告，绝不下载、绝不改配置——
  1) embedding 后端可离线加载（local_files_only 尝试，失败给安装/预下载提示）；
  2) reranker 缓存是否存在（只报告不下载；缺失不阻断，运行时自动回退无精排）；
  3) llm.mode 是否 local；4) llm.base_url 是否本机地址；5) tools.web_search_enabled 是否 false。
  全部就绪退出码 0，否则 1。

--prepare：只打印准备指引（ollama pull 建议与依赖检查），不做任何下载。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agents.config as config_mod  # noqa: E402
from agents.config import Config, load_config  # noqa: E402

# 离线档位建议的本地对话模型（Ollama 命令示例）
_SUGGESTED_OLLAMA_MODEL = "qwen2.5:7b"
_SUGGESTED_BASE_URL = "http://localhost:11434/v1"


def _load_sentence_transformer(model_name: str):
    """以 local_files_only 方式加载模型（绝不联网下载）；失败由调用方捕获并报告。"""
    from sentence_transformers import SentenceTransformer  # 延迟导入：未安装时给安装提示

    return SentenceTransformer(model_name, local_files_only=True)


def check_embedding_offline(config: Config) -> tuple[bool, str]:
    """embedding 后端能否离线获得向量：tfidf 兜底直接就绪；auto/local 尝试本地缓存加载。"""
    if config.embedding_backend == "tfidf":
        return True, "embedding.backend=tfidf：内置 TF-IDF 后端，零模型依赖，天然离线可用"
    try:
        _load_sentence_transformer(config.embedding_model)
    except Exception as exc:  # noqa: BLE001 - 未安装/未缓存统一给修复建议
        return False, (
            f"embedding 模型 {config.embedding_model} 无法离线加载（{type(exc).__name__}）。修复建议："
            f"联网时预下载一次（python -c \"from sentence_transformers import SentenceTransformer; "
            f"SentenceTransformer('{config.embedding_model}')\"），"
            "或安装 sentence-transformers（pip install 'sentence-transformers>=3.0'），"
            "或将 embedding.backend 设为 tfidf 使用内置向量。"
        )
    return True, f"embedding 模型 {config.embedding_model} 已在本机缓存，可离线加载"


def _hf_cache_exists(model_name: str) -> bool:
    """检查 Hugging Face hub 缓存目录是否已有该模型（只读探测，不联网）。"""
    hub_dir = Path(os.environ.get("HF_HUB_CACHE", Path.home() / ".cache" / "huggingface" / "hub"))
    return (hub_dir / ("models--" + model_name.replace("/", "--"))).is_dir()


def check_reranker_cache(config: Config) -> tuple[bool, str]:
    """reranker 缓存是否存在（只报告不下载）：缺失不阻断退出码——运行时自动回退无精排。"""
    model = config.reranker_model
    if Path(model).is_dir():
        return True, f"reranker 使用本地模型目录 {model}（仅报告项，缺失也不阻断）"
    if _hf_cache_exists(model):
        return True, f"reranker 模型 {model} 已在本机缓存（仅报告项，缺失也不阻断）"
    return False, (
        f"reranker 模型 {model} 未在本机缓存（仅报告项，缺失也不阻断：运行时自动回退无精排）。"
        "如需离线精排，可联网时预下载，或将 retrieval.rerank 设为 off。"
    )


def check_llm_mode(config: Config) -> tuple[bool, str]:
    """llm.mode 是否已切到 local 离线档位。"""
    mode = config.llm_mode
    if mode != "local":
        return False, (
            f"llm.mode 当前为 {mode}。修复建议：在设置面板或 config.yaml 把 llm.mode 改为 local"
            f"（本地 OpenAI 兼容端点，数据不出本机）。"
        )
    return True, "llm.mode=local：离线档位已开启（空 API Key 可用，联网检索运行期强制禁用）"


def check_llm_base_url(config: Config) -> tuple[bool, str]:
    """llm.base_url 是否指向本机地址（localhost / 127.0.0.1 / [::1]）。"""
    url = config.llm_base_url
    if "://localhost" in url or "://127.0.0.1" in url or "://[::1]" in url:
        return True, f"llm.base_url={url}：本机端点"
    return False, (
        f"llm.base_url 当前为 {url}，不是本机地址。修复建议：改为本地 OpenAI 兼容端点"
        f"（如 Ollama：{_SUGGESTED_BASE_URL}）。"
    )


def check_web_search_disabled(config: Config) -> tuple[bool, str]:
    """tools.web_search_enabled 是否关闭（离线档位运行期还会强制钳制 allow_web，双保险）。"""
    if bool(config.get("tools.web_search_enabled", True)):
        return False, (
            "tools.web_search_enabled 当前为 true。修复建议：设为 false 关闭 Web 搜索注册"
            "（离线档位运行期也会强制钳制 allow_web，二者叠加双保险）。"
        )
    return True, "tools.web_search_enabled=false：Web 搜索已关闭"


# 参与退出码判定的检查项（顺序即报告顺序）；reranker 只报告、不阻断
_GATING_CHECKS = (check_llm_mode, check_llm_base_url, check_web_search_disabled, check_embedding_offline)
_REPORT_ONLY_CHECKS = (check_reranker_cache,)


def _run_check(config: Config) -> int:
    """执行 --check 体检：打印中文报告，全部阻断项就绪返回 0，否则 1。"""
    print("=" * 62)
    print("myAgents 离线档位体检（--check，只读，不下载不改配置）")
    print("=" * 62)
    failures = 0
    for check in _GATING_CHECKS:
        ok, message = check(config)
        print(f"[{'通过' if ok else '未就绪'}] {message}")
        if not ok:
            failures += 1
    for check in _REPORT_ONLY_CHECKS:
        ok, message = check(config)
        print(f"[{'通过' if ok else '提示'}] {message}")
    print("-" * 62)
    if failures:
        print(f"结果：{failures} 项未就绪（见上方「未就绪」条目与修复建议），退出码 1。")
        return 1
    print("结果：离线档位全部就绪，可以断网使用，退出码 0。")
    return 0


def _dependency_line(module: str, install_hint: str) -> str:
    """依赖检查行（--prepare 用）：可导入即就绪，否则给安装命令。"""
    try:
        __import__(module)
    except Exception:  # noqa: BLE001 - 导入失败统一按未安装报告
        return f"  [缺失] {module}：{install_hint}"
    return f"  [已装] {module}"


def _run_prepare(config: Config) -> int:
    """执行 --prepare：只打印准备指引与依赖检查结论，不做真实下载。"""
    print("=" * 62)
    print("myAgents 离线档位准备指引（--prepare，本期不下载任何模型）")
    print("=" * 62)
    print(f"1. 对话模型（Ollama）：安装并启动 Ollama 后执行  ollama pull {_SUGGESTED_OLLAMA_MODEL}")
    print(f"   然后在设置中把 llm.mode 改为 local、llm.base_url 改为 {_SUGGESTED_BASE_URL}、")
    print(f"   llm.chat_model 改为 {_SUGGESTED_OLLAMA_MODEL}（当前 chat_model={config.llm_chat_model}）。")
    ok, message = check_embedding_offline(config)
    print(f"2. embedding 模型缓存：{'已就绪' if ok else '未就绪'}——{message}")
    ok, message = check_reranker_cache(config)
    print(f"3. reranker 模型缓存：{message}")
    print("4. Python 依赖检查：")
    print(_dependency_line("sentence_transformers", "pip install 'sentence-transformers>=3.0'（不装则自动用 TF-IDF 兜底）"))
    print(_dependency_line("jieba", "pip install jieba"))
    print(_dependency_line("numpy", "pip install numpy"))
    print(_dependency_line("yaml", "pip install pyyaml"))
    print("5. 收尾：把 tools.web_search_enabled 设为 false，然后运行 --check 验证退出码为 0。")
    return 0


def main(argv: list[str] | None = None) -> int:
    """入口：--check 体检（默认）/ --prepare 指引；--config 指定配置文件（测试隔离）。"""
    parser = argparse.ArgumentParser(description="myAgents 本地 LLM 离线档位一键准备/体检")
    parser.add_argument("--check", action="store_true", help="只读体检：输出报告，全部就绪退出 0 否则 1（默认动作）")
    parser.add_argument("--prepare", action="store_true", help="打印准备指引与依赖检查（不做真实下载）")
    parser.add_argument("--config", help="配置文件路径（默认仓库 config.yaml；测试可指向临时文件）")
    args = parser.parse_args(argv)
    if args.config:
        config_mod.CONFIG_PATH = Path(args.config).expanduser().resolve()
    config = load_config()
    if args.prepare:
        return _run_prepare(config)
    return _run_check(config)


if __name__ == "__main__":
    sys.exit(main())
