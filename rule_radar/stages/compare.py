"""Compare stage: decide which local edits are real rule changes.

A local text diff finds every edit but cannot tell a reworded sentence from a
moved deadline. Clocked Compare reads both versions and returns the passages that
conflict and the passages that say the same thing, with offsets in each version.
We map those passages onto our clauses and give each change a materiality:

- ``material``    an edit (or addition) that Compare says conflicts with the old rules
- ``wording``     an edit Compare says restates the old clause (cosmetic, hidden by default)
- ``unconfirmed`` an edit Compare did not comment on -> shown, marked "read it yourself"
- ``new`` / ``removed``  clauses with no counterpart and no conflict
- ``moved``       an added or removed clause whose content Compare found on the other side
"""
from __future__ import annotations

from typing import Any

from ..clauses import Change, Clause

INSTRUCTIONS = (
    "Document 'old' is the previous version of a contest's official rules and 'new' is the current "
    "version. Treat a changed date, deadline, amount, limit, eligibility condition, requirement or "
    "payment term as a conflict. A changed version label or publication date in the title is not a "
    "rule change. Rewording that keeps the same meaning is not a conflict."
)


def build_payload(old_text: str, new_text: str) -> dict[str, Any]:
    return {"documents": [{"id": "old", "text": old_text}, {"id": "new", "text": new_text}],
            "evidence": "all", "instructions": INSTRUCTIONS}


def _touching(clauses: list[Clause], passage: dict | None) -> list[Clause]:
    if not passage:
        return []
    s, e = passage.get("start"), passage.get("end")
    if not isinstance(s, int) or not isinstance(e, int) or e <= s:
        return []
    hits = []
    for c in clauses:
        ov = c.overlaps(s, e)
        # count a clause when the passage covers a real part of it (not a 1-char brush)
        if ov >= min(12, len(c.text)) or ov >= 0.5 * len(c.text):
            hits.append(c)
    return hits


def _findings(resp: dict[str, Any]) -> list[tuple[str, dict]]:
    out = []
    for pair in resp.get("pairs") or []:
        swap = pair.get("a") == "new" and pair.get("b") == "old"
        for bucket, rel in (("conflicts", "conflict"), ("duplicates", "restated")):
            for f in pair.get(bucket) or []:
                a, b = f.get("a"), f.get("b")
                if swap:
                    a, b = b, a
                out.append((rel, {"kind": f.get("kind"), "confidence": f.get("confidence"),
                                  "old": a, "new": b}))
    return out


def interpret(resp: dict[str, Any], changes: list[Change], old: list[Clause],
              new: list[Clause]) -> list[dict[str, Any]]:
    """Annotate every non-unchanged change with Compare's view of it."""
    by_old: dict[str, list[tuple[str, dict, list[str]]]] = {}
    by_new: dict[str, list[tuple[str, dict, list[str]]]] = {}
    for rel, f in _findings(resp):
        o_hits, n_hits = _touching(old, f["old"]), _touching(new, f["new"])
        for c in o_hits:
            by_old.setdefault(c.id, []).append((rel, f, [x.id for x in n_hits]))
        for c in n_hits:
            by_new.setdefault(c.id, []).append((rel, f, [x.id for x in o_hits]))

    out = []
    for ch in changes:
        if ch.status == "unchanged":
            continue
        notes = (by_old.get(ch.old.id, []) if ch.old else []) + (by_new.get(ch.new.id, []) if ch.new else [])
        conflicts = [n for n in notes if n[0] == "conflict"]
        restated = [n for n in notes if n[0] == "restated"]
        if ch.status == "edited":
            materiality = "material" if conflicts else ("wording" if restated else "unconfirmed")
        elif conflicts:
            materiality = "material"
        elif restated:
            materiality = "moved"
        else:
            materiality = "new" if ch.status == "added" else "removed"
        best = max(conflicts or restated, key=lambda n: n[1].get("confidence") or 0, default=None)
        related = sorted({cid for n in conflicts for cid in n[2]} -
                         {c.id for c in (ch.old, ch.new) if c})
        out.append({
            "id": ch.id, "status": ch.status, "section": ch.section,
            "similarity": round(ch.similarity, 3), "materiality": materiality,
            "old": ch.old.as_dict() if ch.old else None,
            "new": ch.new.as_dict() if ch.new else None,
            "compare": None if best is None else {
                "relation": best[0], "kind": best[1].get("kind"),
                "confidence": best[1].get("confidence"),
                "old_quote": (best[1].get("old") or {}).get("quote"),
                "new_quote": (best[1].get("new") or {}).get("quote"),
            },
            "conflicts_with": related,
        })
    return out


def local_only(changes: list[Change]) -> list[dict[str, Any]]:
    """Fallback when Compare is unavailable: every edit is unconfirmed."""
    return interpret({}, changes, [], [])
