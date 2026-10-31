"""Verify stage: check every line of the change brief against both versions.

The brief is written by a language model, so it can be wrong. Each line is sent to
Clocked Verify (``/grounding``) as an explicit claim, with the old and new rules as
the only sources. A line is published only when the sources support it:

- ``verified``  supported with probability >= VERIFIED
- ``check``     supported, but below VERIFIED -> published with a "check" badge
- ``rejected``  contradicted or not stated -> pulled out of the brief, evidence shown
- ``not_a_claim`` Verify found nothing factual to check -> also kept out of the brief
"""
from __future__ import annotations

from typing import Any

VERIFIED = 0.8
INSTRUCTIONS = (
    "Document 'old' is the previous version of the rules and 'new' is the current version. A claim that "
    "something changed is supported only when the old version states the earlier value and the new "
    "version states the new value. A claim about the current rules must be supported by 'new'. Missing "
    "evidence is not proof that a claim is false; use not stated for it."
)
MAX_CLAIMS = 30
PULLED = {"rejected", "not_a_claim"}   # statuses that never appear in the published brief


def build_payload(old_text: str, new_text: str, claims: list[str], question: str | None = None) -> dict[str, Any]:
    claims = [c.strip() for c in claims if c and c.strip()][:MAX_CLAIMS]
    if not claims:
        raise ValueError("no claims to verify")
    payload: dict[str, Any] = {
        "documents": [{"id": "old", "text": old_text}, {"id": "new", "text": new_text}],
        "answer": " ".join(claims)[:20000], "claims": claims, "evidence": "all",
        "instructions": INSTRUCTIONS,
    }
    if question:
        payload["question"] = question
    return payload


def classify(claim: dict[str, Any]) -> str:
    if claim.get("verdict") == "supported":
        return "verified" if (claim.get("supported") or 0) >= VERIFIED else "check"
    if claim.get("verdict") == "not_a_claim":
        return "not_a_claim"   # no factual support either way; never shown as "weakly supported"
    return "rejected"


def interpret(resp: dict[str, Any], claims: list[str]) -> dict[str, Any]:
    rows = []
    got = resp.get("claims") or []
    for i, text in enumerate(claims[:MAX_CLAIMS]):
        c = got[i] if i < len(got) and isinstance(got[i], dict) else None
        if c is None:
            rows.append({"claim": text, "status": "unverified", "verdict": None, "supported": None,
                         "evidence": None, "source": None})
            continue
        ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else None
        rows.append({"claim": text, "status": classify(c), "verdict": c.get("verdict"),
                     "supported": c.get("supported"), "contradicted": c.get("contradicted"),
                     "not_stated": c.get("not_stated"), "source": c.get("source"), "evidence": ev})
    return {"grounded": resp.get("grounded"), "verdict": resp.get("verdict"),
            "answers_question": resp.get("answers_question"), "claims": rows}


def unverified(claims: list[str]) -> dict[str, Any]:
    """What the brief looks like when Verify could not run."""
    return {"grounded": None, "verdict": None, "answers_question": None,
            "claims": [{"claim": c, "status": "unverified", "verdict": None, "supported": None,
                        "evidence": None, "source": None} for c in claims]}
