"""OpenAI-compatible Chat Completions backend."""
from __future__ import annotations

from typing import Any

from .base import HttpPost, LLMError, post_json, urllib_post

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatLLM:
    def __init__(self, model: str, api_key: str = "", base_url: str = DEFAULT_BASE_URL, *,
                 timeout: float = 90.0, max_tokens: int = 1200, temperature: float | None = None,
                 post: HttpPost = urllib_post):
        if not model:
            raise ValueError("RADAR_LLM_MODEL is required for the openai provider")
        self.model, self.api_key = model, api_key
        self.base_url = base_url.rstrip("/")
        self.timeout, self.max_tokens, self.temperature, self.post = timeout, max_tokens, temperature, post
        self.name = f"openai:{model}"

    def complete(self, system: str, user: str) -> str:
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        data = post_json(self.post, f"{self.base_url}/chat/completions", payload, headers, self.timeout,
                         self.api_key)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("chat/completions response has no message content") from None
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            raise LLMError("chat/completions returned empty content")
        return content
