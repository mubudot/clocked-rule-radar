"""Anthropic Messages API backend (Claude or a Messages-compatible gateway)."""
from __future__ import annotations

from typing import Any

from .base import HttpPost, LLMError, post_json, urllib_post

DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicLLM:
    def __init__(self, model: str, api_key: str = "", base_url: str = DEFAULT_BASE_URL,
                 auth: str = "x-api-key", *, timeout: float = 90.0, max_tokens: int = 1200,
                 temperature: float | None = None, post: HttpPost = urllib_post):
        if not model:
            raise ValueError("RADAR_LLM_MODEL is required for the anthropic provider")
        if auth not in ("x-api-key", "bearer"):
            raise ValueError("RADAR_LLM_AUTH must be x-api-key or bearer")
        self.model, self.api_key, self.auth = model, api_key, auth
        self.base_url = base_url.rstrip("/")
        self.timeout, self.max_tokens, self.temperature, self.post = timeout, max_tokens, temperature, post
        self.name = f"anthropic:{model}"

    def complete(self, system: str, user: str) -> str:
        headers = {"anthropic-version": "2023-06-01"}
        if self.api_key:
            if self.auth == "bearer":
                headers["authorization"] = f"Bearer {self.api_key}"
            else:
                headers["x-api-key"] = self.api_key
        payload: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                                   "messages": [{"role": "user", "content": user}]}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        data = post_json(self.post, f"{self.base_url}/v1/messages", payload, headers, self.timeout,
                         self.api_key)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise LLMError("messages response has no content list")
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if not text.strip():
            raise LLMError("messages response has no text")
        return text
