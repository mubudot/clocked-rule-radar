"""Pluggable language-model backends used to write the change brief.

Two wire formats cover almost every hosted or local model:
- ``openai``    POST {base}/chat/completions  (OpenAI, Azure-compatible gateways, OpenRouter,
                vLLM, Ollama, LM Studio ...)
- ``anthropic`` POST {base}/v1/messages       (Claude, or any Messages-compatible gateway)
``offline`` writes the brief from the change list with a template, so the app works
with no model at all. Every backend returns plain text; parsing lives in summarize.py.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class LLMError(Exception):
    pass


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...


@dataclass
class HttpResult:
    status: int
    body: bytes


HttpPost = Callable[[str, bytes, dict[str, str], float], HttpResult]


def urllib_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpResult:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return HttpResult(r.status, r.read())
    except urllib.error.HTTPError as e:
        return HttpResult(e.code, e.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"model endpoint unreachable: {type(e).__name__}") from None


def post_json(post: HttpPost, url: str, payload: dict[str, Any], headers: dict[str, str],
              timeout: float, secret: str = "") -> dict[str, Any]:
    res = post(url, json.dumps(payload).encode("utf-8"), {"content-type": "application/json", "user-agent": "clocked-rule-radar/0.1", **headers},
               timeout)
    text = res.body.decode("utf-8", "replace")
    if secret:
        text = text.replace(secret, "***")
    if res.status != 200:
        raise LLMError(f"model endpoint returned HTTP {res.status}: {text[:300]}")
    try:
        data = json.loads(text)
    except ValueError:
        raise LLMError("model endpoint returned a non-JSON body") from None
    if not isinstance(data, dict):
        raise LLMError("model endpoint returned an unexpected body")
    return data
