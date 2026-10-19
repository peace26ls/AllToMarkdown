"""LLM Provider 抽象：先实现 OpenAI（及兼容接口，如 DeepSeek）。

设计要点（对接交接文档第 6 节 / Phase 2）：
- 整理层（organize.py）只依赖本模块的抽象接口，**不直接 touch openai SDK**，
  将来替换为其他云端/本地 provider 都不影响整理层。
- 所有返回都是「字符串文本」；需要 JSON 时上层调 ``complete_json``，
  该函数内置了 ```json 围栏剥离、严格解析与失败重试。
- 配置全部走环境变量：OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL。
  未配置 key 时仍可 import 本模块（client 延迟到调用时创建），
  便于测试或在未联网环境做 organize 逻辑验证时注入 FakeProvider。
"""
from __future__ import annotations

import base64
import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any

# 从 LLM 输出中提取首个 JSON 对象（容忍 ```json 围栏 / 前后夹带的说明文字）
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


class LLMError(Exception):
    """LLM 调用/解析层面的错误。"""


class LLMProvider(ABC):
    """统一补全接口。实现方负责与具体供应商通信。"""

    @abstractmethod
    def complete(self, user: str, *, system: str | None = None,
                 temperature: float = 0.2, max_tokens: int | None = None) -> str:
        """发起一次补全，返回模型输出的原始文本。"""

    def complete_vision(self, user: str, images: list[bytes], *,
                        system: str | None = None,
                        temperature: float = 0.2,
                        max_tokens: int | None = None) -> str:
        """多模态补全：把图片与文字一起送模型，返回模型输出文本。

        默认不支持（普通文本模型没有视觉能力）；子类实现后即可用于
        Phase 4 的「高质量模式」。显式 raise 便于调用方降级而不是静默出错。
        """
        raise LLMError(f"{self.name} 不支持多模态（vision）输入")

    @property
    @abstractmethod
    def name(self) -> str:
        """provider 标识，用于日志/meta。"""


class OpenAIProvider(LLMProvider):
    """OpenAI 兼容接口实现（openai SDK）。

    兼容 OpenAI 官方与 DeepSeek 等任何兼容 /chat/completions 的端点：
    通过 OPENAI_BASE_URL 指向对应服务。
    """

    def __init__(self, *, api_key: str | None = None,
                 base_url: str | None = None, model: str | None = None):
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._base_url = base_url or os.getenv("OPENAI_BASE_URL") or None
        self._model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self._client = None  # 延迟实例化

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            if not self._api_key:
                raise LLMError(
                    "未配置 OPENAI_API_KEY，无法调用 LLM（整理层 Phase 2 需要云端模型）。"
                )
            kwargs: dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = OpenAI(**kwargs)
        return self._client

    def complete(self, user: str, *, system: str | None = None,
                 temperature: float = 0.2, max_tokens: int | None = None) -> str:
        client = self._get_client()
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        import openai

        try:
            resp = client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # 网络/鉴权/限流等
            raise LLMError(f"LLM 调用失败: {exc}") from exc

        content: str | None = resp.choices[0].message.content if resp.choices else None
        if not content:
            raise LLMError("LLM 返回空内容")
        return content

    def complete_vision(self, user: str, images: list[bytes], *,
                        system: str | None = None,
                        temperature: float = 0.2,
                        max_tokens: int | None = None) -> str:
        """OpenAI 兼容的 vision 补全：图片以 base64 data URI 随消息发送。"""
        client = self._get_client()
        content: list[dict] = [{"type": "text", "text": user}]
        for img in images:
            b64 = base64.b64encode(img).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        try:
            resp = client.chat.completions.create(
                model=self._model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 网络/鉴权/限流/模型不支持视觉
            raise LLMError(f"多模态调用失败: {exc}") from exc

        out: str | None = resp.choices[0].message.content if resp.choices else None
        if not out:
            raise LLMError("多模态返回空内容")
        return out

    @property
    def name(self) -> str:
        return f"openai:{self._model}"


def parse_json_response(text: str) -> Any:
    """从模型输出中解析首个 JSON 对象。

    - 剥离可能的 ```json ... ``` 围栏与前后夹带的自然语言说明。
    - 解析失败抛 LLMError（供上层重试逻辑捕获）。
    """
    if not text:
        raise LLMError("空响应，无法解析 JSON")
    m = _JSON_OBJECT_RE.search(text.strip())
    if not m:
        raise LLMError("响应中未找到 JSON 对象")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON 解析失败: {exc}") from exc


def complete_json(provider: LLMProvider, user: str, *,
                  system: str | None = None, retries: int = 1) -> Any:
    """请求 LLM 输出严格 JSON 并解析。

    失败（网络/校验/解析）自动重试 ``retries`` 次后仍失败则抛 LLMError，
    由上层决定是否回退到原始块。
    """
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            text = provider.complete(user, system=system, temperature=0.0)
            return parse_json_response(text)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt >= retries:
                break
    raise LLMError(f"LLM JSON 调用重试 {retries} 次仍失败: {last_err}")


def make_vision_call(provider: LLMProvider, *, retries: int = 1):
    """构造多模态调用适配器：``call(system, user, images) -> str``。

    与 organize 层的 llm_call 适配器同构：让上层（vision.py）只依赖
    本项目定义的协议，不直接 touch openai SDK，便于测试注入 FakeProvider。
    单页失败自动重试 ``retries`` 次，仍失败则抛 LLMError（上层按页跳过）。
    """
    def _call(system: str, user: str, images: list[bytes]) -> str:
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return provider.complete_vision(
                    user, images, system=system, temperature=0.0
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= retries:
                    break
        raise LLMError(f"多模态调用重试 {retries} 次仍失败: {last_err}")

    return _call
