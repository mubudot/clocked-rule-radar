"""Extract stage: read the key facts of each version verbatim, with source spans.

Clocked Extract copies values exactly and returns their offsets, so the UI can
highlight where a deadline or a prize amount sits in each version. A field the
text does not state comes back as ``null``; we show it as "not stated" instead of
guessing, and a low-confidence read is flagged for a human to check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..clauses import norm

# name -> (label shown in the UI, Clocked field definition)
DEFAULT_FIELDS: dict[str, tuple[str, Any]] = {
    "submission_deadline": ("Submission deadline",
                            {"description": "the date and time when submissions close, with its time zone"}),
    "building_period": ("Building period", "the dates of the building period"),
    "winners_announced": ("Winners announced", {"description": "the date winners are announced",
                                                "normalize": "date"}),
    "prize_pool": ("Total prize pool", "the total prize pool amount"),
    "top_prize": ("Top prize", "the amount of the first place or best overall prize"),
    "max_team_size": ("Max team size", {"description": "the maximum number of people in a team",
                                        "normalize": "integer"}),
    "video_limit": ("Demo video limit", "the maximum length of the demo video"),
    "repository_rule": ("Repository rule", "what kind of source repository must be submitted"),
    "existing_work_rule": ("Existing-work rule",
                           "the rule about projects started before the building period"),
    "excluded_residents": ("Excluded residents", {"description": "places whose residents may not enter",
                                                  "list": True, "max_items": 12}),
    "payout_method": ("Prize payout", "how prizes are paid"),
}
LOW_CONFIDENCE = 0.6


@dataclass
class FieldRead:
    name: str
    value: Any                       # str | list[str] | None
    spans: list[tuple[int, int]] = field(default_factory=list)
    confidence: float = 0.0
    normalized: Any = None

    @property
    def stated(self) -> bool:
        return self.value not in (None, "", [])

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "spans": [list(s) for s in self.spans],
                "confidence": round(self.confidence, 4), "normalized": self.normalized}


def build_payload(text: str, fields: dict[str, tuple[str, Any]] | None = None) -> dict[str, Any]:
    fields = fields or DEFAULT_FIELDS
    return {
        "text": text,
        "fields": {name: spec for name, (_label, spec) in fields.items()},
        "instructions": "The text is the official rules of a contest or hackathon. Copy values exactly "
                        "as written. If a rule is not stated, return null.",
    }


def interpret(resp: dict[str, Any], text: str) -> dict[str, FieldRead]:
    out: dict[str, FieldRead] = {}
    for name, f in (resp.get("fields") or {}).items():
        if not isinstance(f, dict):
            continue
        value = f.get("value")
        spans: list[tuple[int, int]] = []
        items = f.get("items")
        if isinstance(items, list):
            for it in items:
                s, e = it.get("start"), it.get("end")
                if _valid_span(text, s, e):
                    spans.append((s, e))
        elif _valid_span(text, f.get("start"), f.get("end")):
            spans.append((f["start"], f["end"]))
        norm_v = f.get("normalized")
        if isinstance(norm_v, dict):
            norm_v = norm_v.get("value")
        out[name] = FieldRead(name, value, spans, float(f.get("confidence") or 0.0), norm_v)
    return out


def _valid_span(text: str, s: Any, e: Any) -> bool:
    return isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= len(text)


def _key(v: Any) -> Any:
    if isinstance(v, list):
        return frozenset(norm(str(x)) for x in v)
    return norm(str(v)) if v is not None else None


def diff_fields(old: dict[str, FieldRead], new: dict[str, FieldRead],
                fields: dict[str, tuple[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One row per field: same / changed / added / removed / not_stated."""
    fields = fields or DEFAULT_FIELDS
    rows = []
    for name, (label, _spec) in fields.items():
        o, n = old.get(name), new.get(name)
        o_stated, n_stated = bool(o and o.stated), bool(n and n.stated)
        if not o_stated and not n_stated:
            status = "not_stated"
        elif o_stated and not n_stated:
            status = "removed"
        elif n_stated and not o_stated:
            status = "added"
        else:
            status = "same" if _key(o.value) == _key(n.value) else "changed"  # type: ignore[union-attr]
        reads = [r for r in (o, n) if r is not None and r.stated]
        uncertain = any(r.confidence < LOW_CONFIDENCE for r in reads)
        rows.append({"name": name, "label": label, "status": status, "uncertain": uncertain,
                     "old": o.as_dict() if o else None, "new": n.as_dict() if n else None})
    return rows
