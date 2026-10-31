"""Plain-text rendering of a report, for the terminal and for watch alerts."""
from __future__ import annotations

from typing import Any

LABEL = {"act": "ACT ON THIS", "read_section": "READ SECTION", "low_impact": "low impact",
         "not_your_case": "not your case", None: "unranked"}
BADGE = {"verified": "✓", "check": "?", "unverified": "·", "rejected": "✗", "not_a_claim": "–"}


def render(rep: dict[str, Any]) -> str:
    out: list[str] = []
    an = rep.get("action_needed") or {}
    a = rep.get("assessment") or {}
    word = {"yes": "YES", "no": "no", "unknown": "UNKNOWN"}.get(a.get("action"), "UNKNOWN")
    prob = an.get("probability")
    out.append(f"Action needed before the deadline: {word}"
               + (f" ({prob:.0%})" if isinstance(prob, (int, float)) and a.get("action") != "unknown" else ""))
    if a.get("complete") is False:
        gaps = [f"{k} failed" for k in a.get("failed_stages", [])] + \
               [f"{k} ran only in part" for k in a.get("partial_stages", [])]
        out.append("Assessment incomplete: " + (", ".join(gaps) or "Verify did not judge every line"))
    b = rep["brief"]
    flag = "" if b.get("verified") else "  [PARTLY VERIFIED]" if b.get("partial") else "  [NOT VERIFIED]"
    head = "Change brief" + flag + f"  (written by {b.get('source')})"
    out += ["", head, "-" * len(head)]
    for ln in b["lines"]:
        out.append(f" {BADGE.get(ln.get('status'), '·')} {ln['text']}")
        if ln.get("repaired_from"):
            out.append(f"     (rewritten after Verify rejected: {ln['repaired_from']})")
    if b.get("rejected"):
        out.append("\nPulled from the brief by Verify:")
        for ln in b["rejected"]:
            ev = (ln.get("evidence") or {}).get("quote")
            mark = BADGE.get(ln.get("status"), "✗")
            out.append(f" {mark} {ln['claim']}" + (f"\n     source says: {ev}" if ev else "")
                       + ("\n     (not a factual claim)" if ln.get("status") == "not_a_claim" else ""))

    out += ["", "Changes, most important first", "-----------------------------"]
    for c in rep["changes"]:
        if c["materiality"] == "wording":
            continue
        label = LABEL.get((c.get("rank") or {}).get("label"))
        out.append(f"[{label}] {c['section']}  ({c['status']}, {c['materiality']})")
        if c.get("old"):
            out.append(f"   - {c['old']['text']}")
        if c.get("new"):
            out.append(f"   + {c['new']['text']}")
    changed = [f for f in rep.get("fields", []) if f["status"] in ("changed", "added", "removed")]
    if changed:
        out += ["", "Key facts that changed", "----------------------"]
        for f in changed:
            o = (f.get("old") or {}).get("value")
            n = (f.get("new") or {}).get("value")
            out.append(f" {f['label']}: {o or '(not stated)'}  ->  {n or '(not stated)'}"
                       + ("  [low confidence]" if f.get("uncertain") else ""))
    failed = {k: v for k, v in rep["stages"].items() if v["status"] == "failed"}
    partial = {k: v for k, v in rep["stages"].items() if v["status"] == "partial"}
    if failed or partial:
        out += ["", "Degraded stages:"]
        out += [f" {k}: {v['error']} -> {v['fallback']}" for k, v in failed.items()]
        out += [f" {k}: ran only in part ({v.get('note', '')})" for k, v in partial.items()]
    u = rep["usage"]
    out.append(f"\n{u['calls']} Clocked calls, {u['credits']} credits"
               + (" (replayed recordings)" if u.get("replayed") else ""))
    return "\n".join(out)
