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

from difflib import SequenceMatcher

from ..clauses import Change, Clause

RESTATED_KINDS = {"verbatim", "restated", None}   # "elaborated" / "different" are not cosmetic

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
        # count a clause when the passage sits inside it (however short, e.g. "six" vs "four")
        # or covers a real part of it (a passage spanning a heading brushes the next clause by a few chars)
        if ov and (c.contains(s, e) or ov >= min(12, len(c.text)) or ov >= 0.5 * len(c.text)):
            hits.append(c)
    return hits


def _changed_ranges(old: Clause, new: Clause) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Absolute character ranges that differ between an edited old/new clause pair."""
    sm = SequenceMatcher(None, old.text, new.text, autojunk=False)
    o, n = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            o.append((old.start + i1, old.start + i2))
        if j2 > j1:
            n.append((new.start + j1, new.start + j2))
    return o, n


def _covered(ranges: list[tuple[int, int]], spans: list[tuple[int, int]]) -> bool:
    """Every changed range lies inside one restated passage."""
    return all(any(s <= a and b <= e for s, e in spans) for a, b in ranges)


def _span(p: dict | None) -> tuple[int, int] | None:
    if not p or not isinstance(p.get("start"), int) or not isinstance(p.get("end"), int):
        return None
    return p["start"], p["end"]


def _cosmetic(ch: Change, restated: list) -> bool:
    """An edit is wording-only when restated passages cover every changed part on both sides."""
    o_ranges, n_ranges = _changed_ranges(ch.old, ch.new)  # type: ignore[arg-type]
    o_spans = [sp for n in restated if (sp := _span(n[1].get("old")))]
    n_spans = [sp for n in restated if (sp := _span(n[1].get("new")))]
    return _covered(o_ranges, o_spans) and _covered(n_ranges, n_spans)


def _findings(resp: dict[str, Any]) -> list[tuple[str, dict]]:
    out = []
    for pair in resp.get("pairs") or []:
        swap = pair.get("a") == "new" and pair.get("b") == "old"
        for bucket, rel in (("conflicts", "conflict"), ("duplicates", "restated")):
            for f in pair.get(bucket) or []:
                a, b = f.get("a"), f.get("b")
                if swap:
                    a, b = b, a
                if rel == "restated" and f.get("kind") not in RESTATED_KINDS:
                    rel = "related"   # elaborated/different: same topic, not proof of same meaning
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
            if conflicts:
                materiality = "material"
            elif restated and _cosmetic(ch, restated):
                materiality = "wording"
            else:
                materiality = "unconfirmed"
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
