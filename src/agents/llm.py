"""LLM 客户端：DeepSeek（OpenAI 兼容 /chat/completions），纯 requests 实现。

能力：
- chat(messages) -> str：普通对话
- chat_json(messages) -> dict|list：要求结构化 JSON 输出并稳健解析
- 统计 usage（输入/输出 token）与耗时，供评测与成本估算
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import requests

from . import audit as audit_mod


class LLMError(RuntimeError):
    pass


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    raw: dict = field(default_factory=dict)


class LLMClient:
    def __init__(self, config, base_url: str | None = None, api_key: str | None = None):
        self.config = config
        self.base_url = (base_url or config.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else config.llm_api_key
        if not self.api_key:
            raise LLMError(
                "未找到 DEEPSEEK_API_KEY。请在项目根目录创建 .env（参考 .env.example）"
                "并填入你的 DeepSeek API Key。"
            )

    @classmethod
    def for_vision(cls, config) -> "LLMClient":
        """视觉模型客户端（图片搜索）。未配置时抛错提示。"""
        if not config.vision_model:
            raise LLMError(
                "尚未配置视觉模型。请到 设置 → 视觉模型 填写 model（如 glm-4v-flash / qwen-vl-plus / gpt-4o）"
                "与 base_url、API Key。"
            )
        return cls(config, base_url=config.vision_base_url, api_key=config.vision_api_key)

    def describe_image(self, image_data_url: str, question: str = "") -> str:
        """用视觉模型描述图片内容（用于图片检索/问答）。"""
        prompt = (
            "请仔细描述这张图片的内容：画面主体、文字、图表、界面元素、场景等，尽量具体、客观。"
            "如果图中包含文字或代码，请完整转录。"
            + (f"\n用户针对图片的补充问题：{question}" if question else "")
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ]
        result = self._chat(messages, model=self.config.vision_model, temperature=0.2, max_tokens=1024)
        return result.text

    # ---- 底层请求 ----
    def _chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> ChatResult:
        url = f"{self.base_url}/chat/completions"
        payload: dict = {
            "model": model or self.config.llm_chat_model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.llm_temperature,
            "max_tokens": max_tokens or self.config.llm_max_tokens,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format

        last_err: Exception | None = None
        for attempt in range(self.config.llm_max_retries):
            t0 = time.monotonic()
            try:
                resp = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.config.llm_timeout,
                )
                latency = time.monotonic() - t0
                if resp.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                content = choice["message"].get("content") or ""
                usage = data.get("usage", {})
                return ChatResult(
                    text=content.strip(),
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    latency_s=latency,
                    raw=data,
                )
            except Exception as e:  # noqa: BLE001 - 网络/HTTP/解析统一重试
                last_err = e
                time.sleep(1.0 * (attempt + 1))
        raise LLMError(f"LLM 请求失败（重试 {self.config.llm_max_retries} 次后）: {last_err}")

    # ---- 公开方法 ----
    def _chat_audited(self, messages: list[dict], **kwargs) -> ChatResult:
        """_chat + llm_call 审计（G6 埋点单点：公开路径统一走这里，子类覆写 _chat 也生效）。"""
        try:
            result = self._chat(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 记录后原样抛出
            self._audit_llm(ok=False, latency_s=0.0, error_type=type(exc).__name__)
            raise
        self._audit_llm(
            ok=True, latency_s=result.latency_s,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        )
        return result

    def _audit_llm(self, ok: bool, latency_s: float, prompt_tokens: int = 0,
                   completion_tokens: int = 0, error_type: str = "") -> None:
        """写 llm_call 审计事件（未装配审计器或已关闭时 no-op；永不记录 messages 与 Key）。"""
        audit = audit_mod.get()
        if audit is None or not getattr(self.config, "audit_enabled", True):
            return
        try:
            audit.log_llm_call(
                ok=ok, latency_s=latency_s, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, error_type=error_type,
                model=getattr(self.config, "llm_chat_model", ""),
            )
        except Exception:  # noqa: BLE001 - 审计失败不影响业务
            pass

    def chat(self, messages: list[dict], **kwargs) -> ChatResult:
        return self._chat_audited(messages, **kwargs)

    def chat_json(self, messages: list[dict], **kwargs) -> object:
        """要求 JSON 输出并稳健解析；解析失败走修复轮，耗尽后抛 LLMError。

        修复轮与 _chat 的网络重试是两层：本方法在"模型已返回但解析失败"时，
        把坏输出连同修复指令追加进对话再请求（llm.json_repair_rounds 轮）。
        """
        use_format = self.config.llm_chat_model.startswith(("deepseek-chat", "deepseek-reasoner"))
        response_format = {"type": "json_object"} if use_format else None
        result = self._chat_audited(messages, response_format=response_format, **kwargs)
        try:
            return parse_json_robust(result.text)
        except LLMError as first_err:
            rounds = max(0, int(getattr(self.config, "llm_json_repair_rounds", 1) or 0))
            if rounds <= 0:
                raise
            convo = list(messages)
            bad_text, last_err = result.text, first_err
            for _ in range(rounds):
                convo = convo + [
                    {"role": "assistant", "content": bad_text},
                    {
                        "role": "user",
                        "content": (
                            f"你的输出无法解析为 JSON：{last_err}。"
                            "请只输出修正后的合法 JSON，不要任何其他文字。"
                        ),
                    },
                ]
                result = self._chat_audited(convo, response_format=response_format, **kwargs)
                try:
                    return parse_json_robust(result.text)
                except LLMError as err:
                    bad_text, last_err = result.text, err
            # 修复轮耗尽仍失败：保留首次解析错误信息
            raise LLMError(f"{first_err}（经过 {rounds} 轮修复仍失败，最后错误: {last_err}）") from last_err

    # ---- 成本估算（DeepSeek 定价，元/百万 token） ----
    PRICING = {"input": 1.0, "output": 2.0}  # deepseek-chat 官方定价（元/1M tokens）

    @classmethod
    def estimate_cost(cls, result: ChatResult) -> float:
        return (result.prompt_tokens * cls.PRICING["input"] + result.completion_tokens * cls.PRICING["output"]) / 1_000_000


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_robust(text: str) -> object:
    """从 LLM 输出中稳健提取 JSON：先去代码围栏，再找首尾花括号/方括号。"""
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"无法从 LLM 输出解析 JSON: {text[:300]!r}")
