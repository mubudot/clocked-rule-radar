"""Minimal Clocked HTTP client (stdlib only).

Follows the documented contract (https://clocked.rezolve.ai/docs/errors):
- one Idempotency-Key per logical request, reused unchanged on every retry;
- bounded exponential backoff with jitter for 429, 5xx and network errors;
- no retry for 400/401/402/403/413/422, and none once ``retryAsNew`` is returned;
- per-call usage (credits, USD cost, model ms) read from the response headers.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

DEFAULT_BASE_URL = "https://clocked.rezolve.ai/clocked"
RETRYABLE = {429, 500, 502, 503, 504}


class ClockedError(Exception):
    """A Clocked request that did not produce a result."""

    def __init__(self, message: str, *, status: int | None = None, endpoint: str = "",
                 retry_as_new: bool = False, request_id: str = ""):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.retry_as_new = retry_as_new
        self.request_id = request_id

    @property
    def kind(self) -> str:
        """Short category used by the UI to explain what went wrong."""
        s = self.status
        if s is None:
            return "network"
        if s in (401, 403):
            return "auth"
        if s == 402:
            return "credits"
        if s in (400, 413, 422):
            return "invalid_request"
        if s == 429:
            return "busy"
        if s == 409:
            return "conflict"
        return "service"


@dataclass
class Usage:
    endpoint: str
    credits: float = 0.0
    cost_usd: float = 0.0
    model_ms: float = 0.0
    attempts: int = 1
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "credits": self.credits, "cost_usd": self.cost_usd,
                "model_ms": round(self.model_ms, 1), "attempts": self.attempts,
                "replayed": self.replayed}


@dataclass
class RawResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> RawResponse: ...


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> RawResponse:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return RawResponse(resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as e:  # non-2xx still carries a JSON error body
        return RawResponse(e.code, e.read() or b"", {k.lower(): v for k, v in (e.headers or {}).items()})


def _num(v: str | None) -> float:
    try:
        return float(v) if v is not None else 0.0
    except ValueError:
        return 0.0


class ClockedClient:
    """Calls ``POST {base_url}/{endpoint}`` and returns ``(json, Usage)``."""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 120.0,
                 max_attempts: int = 4, backoff: float = 1.0,
                 transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep,
                 new_id: Callable[[], str] = lambda: str(uuid.uuid4())):
        if not api_key:
            raise ValueError("CLOCKED_API_KEY is not set (or use CLOCKED_MODE=replay)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff = backoff
        self.transport = transport
        self.sleep = sleep
        self.new_id = new_id
        self.usage_log: list[Usage] = []

    def call(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_id = self.new_id()  # one logical request -> one key, reused on retries
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": request_id,
            "User-Agent": "clocked-rule-radar/0.1",
        }
        url = f"{self.base_url}/{endpoint}"
        last: ClockedError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = self.transport(url, body, headers, self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = ClockedError(f"network error: {e}", endpoint=endpoint, request_id=request_id)
            else:
                if 200 <= raw.status < 300:
                    try:
                        data = json.loads(raw.body.decode("utf-8"))
                    except ValueError as e:
                        raise ClockedError(f"invalid JSON from {endpoint}: {e}", status=raw.status,
                                           endpoint=endpoint, request_id=request_id) from e
                    h = raw.headers
                    usage = Usage(endpoint, _num(h.get("x-clocked-credits")),
                                  _num(h.get("x-clocked-cost-usd")), _num(h.get("x-clocked-model-ms")),
                                  attempt, replayed=h.get("x-radar-replay") == "1")
                    self.usage_log.append(usage)
                    return data, usage
                msg, retry_as_new = _error_detail(raw)
                last = ClockedError(f"{endpoint} returned HTTP {raw.status}: {msg}", status=raw.status,
                                    endpoint=endpoint, retry_as_new=retry_as_new, request_id=request_id)
                if retry_as_new or raw.status not in RETRYABLE:
                    raise last
            if attempt < self.max_attempts:
                delay = self.backoff * (2 ** (attempt - 1))
                self.sleep(delay + random.uniform(0, delay / 2))
        assert last is not None
        raise last


def _error_detail(raw: RawResponse) -> tuple[str, bool]:
    try:
        err = json.loads(raw.body.decode("utf-8") or "{}")
    except ValueError:
        return raw.body[:200].decode("utf-8", "replace"), False
    if not isinstance(err, dict):
        return str(err)[:200], False
    msg = err.get("error") or err.get("detail") or err.get("message") or "error"
    if not isinstance(msg, str):
        msg = json.dumps(msg)[:300]
    return msg, bool(err.get("retryAsNew"))
