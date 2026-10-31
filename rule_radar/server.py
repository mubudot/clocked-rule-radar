"""Local web app (stdlib only): static UI + a small JSON API.

GET  /                     the app
GET  /api/config           mode and model in use (never a key)
GET  /api/samples          bundled sample pairs
GET  /api/samples/<name>   one sample: {old, new, profile, claims}
POST /api/analyze          {old, new, profile?, query?} -> full report
POST /api/check            {old, new, claims: [...]}   -> Verify result per claim
"""
from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .clocked import ClockedError
from .config import ROOT, Settings
from .pipeline import InputError, analyze, check_claims

WEB = Path(__file__).resolve().parent / "web"
SAMPLES = ROOT / "samples"
MAX_BODY = 1_000_000
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def list_samples() -> list[dict[str, str]]:
    out = []
    for meta in sorted(SAMPLES.glob("*.sample.json")):
        info = json.loads(meta.read_text("utf-8"))
        out.append({"name": meta.name.removesuffix(".sample.json"), "title": info.get("title", meta.stem)})
    return out


def load_sample(name: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[a-z0-9_-]{1,60}", name):
        return None
    meta = SAMPLES / f"{name}.sample.json"
    if not meta.is_file():
        return None
    info = json.loads(meta.read_text("utf-8"))
    return {"title": info.get("title"), "old": (SAMPLES / info["old"]).read_text("utf-8"),
            "new": (SAMPLES / info["new"]).read_text("utf-8"), "profile": info.get("profile", {}),
            "claims": info.get("claims", [])}


class GatedClient:
    """Shares one concurrency budget across every request the server is handling."""

    def __init__(self, inner: Any, gate: threading.BoundedSemaphore):
        self.inner, self.gate = inner, gate

    def call(self, endpoint: str, payload: dict[str, Any]):
        with self.gate:
            return self.inner.call(endpoint, payload)


class App:
    """Holds settings and builds a fresh client per request (clients keep a usage log)."""

    def __init__(self, settings: Settings, max_concurrent: int = 4):
        self.settings = settings
        # Clocked accounts with the hackathon code allow 4 requests at a time: one budget for all scans
        self.gate = threading.BoundedSemaphore(max_concurrent)

    def client(self) -> GatedClient:
        return GatedClient(self.settings.clocked(), self.gate)

    def analyze(self, body: dict[str, Any]) -> dict[str, Any]:
        return analyze(_text(body, "old"), _text(body, "new"), client=self.client(),
                       llm=self.settings.model(), profile=_profile(body.get("profile")),
                       query=body.get("query") if isinstance(body.get("query"), str) else None)

    def check(self, body: dict[str, Any]) -> dict[str, Any]:
        claims = body.get("claims")
        if not isinstance(claims, list) or not all(isinstance(c, str) for c in claims):
            raise InputError("claims must be a list of strings")
        claims = [c for c in claims if c.strip()]
        if not claims:
            raise InputError("enter at least one claim")
        return check_claims(_text(body, "old"), _text(body, "new"), claims, client=self.client())


def _text(body: dict[str, Any], key: str) -> str:
    v = body.get(key)
    if not isinstance(v, str):
        raise InputError(f"'{key}' must be text")
    return v


def _profile(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise InputError("profile must be an object")
    return {str(k)[:60]: (x if isinstance(x, (int, float)) else str(x)[:300])
            for k, x in list(v.items())[:20] if x not in (None, "")}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RuleRadar/0.1"

        def log_message(self, fmt: str, *args) -> None:  # one short line per request, no bodies
            sys.stderr.write(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/config":
                return self._json(200, app.settings.public())
            if path == "/api/samples":
                return self._json(200, list_samples())
            if path.startswith("/api/samples/"):
                s = load_sample(path.rsplit("/", 1)[-1])
                return self._json(200, s) if s else self._json(404, {"error": "no such sample"})
            name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            f = (WEB / name).resolve()
            if WEB not in f.parents or not f.is_file():
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            route = {"/api/analyze": app.analyze, "/api/check": app.check}.get(path)
            if route is None:
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_BODY:
                return self._json(413 if n > MAX_BODY else 400, {"error": "request body missing or too large"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json(415, {"error": "send application/json"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(body, dict):
                    raise InputError("send a JSON object")
                return self._json(200, route(body))
            except (InputError, ValueError) as e:
                return self._json(400, {"error": str(e)})
            except ClockedError as e:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": str(e), "kind": e.kind})
            except Exception as e:  # noqa: BLE001 - never leak a traceback to the page
                sys.stderr.write(f"internal error: {type(e).__name__}: {e}\n")
                return self._json(500, {"error": "internal error (see server log)"})

    return Handler


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(App(settings)))
    print(f"Rule Change Radar on http://{host}:{port}/  (Clocked mode: {settings.mode}, "
          f"model: {settings.llm_provider}{':' + settings.llm_model if settings.llm_model else ''})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
