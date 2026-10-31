"""Settings from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clocked import ClockedClient, RecordingClient, ReplayClient
from .llm.anthropic_messages import AnthropicLLM
from .llm.anthropic_messages import DEFAULT_BASE_URL as ANTHROPIC_URL
from .llm.base import LLM
from .llm.openai_compat import DEFAULT_BASE_URL as OPENAI_URL
from .llm.openai_compat import OpenAICompatLLM
from .llm.recorded import RecordingLLM, ReplayLLM

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPLAY_DIR = ROOT / "fixtures" / "replay"


def load_dotenv(path: str | Path = ".env") -> None:
    """Tiny .env reader: KEY=VALUE lines; existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


@dataclass
class Settings:
    mode: str                 # live | replay | record
    api_key: str
    base_url: str
    replay_dir: Path
    llm_provider: str         # offline | openai | anthropic
    llm_model: str
    llm_base_url: str
    llm_api_key: str
    llm_auth: str
    llm_temperature: float | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        key = e.get("CLOCKED_API_KEY", "")
        mode = (e.get("CLOCKED_MODE") or ("live" if key else "replay")).lower()
        if mode not in ("live", "replay", "record"):
            raise ValueError("CLOCKED_MODE must be live, replay or record")
        provider = (e.get("RADAR_LLM_PROVIDER") or "offline").lower()
        if provider not in ("offline", "openai", "anthropic"):
            raise ValueError("RADAR_LLM_PROVIDER must be offline, openai or anthropic")
        default_url = OPENAI_URL if provider == "openai" else ANTHROPIC_URL
        llm_key = e.get("RADAR_LLM_API_KEY") or (
            e.get("OPENAI_API_KEY", "") if provider == "openai" else
            e.get("ANTHROPIC_API_KEY", "") if provider == "anthropic" else "")
        return cls(mode=mode, api_key=key,
                   base_url=e.get("CLOCKED_BASE_URL", "https://clocked.rezolve.ai/clocked"),
                   replay_dir=Path(e.get("RADAR_REPLAY_DIR") or DEFAULT_REPLAY_DIR),
                   llm_provider=provider, llm_model=e.get("RADAR_LLM_MODEL", ""),
                   llm_base_url=e.get("RADAR_LLM_BASE_URL") or default_url,
                   llm_api_key=llm_key, llm_auth=(e.get("RADAR_LLM_AUTH") or "x-api-key").lower(),
                   llm_temperature=float(e["RADAR_LLM_TEMPERATURE"]) if e.get("RADAR_LLM_TEMPERATURE") else None)

    def clocked(self):
        if self.mode == "replay":
            return ReplayClient(self.replay_dir)
        live = ClockedClient(self.api_key, base_url=self.base_url)
        return RecordingClient(live, self.replay_dir) if self.mode == "record" else live

    def model(self) -> LLM | None:
        inner: LLM | None
        if self.llm_provider == "openai":
            inner = OpenAICompatLLM(self.llm_model, self.llm_api_key, self.llm_base_url,
                                    temperature=self.llm_temperature)
        elif self.llm_provider == "anthropic":
            inner = AnthropicLLM(self.llm_model, self.llm_api_key, self.llm_base_url, self.llm_auth,
                                 temperature=self.llm_temperature)
        else:
            inner = None
        if self.mode == "replay":
            return ReplayLLM(self.replay_dir, fallback=inner)
        if self.mode == "record" and inner is not None:
            return RecordingLLM(inner, self.replay_dir)
        return inner

    def public(self) -> dict[str, Any]:
        """Safe to show in the UI: never includes a key."""
        return {"mode": self.mode, "llm_provider": self.llm_provider, "llm_model": self.llm_model or None,
                "clocked_key_set": bool(self.api_key)}
