"""Watch a rules page: save a text snapshot, and analyze it against the previous one.

Run it from cron or CI. Exit codes: 0 = no change, 3 = material change found,
2 = fetch or input error. Snapshots are plain text files you can read and diff.
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .pipeline import analyze
from .report_text import render

BLOCK = {"p", "div", "li", "tr", "br", "section", "article", "table", "ul", "ol"}
HEAD = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP:
            self.skip += 1
        elif tag in HEAD:
            self.parts.append(f"\n\n{HEAD[tag]} ")
        elif tag in BLOCK:
            self.parts.append("\n\n" if tag in ("p", "section", "article", "table", "ul", "ol") else "\n")
        if tag == "li" and not self.skip:
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP and self.skip:
            self.skip -= 1
        elif tag in HEAD:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def fetch_text(url: str, timeout: float = 30.0) -> str:
    if not url.startswith(("https://", "http://")):
        raise ValueError("watch needs an http(s) URL")
    req = urllib.request.Request(url, headers={"User-Agent": "clocked-rule-radar/0.1 (+rules watcher)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    return html_to_text(raw) if "<" in raw[:2000] else raw


def store_dir(root: Path, url: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower().split("//", 1)[-1]).strip("-")[:60]
    return root / f"{slug}-{hashlib.sha256(url.encode()).hexdigest()[:8]}"


def watch(url: str, root: Path, *, client: Any, llm: Any = None, profile: dict | None = None,
          fetch=fetch_text, now=time.time) -> int:
    try:
        text = fetch(url)
    except Exception as e:  # noqa: BLE001 - report any fetch failure plainly
        print(f"error: could not fetch {url}: {e}", file=sys.stderr)
        return 2
    d = store_dir(root, url)
    d.mkdir(parents=True, exist_ok=True)
    snaps = sorted(d.glob("*.txt"))
    if snaps and snaps[-1].read_text("utf-8") == text:
        print(f"no change since {snaps[-1].stem}")
        return 0
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now()))
    (d / f"{stamp}.txt").write_text(text, "utf-8")
    if not snaps:
        print(f"first snapshot saved ({len(text):,} characters); run again later to compare")
        return 0
    rep = analyze(snaps[-1].read_text("utf-8"), text, client=client, llm=llm, profile=profile)
    print(render(rep))
    return 3 if rep["counts"].get("material") else 0
