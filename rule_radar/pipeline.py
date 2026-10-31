"""Run the whole radar: diff -> Compare + Extract -> Rerank -> brief -> Verify (-> repair).

Each stage reports its own status. A failed Clocked call never sinks the run: the
report says which stage failed and what the app did instead, e.g. "Compare
unavailable: every edit is shown as unconfirmed" or "Verify unavailable: the brief
is marked NOT VERIFIED".
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from . import summarize
from .clauses import align, split_clauses
from .clocked import ClockedError, Usage
from .llm.base import LLM, LLMError
from .stages import compare, extract, rerank, verify

MAX_VERSION_CHARS = 90_000   # two versions must fit Compare/Verify's 200k-character budget


class Client(Protocol):
    def call(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], Usage]: ...


class InputError(ValueError):
    pass


def _check_inputs(old_text: str, new_text: str) -> None:
    for name, t in (("old", old_text), ("new", new_text)):
        if not t or not t.strip():
            raise InputError(f"the {name} version is empty")
        if len(t) > MAX_VERSION_CHARS:
            raise InputError(f"the {name} version is {len(t):,} characters; the limit is {MAX_VERSION_CHARS:,}")
    if old_text.strip() == new_text.strip():
        raise InputError("the two versions are identical")


class _Stages:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.usage: list[Usage] = []

    def ok(self, name: str, usage: Usage | list[Usage] | None = None, note: str = "") -> None:
        used = usage if isinstance(usage, list) else [usage] if usage else []
        entry: dict[str, Any] = {"status": "ok", "note": note}
        if used:
            entry["usage"] = {"credits": round(sum(u.credits for u in used), 2), "calls": len(used),
                              "attempts": sum(u.attempts for u in used),
                              "model_ms": round(sum(u.model_ms for u in used), 1)}
        self.items[name] = entry
        self.usage.extend(used)

    def fail(self, name: str, err: Exception, fallback: str) -> None:
        kind = err.kind if isinstance(err, ClockedError) else "model"
        self.items[name] = {"status": "failed", "error": str(err), "error_kind": kind, "fallback": fallback}

    def skip(self, name: str, note: str) -> None:
        self.items[name] = {"status": "skipped", "note": note}


def _call(client: Client, endpoint: str, payload: dict[str, Any]):
    try:
        return client.call(endpoint, payload), None
    except ClockedError as e:
        return None, e


def analyze(old_text: str, new_text: str, *, client: Client, llm: LLM | None = None,
            profile: dict[str, Any] | None = None, query: str | None = None,
            do_repair: bool = True) -> dict[str, Any]:
    _check_inputs(old_text, new_text)
    t0 = time.monotonic()
    st = _Stages()
    old_cl, new_cl = split_clauses(old_text, "old"), split_clauses(new_text, "new")
    aligned = align(old_cl, new_cl)
    st.ok("diff", note=f"{len(old_cl)} -> {len(new_cl)} clauses")

    # Phase 1: Compare and both Extract reads are independent -> run them together.
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_cmp = pool.submit(_call, client, "compare", compare.build_payload(old_text, new_text))
        f_old = pool.submit(_call, client, "extract", extract.build_payload(old_text))
        f_new = pool.submit(_call, client, "extract", extract.build_payload(new_text))
        (cmp_res, cmp_err), (old_res, old_err), (new_res, new_err) = f_cmp.result(), f_old.result(), f_new.result()

    if cmp_res:
        changes = compare.interpret(cmp_res[0], aligned, old_cl, new_cl)
        st.ok("compare", cmp_res[1], note=f"verdict: {cmp_res[0].get('verdict')}")
    else:
        changes = compare.local_only(aligned)
        st.fail("compare", cmp_err, "every edit is shown as unconfirmed (local diff only)")

    fields: list[dict[str, Any]] = []
    if old_res and new_res:
        fields = extract.diff_fields(extract.interpret(old_res[0], old_text), extract.interpret(new_res[0], new_text))
        st.ok("extract", [old_res[1], new_res[1]], note="old and new version read")
    else:
        st.fail("extract", old_err or new_err, "key-facts table not available")

    # Phase 2: Rerank orders what is left by impact on this entrant.
    ranks, action_needed = None, None
    rr_payloads, shortened = rerank.build_payloads(changes, profile, query)
    if not rr_payloads:
        st.skip("rerank", "no rule changes to rank")
    else:
        parts, used, rr_err = [], [], None
        for p in rr_payloads:
            res, rr_err = _call(client, "rerank", p)
            if not res:
                break
            r, verdict = rerank.interpret(res[0])
            parts.append(r)
            used.append(res[1])
            action_needed = action_needed or verdict
        if rr_err is None:
            ranks = rerank.merge(parts)
            note = f"{len(ranks)} changes ranked" + (f" in {len(rr_payloads)} requests" if len(rr_payloads) > 1 else "")
            if shortened:
                note += f"; {len(shortened)} long change(s) ranked on a shortened passage"
            st.ok("rerank", used, note=note)
        else:
            action_needed = None
            st.fail("rerank", rr_err, "changes are ordered by a local rule (material first)")
    for c in changes:
        c["shortened_for_rerank"] = c["id"] in shortened
    changes = rerank.order(changes, ranks)

    # Phase 3: the brief (model, or template when there is no model / the model fails).
    brief_source = "template"
    try:
        lines, brief_source = summarize.write_brief(changes, fields, llm)
        st.ok("brief", note=brief_source)
    except LLMError as e:
        lines = summarize.offline_brief(changes, fields)
        st.fail("brief", e, "template brief written from the change list")

    # Phase 4: Verify every line; repair rejected lines once with the model, then re-verify them.
    repair_llm = llm if do_repair and st.items["brief"]["status"] == "ok" else None  # a failed model is not retried
    brief = _verify_brief(client, st, old_text, new_text, lines, repair_llm)
    brief["source"] = brief_source

    counts: dict[str, int] = {}
    for c in changes:
        counts[c["materiality"]] = counts.get(c["materiality"], 0) + 1
    return {
        "assessment": _assessment(st.items, changes, action_needed, brief),
        "versions": {"old": old_text, "new": new_text},
        "profile": profile or {},
        "changes": changes, "fields": fields, "brief": brief, "action_needed": action_needed,
        "counts": counts, "stages": st.items,
        "usage": {"credits": round(sum(u.credits for u in st.usage), 2),
                  "cost_usd": round(sum(u.cost_usd for u in st.usage), 6),
                  "calls": len(st.usage), "replayed": any(u.replayed for u in st.usage)},
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
    }


EXIT_CHANGES, EXIT_INCOMPLETE = 3, 4


def exit_code(rep: dict[str, Any]) -> int:
    """CLI/watch status: 0 = nothing relevant changed, 3 = relevant changes, 4 = incomplete assessment."""
    a = rep["assessment"]
    if not a["complete"]:
        return EXIT_INCOMPLETE
    return EXIT_CHANGES if a["relevant_changes"] else 0


def relevant(change: dict[str, Any]) -> bool:
    """A change an entrant should look at: not cosmetic and not ranked as someone else's case."""
    return change["materiality"] != "wording" and (change.get("rank") or {}).get("label") != "not_your_case"


def _assessment(stages: dict[str, dict[str, Any]], changes: list[dict[str, Any]],
                action_needed: dict[str, Any] | None, brief: dict[str, Any]) -> dict[str, Any]:
    """Overall answer + whether it can be trusted. Used by the UI banner and CLI exit codes."""
    failed = [k for k, v in stages.items() if v["status"] == "failed"]
    incomplete = bool(failed) or bool(brief.get("partial"))
    if action_needed and isinstance(action_needed.get("decision"), bool):
        action = "yes" if action_needed["decision"] else "no"
    elif "rerank" in failed or "compare" in failed:
        action = "unknown"
    else:
        action = "yes" if any((c.get("rank") or {}).get("label") == "act" for c in changes) else "no"
    return {"action": action, "complete": not incomplete, "failed_stages": failed,
            "relevant_changes": sum(1 for c in changes if relevant(c))}


def _verify_brief(client: Client, st: _Stages, old_text: str, new_text: str,
                  lines: list[dict[str, Any]], llm: LLM | None) -> dict[str, Any]:
    texts = [ln["text"] for ln in lines][:verify.MAX_CLAIMS]
    if not texts:
        st.skip("verify", "nothing to verify")
        return {"lines": [], "rejected": [], "grounded": None, "verified": False, "partial": False}
    res, err = _call(client, "grounding", verify.build_payload(old_text, new_text, texts))
    if not res:
        st.fail("verify", err, "brief is shown as NOT VERIFIED")
        v = verify.unverified(texts)
        return {"lines": _join(lines, v["claims"]), "rejected": [], "grounded": None, "verified": False,
                "partial": False}
    v = verify.interpret(res[0], texts)
    st.ok("verify", res[1], note=f"grounded {v['grounded']}")
    rows = _join(lines, v["claims"])

    bad = [i for i, r in enumerate(rows) if r["status"] == "rejected"]
    if bad and llm is not None:
        _repair(client, st, old_text, new_text, rows, bad, llm)
    else:
        st.skip("repair", "no rejected lines" if not bad else "no model available to rewrite them")
    kept = [r for r in rows if r["status"] != "rejected"]
    rejected = [r for r in rows if r["status"] == "rejected"]
    missing = sum(1 for r in kept if r["status"] == "unverified")
    if missing:  # Verify answered, but not for every line: say so instead of claiming a checked brief
        st.items["verify"]["note"] += f"; {missing} line(s) had no judgment"
    return {"lines": kept, "rejected": rejected, "grounded": v["grounded"], "verified": missing == 0,
            "partial": missing > 0}


def _join(lines: list[dict[str, Any]], claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**ln, **cl} for ln, cl in zip(lines, claims)]


def _repair(client: Client, st: _Stages, old_text: str, new_text: str, rows: list[dict[str, Any]],
            bad: list[int], llm: LLM) -> None:
    ask = [{"index": i, "text": rows[i]["claim"], "verdict": rows[i]["verdict"],
            "evidence": (rows[i].get("evidence") or {}).get("quote")} for i in bad]
    try:
        fixes = summarize.repair(ask, llm)
    except LLMError as e:
        st.fail("repair", e, "rejected lines stay out of the brief")
        return
    todo = [(i, t) for i, t in fixes.items() if t]
    for i, t in fixes.items():
        if t is None:
            rows[i]["repair"] = "dropped by the model"
    if not todo:
        st.ok("repair", note=f"{len(fixes)} line(s) dropped")
        return
    res, err = _call(client, "grounding", verify.build_payload(old_text, new_text, [t for _, t in todo]))
    if not res:
        st.fail("repair", err, "rewritten lines could not be re-verified, so they stay out")
        return
    v = verify.interpret(res[0], [t for _, t in todo])
    for (i, t), claim in zip(todo, v["claims"]):
        original = rows[i]["claim"]
        if claim["status"] in ("verified", "check"):
            rows[i] = {**rows[i], **claim, "text": t, "repaired_from": original}
        else:
            rows[i]["repair"] = "rewrite was rejected again"
    st.ok("repair", res[1], note=f"{len(todo)} line(s) rewritten and re-verified")


def check_claims(old_text: str, new_text: str, claims: list[str], *, client: Client) -> dict[str, Any]:
    """Verify claims someone else made (a forum post, a chat message) against both versions.

    Each claim is its own Verify request (run in parallel), so one claim's result never
    depends on which other claims were pasted with it.
    """
    _check_inputs(old_text, new_text)
    claims = [c.strip() for c in claims if c and c.strip()][:verify.MAX_CLAIMS]
    if not claims:
        raise InputError("enter at least one claim")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda c: _call(client, "grounding", verify.build_payload(old_text, new_text, [c])),
                                claims))
    rows, credits = [], 0.0
    for claim, (res, err) in zip(claims, results):
        if res:
            rows.append(verify.interpret(res[0], [claim])["claims"][0])
            credits += res[1].credits
        else:
            rows.append({**verify.unverified([claim])["claims"][0], "error": str(err)})
    return {"claims": rows, "usage": {"credits": round(credits, 2), "calls": len(claims)}}
