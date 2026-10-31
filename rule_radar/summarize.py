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


def _included(change: dict[str, Any]) -> bool:
    """Changes the brief may talk about: not cosmetic, not a rule for someone else."""
    return change["materiality"] != "wording" and (change.get("rank") or {}).get("label") != "not_your_case"


def _field_changes(fields: list[dict[str, Any]], changes: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Field name -> ids of the changed clauses its Extract spans fall in."""
    out: dict[str, list[str]] = {}
    for f in fields:
        if f["status"] not in ("changed", "added", "removed"):
            continue
        ids = []
        for c in changes:
            for side in ("old", "new"):
                cl, read = c.get(side), f.get(side)
                if not cl or not read:
                    continue
                if any(cl["start"] <= a and b <= cl["end"] for a, b in read.get("spans") or []):
                    ids.append(c["id"])
                    break
        out[f["name"]] = ids
    return out


def _value(v: Any) -> Any:
    v = ", ".join(map(str, v)) if isinstance(v, list) else v
    return v.rstrip(" .") if isinstance(v, str) else v


def _usable_fields(fields: list[dict[str, Any]], changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Changed fields that sit inside a change the brief may talk about (so Rerank's
    'not your case' and Compare's 'wording only' apply to Extract facts too)."""
    ok = {c["id"] for c in changes if _included(c)}
    where = _field_changes(fields, changes)
    return [{**f, "change_ids": [i for i in where[f["name"]] if i in ok]}
            for f in fields if f["name"] in where and any(i in ok for i in where[f["name"]])]


def _facts(changes: list[dict[str, Any]], fields: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [{"id": c["id"], "section": c["section"], "change": c["status"],
             "priority": (c.get("rank") or {}).get("label") or "unranked",
             "before": (c.get("old") or {}).get("text"), "now": (c.get("new") or {}).get("text")}
            for c in changes if _included(c)]
    table = [{"field": f["label"], "status": f["status"], "before": _value((f.get("old") or {}).get("value")),
              "now": _value((f.get("new") or {}).get("value"))} for f in _usable_fields(fields, changes)]
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
    return [ln for ln in lines if isinstance(ln, dict)]


def _clause_line(c: dict[str, Any]) -> str:
    sec = c["section"] or "Rules"
    if c["status"] == "edited":
        return f"{sec}: now \u201c{c['new']['text']}\u201d (was \u201c{c['old']['text']}\u201d)"
    if c["status"] == "added":
        return f"New rule in {sec}: {c['new']['text']}"
    return f"Removed from {sec}: {c['old']['text']}"


def offline_brief(changes: list[dict[str, Any]], fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Template brief used with no model, or when the model fails: one line per change in ranked
    order, using the Extract field wording when a changed field sits in that change."""
    usable = _usable_fields(fields, changes)
    lines: list[dict[str, Any]] = []
    used: set[str] = set()
    for c in changes:
        if len(lines) >= MAX_LINES:
            break
        if not _included(c):
            continue
        mine = [f for f in usable if c["id"] in f["change_ids"] and f["name"] not in used]
        for f in mine:
            used.add(f["name"])
            o, n = _value((f.get("old") or {}).get("value")), _value((f.get("new") or {}).get("value"))
            text = (f"{f['label']} changed from {o} to {n}." if f["status"] == "changed" else
                    f"{f['label']} is now stated: {n}." if f["status"] == "added" else
                    f"{f['label']} is no longer stated (was {o}).")
            lines.append({"text": text, "change_ids": f["change_ids"]})
        if not mine and not any(c["id"] in f["change_ids"] for f in usable):
            lines.append({"text": _clause_line(c), "change_ids": [c["id"]]})
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
        if not isinstance(item.get("text"), str) or not item["text"].strip():
            continue
        raw_ids = item.get("change_ids")
        ids = [i for i in raw_ids if isinstance(i, str) and i in known] if isinstance(raw_ids, list) else []
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
        idx = item.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or idx not in valid:
            continue
        t = item.get("text")
        fixes[idx] = t.strip() if isinstance(t, str) and t.strip() else None
    return fixes
