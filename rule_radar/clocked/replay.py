"""Record real Clocked responses once, replay them offline.

The repository ships recordings of live responses for the bundled sample rules, so
anyone can run the full demo without an API key (``CLOCKED_MODE=replay``). A
recording is keyed by endpoint + a hash of the exact request payload, so a replay
can never return a response that belongs to a different input.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .client import ClockedClient, ClockedError, Usage


def payload_key(endpoint: str, payload: dict[str, Any]) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{endpoint}-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:20]


class ReplayClient:
    """Serves recorded responses. Unknown payloads raise a clear error."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.usage_log: list[Usage] = []

    def call(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        path = self.dir / f"{payload_key(endpoint, payload)}.json"
        if not path.exists():
            raise ClockedError(
                f"no recording for this {endpoint} request (replay mode only covers the bundled "
                "samples; set CLOCKED_MODE=live and CLOCKED_API_KEY for your own text)",
                status=None, endpoint=endpoint)
        rec = json.loads(path.read_text("utf-8"))
        u = rec.get("usage", {})
        usage = Usage(endpoint, u.get("credits", 0.0), u.get("cost_usd", 0.0), u.get("model_ms", 0.0),
                      1, replayed=True, ledger_replay=bool(u.get("ledger_replay")))
        self.usage_log.append(usage)
        return rec["response"], usage


class RecordingClient:
    """Record mode: replay a request that is already recorded, otherwise call the live API and save it.

    Never overwriting a recording keeps a recorded run self-consistent: every later request
    in the run was built from exactly the responses that are on disk.
    """

    def __init__(self, live: ClockedClient, directory: str | Path):
        self.live = live
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def usage_log(self) -> list[Usage]:
        return self.live.usage_log

    def call(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        path = self.dir / f"{payload_key(endpoint, payload)}.json"
        if path.exists():
            return ReplayClient(self.dir).call(endpoint, payload)
        data, usage = self.live.call(endpoint, payload)
        rec = {"endpoint": endpoint, "usage": usage.as_dict(), "response": data}
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), "utf-8")
        return data, usage
