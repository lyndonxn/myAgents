"""LLM 客户端：DeepSeek（OpenAI 兼容 /chat/completions），纯 requests 实现。

能力：
- chat(messages) -> str：普通对话
- chat_json(messages) -> dict|list：要求结构化 JSON 输出并稳健解析
- 统计 usage（输入/输出 token）与耗时，供评测与成本估算

G11 离线档位：llm.mode=local 时指向本地 OpenAI 兼容端点（如 Ollama），
允许 API Key 为空（请求头不带 Authorization）；非 deepseek 模型的 JSON
输出约束退化为提示词侧强约束（修复轮机制不变）。
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


# G11：非 deepseek 模型（本地端点常见）不支持 response_format={"type":"json_object"}，
# chat_json 退而求其次，在 messages 副本末尾追加这条提示词侧强约束。
_JSON_ONLY_SYSTEM_HINT = (
    "只输出合法 JSON：不要输出任何解释文字、Markdown 代码围栏或其他内容，"
    "你的整条回复必须能被 json.loads 直接解析。"
)


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
        # G11：local 档位（本地 OpenAI 兼容端点，如 Ollama）允许 API Key 为空；
        # cloud 档位保持原行为（无 Key 抛错提示）。
        self._local_mode = str(getattr(config, "llm_mode", "cloud") or "cloud").lower() == "local"
        if not self.api_key and not self._local_mode:
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
    def _request_headers(self) -> dict:
        """构造请求头（G11）：API Key 为空（local 档位允许）时不带 Authorization 头。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

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
                    headers=self._request_headers(),
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

    def chat_stream(self, messages: list[dict], on_token=None, **kwargs) -> ChatResult:
        """流式 chat（OpenAI 兼容 SSE，spec/08 LG-2.1）。

        on_token(text) 对每个增量 chunk 回调（回调异常吞掉——前端断连不影响生成）；
        返回聚合后的完整 ChatResult。usage 依赖 stream_options.include_usage，
        端点不支持时 token 计数为 0（成本记 0，如实降级）。
        重试语义与 _chat 不同：仅在建立连接前可安全重试；一旦开始产出 token，
        失败直接抛 LLMError（重试会导致内容重复）。
        """
        url = f"{self.base_url}/chat/completions"
        payload: dict = {
            "model": kwargs.get("model") or self.config.llm_chat_model,
            "messages": messages,
            "temperature": kwargs.get("temperature") if kwargs.get("temperature") is not None else self.config.llm_temperature,
            "max_tokens": kwargs.get("max_tokens") or self.config.llm_max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        t0 = time.monotonic()
        last_err: Exception | None = None
        for attempt in range(self.config.llm_max_retries):
            try:
                resp = requests.post(
                    url,
                    headers=self._request_headers(),
                    json=payload,
                    timeout=self.config.llm_timeout,
                    stream=True,
                )
                if resp.status_code == 429:
                    resp.close()
                    time.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
            except Exception as e:  # noqa: BLE001 - 连接期失败可安全重试
                last_err = e
                time.sleep(1.0 * (attempt + 1))
                continue

            parts: list[str] = []
            usage: dict = {}
            try:
                # 按字节迭代再手动 utf-8 解码：decode_unicode=True 在端点不带 charset 时
                # 按 ISO-8859-1 解码，且 str.splitlines 会把 CJK 字节 0x85 当换行——
                # 中文内容会静默丢失（T-008 独立验收实测抓出；本地端点如 Ollama 高危）。
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        obj = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        parts.append(delta)
                        if on_token is not None:
                            try:
                                on_token(delta)
                            except Exception:  # noqa: BLE001 - 回调（前端断连等）不影响生成
                                pass
            except Exception as e:  # noqa: BLE001 - 已产出 token，不可安全重试
                resp.close()
                self._audit_llm(ok=False, latency_s=time.monotonic() - t0, error_type=type(e).__name__)
                raise LLMError(f"LLM 流式读取中断（已产出 {len(parts)} 个 chunk，不可重试）: {e}") from e

            latency = time.monotonic() - t0
            result = ChatResult(
                text="".join(parts).strip(),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                latency_s=latency,
                raw={"stream": True},
            )
            self._audit_llm(
                ok=True, latency_s=latency,
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            )
            return result
        raise LLMError(f"LLM 流式请求失败（重试 {self.config.llm_max_retries} 次后）: {last_err}")

    def chat_json(self, messages: list[dict], **kwargs) -> object:
        """要求 JSON 输出并稳健解析；解析失败走修复轮，耗尽后抛 LLMError。

        修复轮与 _chat 的网络重试是两层：本方法在"模型已返回但解析失败"时，
        把坏输出连同修复指令追加进对话再请求（llm.json_repair_rounds 轮）。
        G11：deepseek 模型用服务端 response_format 约束；其他模型（本地端点常见）
        在 messages 副本末尾追加提示词侧 JSON 强约束（不修改调用方列表），修复轮保持。
        """
        use_format = self.config.llm_chat_model.startswith(("deepseek-chat", "deepseek-reasoner"))
        if use_format:
            response_format: dict | None = {"type": "json_object"}
            request_messages = messages
        else:
            response_format = None
            request_messages = [*messages, {"role": "system", "content": _JSON_ONLY_SYSTEM_HINT}]
        result = self._chat_audited(request_messages, response_format=response_format, **kwargs)
        try:
            return parse_json_robust(result.text)
        except LLMError as first_err:
            rounds = max(0, int(getattr(self.config, "llm_json_repair_rounds", 1) or 0))
            if rounds <= 0:
                raise
            convo = list(request_messages)
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
