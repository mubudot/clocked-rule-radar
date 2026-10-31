"""Rerank stage: put the changes that matter to *this* entrant first.

Each change is sent as a passage ("Before: ... Now: ..."), with the entrant's
situation as ``context``. Clocked Rerank reads every passage on its own and returns
an action:

- ``use``  -> "Act on this": it changes what this entrant must do
- ``expand`` -> "Read the full section": the answer is elsewhere in that section
- ``skip`` + ``different_case`` -> "Not your case" (e.g. a rule for another country)
- ``skip`` + ``unrelated`` -> "Low impact"

One Decide question rides along (``questions``) and answers, over the whole set,
whether the entrant needs to change anything before the deadline.
"""
from __future__ import annotations

from typing import Any

DEFAULT_QUERY = ("Which rule changes could make me miss the deadline, become ineligible, lose prize money, "
                 "or force me to change what I submit?")
INSTRUCTIONS = ("Rank rule changes by how much they require this entrant to act. A change that applies "
                "only to people in other places or situations than the entrant's is a different case.")
ACTION_QUESTION = {
    "action_needed": {
        "type": "noul",
        "instructions": "Does at least one of these changes require this entrant to change their plan, "
                        "their project or their submission before the deadline?",
        "threshold": 0.7,
    }
}
LABELS = {("use", None): "act", ("expand", None): "read_section",
          ("skip", "different_case"): "not_your_case", ("skip", "unrelated"): "low_impact",
          ("skip", "already_given"): "low_impact"}
RANKABLE = {"material", "unconfirmed", "new", "removed", "moved"}


MAX_DOCS = 256          # per request
MAX_PASSAGE = 20_000    # characters per document
SIDE = 9_900            # each side of a Before/Now passage when the pair is too long


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[: limit - 1] + "\u2026", True)


def passage(change: dict[str, Any]) -> tuple[str, bool]:
    """Passage text for one change, and whether it had to be shortened to fit."""
    old, new = change.get("old"), change.get("new")
    if change["status"] == "edited":
        full = f"Changed rule. Before: {old['text']} Now: {new['text']}"
        if len(full) <= MAX_PASSAGE:
            return full, False
        o, _ = _clip(old["text"], SIDE)   # keep both sides instead of cutting off the new text
        n, _ = _clip(new["text"], SIDE)
        return f"Changed rule. Before: {o} Now: {n}", True
    if change["status"] == "added":
        return _clip(f"New rule: {new['text']}", MAX_PASSAGE)
    return _clip(f"Removed rule: {old['text']}", MAX_PASSAGE)


def build_payloads(changes: list[dict[str, Any]], profile: dict[str, Any] | None,
                   query: str | None = None) -> tuple[list[dict[str, Any]], set[str]]:
    """One payload per 256 changes (every change is ranked), plus the ids of shortened passages.
    The yes/no action question is asked only when everything fits in one request, because an
    answer over part of the changes would be a partial answer."""
    docs, shortened = [], set()
    for c in changes:
        if c["materiality"] not in RANKABLE:
            continue
        text, cut = passage(c)
        if cut:
            shortened.add(c["id"])
        docs.append({"id": c["id"], "title": c["section"][:500] or "Rules", "text": text,
                     "metadata": {"change": c["status"], "materiality": c["materiality"]}})
    ctx = {k: v for k, v in (profile or {}).items() if v not in (None, "", [])}
    batches = [docs[i:i + MAX_DOCS] for i in range(0, len(docs), MAX_DOCS)]
    payloads = []
    for batch in batches:
        p: dict[str, Any] = {"query": (query or DEFAULT_QUERY)[:4000], "documents": batch,
                             "instructions": INSTRUCTIONS}
        if len(batches) == 1:
            p["questions"] = ACTION_QUESTION
        if ctx:
            p["context"] = ctx
        payloads.append(p)
    return payloads, shortened


def build_payload(changes: list[dict[str, Any]], profile: dict[str, Any] | None,
                  query: str | None = None) -> dict[str, Any] | None:
    payloads, _ = build_payloads(changes, profile, query)
    return payloads[0] if payloads else None


def interpret(resp: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    ranks: dict[str, dict[str, Any]] = {}
    for pos, r in enumerate(r for r in resp.get("results") or [] if isinstance(r, dict)):
        rid = r.get("id")
        if not rid:
            continue
        action, reason = r.get("action"), r.get("reason")
        label = LABELS.get((action, reason if action == "skip" else None), "low_impact")
        ranks[rid] = {"position": pos, "action": action, "reason": reason, "label": label,
                      "relevance": r.get("relevance_score"), "document_score": r.get("document_score"),
                      "levels": r.get("levels")}
    ans = (resp.get("answers") or {}).get("action_needed")
    verdict = None
    if isinstance(ans, dict):
        verdict = {"probability": ans.get("noul"), "decision": ans.get("decision")}
    return ranks, verdict


def merge(parts: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Combine ranks from several batches: positions follow relevance across all of them."""
    allr = {k: v for p in parts for k, v in p.items()}
    if len(parts) > 1:
        order = sorted(allr, key=lambda k: -(allr[k].get("relevance") or 0))
        for pos, k in enumerate(order):
            allr[k]["position"] = pos
    return allr


LOCAL_ORDER = {"material": 0, "new": 1, "removed": 2, "unconfirmed": 3, "moved": 4, "wording": 5}


def order(changes: list[dict[str, Any]], ranks: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Ranked changes first in Rerank order; anything unranked keeps a local order after them."""
    ranks = ranks or {}
    for c in changes:
        c["rank"] = ranks.get(c["id"])
    label_order = {"act": 0, "read_section": 1, "low_impact": 2, "not_your_case": 3}
    return sorted(changes, key=lambda c: (
        0 if c["rank"] else 1,
        label_order.get(c["rank"]["label"], 9) if c["rank"] else LOCAL_ORDER.get(c["materiality"], 9),
        c["rank"]["position"] if c["rank"] else 0))
