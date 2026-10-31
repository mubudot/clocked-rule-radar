"""Write the plain-language change brief, and repair lines Verify rejected.

The model only sees facts the pipeline already found (ranked changes and the
field table). Its output is a list of one-fact lines, each pointing at the change
ids it describes, so every line can be verified and linked back to the source.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .llm.base import LLM, LLMError

MAX_LINES = 8
SYSTEM = (
    "You write short, factual briefs about what changed between two versions of a contest's official "
    "rules, for an entrant in a hurry. Use only the facts you are given. Never guess a value. "
    "Each line states one change, in at most 30 words, and names both the old and the new value when "
    "there is one. Lead with what the entrant must act on. Reply with JSON only, in this shape: "
    '{"lines": [{"text": "...", "change_ids": ["new:2.1"]}]}'
)
REPAIR_SYSTEM = (
    "A verifier checked lines of a rules-change brief against the two versions of the rules and rejected "
    "some of them. Rewrite each rejected line so that it is fully supported by the evidence, or drop it "
    "if it cannot be. Reply with JSON only: "
    '{"lines": [{"index": 0, "text": "..."}]} - use "text": null to drop a line.'
)


def _facts(changes: list[dict[str, Any]], fields: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for c in changes:
        if c["materiality"] == "wording":
            continue
        label = (c.get("rank") or {}).get("label")
        if label == "not_your_case":
            continue
        rows.append({"id": c["id"], "section": c["section"], "change": c["status"],
                     "priority": label or "unranked",
                     "before": (c.get("old") or {}).get("text"), "now": (c.get("new") or {}).get("text")})
    table = [{"field": f["label"], "status": f["status"],
              "before": (f.get("old") or {}).get("value"), "now": (f.get("new") or {}).get("value")}
             for f in fields if f["status"] in ("changed", "added", "removed")]
    return {"changes": rows[:24], "key_facts": table}


def _parse_lines(text: str) -> list[dict[str, Any]]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("model reply has no JSON object")
    try:
        data = json.loads(m.group(0))
    except ValueError:
        raise LLMError("model reply is not valid JSON") from None
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list):
        raise LLMError("model reply has no 'lines' list")
    return lines


def offline_brief(changes: list[dict[str, Any]], fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Template brief used with no model, or when the model fails."""
    lines: list[dict[str, Any]] = []
    for f in fields:
        if f["status"] != "changed":
            continue
        o, n = f["old"]["value"], f["new"]["value"]
        o = ", ".join(o) if isinstance(o, list) else o
        n = ", ".join(n) if isinstance(n, list) else n
        lines.append({"text": f"{f['label']} changed from {o} to {n}.", "change_ids": []})
    for c in changes:
        if len(lines) >= MAX_LINES:
            break
        if c["materiality"] == "wording" or (c.get("rank") or {}).get("label") in ("not_your_case",):
            continue
        if c["status"] == "added":
            lines.append({"text": f"New rule in {c['section']}: {c['new']['text']}", "change_ids": [c["id"]]})
        elif c["status"] == "removed":
            lines.append({"text": f"Removed from {c['section']}: {c['old']['text']}", "change_ids": [c["id"]]})
    return lines[:MAX_LINES]


def write_brief(changes: list[dict[str, Any]], fields: list[dict[str, Any]],
                llm: LLM | None) -> tuple[list[dict[str, Any]], str]:
    """Returns (lines, source) where source is the model name or 'template'."""
    if llm is None:
        return offline_brief(changes, fields), "template"
    reply = llm.complete(SYSTEM, json.dumps(_facts(changes, fields), ensure_ascii=False, indent=1))
    known = {c["id"] for c in changes}
    out = []
    for item in _parse_lines(reply)[:MAX_LINES]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            continue
        ids = [i for i in item.get("change_ids") or [] if isinstance(i, str) and i in known]
        out.append({"text": item["text"].strip(), "change_ids": ids})
    if not out:
        raise LLMError("model returned no usable lines")
    return out, llm.name


def repair(rejected: list[dict[str, Any]], llm: LLM) -> dict[int, str | None]:
    """rejected: [{index, text, verdict, evidence}] -> {index: new text or None (drop)}."""
    reply = llm.complete(REPAIR_SYSTEM, json.dumps({"rejected": rejected}, ensure_ascii=False, indent=1))
    valid = {r["index"] for r in rejected}
    fixes: dict[int, str | None] = {}
    for item in _parse_lines(reply):
        if not isinstance(item, dict) or item.get("index") not in valid:
            continue
        t = item.get("text")
        fixes[item["index"]] = t.strip() if isinstance(t, str) and t.strip() else None
    return fixes
