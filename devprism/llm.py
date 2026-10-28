"""OpenAI 兼容接口的最小客户端（纯标准库）。

刻意不引入 openai SDK：Agent 循环要自己写，模型调用层也没必要被框架绑住。
只要服务端兼容 /chat/completions，就能用——DeepSeek、通义、智谱、Ollama 都行。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .net import build_opener


class LLMError(RuntimeError):
    """模型调用失败。错误信息要能直接指导用户排查。"""


def mask_secret(secret: str | None) -> str:
    if not secret:
        return "未配置"
    if len(secret) <= 8:
        return "已配置（****）"
    return f"已配置（****{secret[-4:]}）"


@dataclass
class LLMClient:
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0
    proxy: str | None = None
    _opener: Any = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self._opener = build_opener(self.proxy)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        if max_tokens:
            payload["max_tokens"] = max_tokens

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code == 401:
                raise LLMError(f"鉴权失败（401）：API Key 无效或已过期。{detail}") from exc
            if exc.code == 404:
                raise LLMError(
                    f"接口不存在（404）：请检查 LLM_BASE_URL 是否包含 /v1。{detail}"
                ) from exc
            raise LLMError(f"模型接口返回 {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"无法连接模型接口（{self.base_url}）：{exc.reason}") from exc

    def ping(self) -> str:
        """最小连通性测试，消耗极少 token。"""
        data = self.chat([{"role": "user", "content": "只回复 pong"}], max_tokens=8)
        choices = data.get("choices") or [{}]
        return (choices[0].get("message") or {}).get("content", "").strip()
