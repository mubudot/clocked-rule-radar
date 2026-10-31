import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rule_radar.clocked import ClockedError, Usage  # noqa: E402

SAMPLES = ROOT / "samples"
REPLAY = ROOT / "fixtures" / "replay"


@pytest.fixture
def v1() -> str:
    return (SAMPLES / "northwind_v1.md").read_text("utf-8")


@pytest.fixture
def v2() -> str:
    return (SAMPLES / "northwind_v2.md").read_text("utf-8")


@pytest.fixture
def profile() -> dict:
    return json.loads((SAMPLES / "profile_solo_taiwan.json").read_text("utf-8"))


class FakeClient:
    """Answers per endpoint from a handler; a handler may raise ClockedError."""

    def __init__(self, handlers=None, fallback=None):
        self.handlers = handlers or {}
        self.fallback = fallback
        self.calls = []

    def call(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        h = self.handlers.get(endpoint)
        if h is None:
            if self.fallback is None:
                raise ClockedError(f"{endpoint} not stubbed", status=503, endpoint=endpoint)
            return self.fallback.call(endpoint, payload)
        out = h(payload) if callable(h) else h
        if isinstance(out, Exception):
            raise out
        return out, Usage(endpoint, credits=1.0)


class FakeLLM:
    name = "fake:model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append((system, user))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
