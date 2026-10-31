"""Record / replay model output so the bundled demo is reproducible offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .base import LLM, LLMError


def prompt_key(system: str, user: str) -> str:
    return "llm-" + hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()[:20]


class RecordingLLM:
    def __init__(self, inner: LLM, directory: str | Path):
        self.inner, self.dir = inner, Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.name = inner.name

    def complete(self, system: str, user: str) -> str:
        path = self.dir / f"{prompt_key(system, user)}.json"
        if path.exists():  # keep a recorded run self-consistent (see RecordingClient)
            return json.loads(path.read_text("utf-8"))["text"]
        text = self.inner.complete(system, user)
        path.write_text(
            json.dumps({"model": self.inner.name, "text": text}, ensure_ascii=False, indent=1), "utf-8")
        return text


class ReplayLLM:
    """Returns recorded text for a known prompt; otherwise defers to a fallback."""

    def __init__(self, directory: str | Path, fallback: LLM | None = None):
        self.dir, self.fallback = Path(directory), fallback
        self.name = "replay"

    def complete(self, system: str, user: str) -> str:
        p = self.dir / f"{prompt_key(system, user)}.json"
        if p.exists():
            rec = json.loads(p.read_text("utf-8"))
            self.name = f"replay:{rec.get('model', '?')}"
            return rec["text"]
        if self.fallback is None:
            raise LLMError("no recorded model output for this input")
        self.name = self.fallback.name
        return self.fallback.complete(system, user)
