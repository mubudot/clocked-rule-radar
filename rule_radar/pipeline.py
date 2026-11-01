"""Run the whole radar: diff -> Compare + Extract -> Rerank -> brief -> Verify (-> repair).

Each stage reports its own status. A failed Clocked call never sinks the run: the
report says which stage failed and what the app did instead, e.g. "Compare
unavailable: every edit is shown as unconfirmed" or "Verify unavailable: the brief
is marked NOT VERIFIED".
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from . import summarize
from .clauses import align, split_clauses
from .clocked import ClockedError, Usage
from .llm.base import LLM, LLMError
from .stages import compare, extract, rerank, verify

MAX_CHECK_CLAIMS = 20       # rumour checker: one Verify request per claim
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
    """Per-stage status, plus the usage of every Clocked call that returned a result.

    Usage is recorded per call, not per stage, so a stage that half-failed (one Extract
    read out of two, two Rerank batches out of three) still reports the credits it spent."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.usage: list[Usage] = []
        self.by_stage: dict[str, list[Usage]] = {}
        self.failed_calls: dict[str, int] = {}
        self._lock = threading.Lock()

    def call(self, stage: str, client: Client, endpoint: str, payload: dict[str, Any]):
        try:
            res = client.call(endpoint, payload)
        except ClockedError as e:
            with self._lock:
                self.failed_calls[stage] = self.failed_calls.get(stage, 0) + 1
            return None, e
        with self._lock:
            self.usage.append(res[1])
            self.by_stage.setdefault(stage, []).append(res[1])
        return res, None

    def _usage(self, name: str) -> dict[str, Any] | None:
        used = self.by_stage.get(name) or []
        failed = self.failed_calls.get(name, 0)
        if not used and not failed:
            return None
        return {"credits": round(sum(u.credits for u in used), 2), "calls": len(used),
                "failed_calls": failed, "attempts": sum(u.attempts for u in used),
                "model_ms": round(sum(u.model_ms for u in used), 1)}

    def ok(self, name: str, note: str = "", partial: bool = False) -> None:
        entry: dict[str, Any] = {"status": "partial" if partial else "ok", "note": note}
        if (u := self._usage(name)) is not None:
            entry["usage"] = u
        self.items[name] = entry

    def fail(self, name: str, err: Exception, fallback: str) -> None:
        kind = err.kind if isinstance(err, ClockedError) else "model"
        entry: dict[str, Any] = {"status": "failed", "error": str(err), "error_kind": kind, "fallback": fallback}
        if (u := self._usage(name)) is not None:
            entry["usage"] = u
        self.items[name] = entry

    def skip(self, name: str, note: str) -> None:
        self.items[name] = {"status": "skipped", "note": note}


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
        f_cmp = pool.submit(st.call, "compare", client, "compare", compare.build_payload(old_text, new_text))
        f_old = pool.submit(st.call, "extract", client, "extract", extract.build_payload(old_text))
        f_new = pool.submit(st.call, "extract", client, "extract", extract.build_payload(new_text))
        (cmp_res, cmp_err), (old_res, old_err), (new_res, new_err) = f_cmp.result(), f_old.result(), f_new.result()

    if cmp_res:
        changes = compare.interpret(cmp_res[0], aligned, old_cl, new_cl)
        st.ok("compare", note=f"verdict: {cmp_res[0].get('verdict')}")
    else:
        changes = compare.local_only(aligned)
        st.fail("compare", cmp_err, "every edit is shown as unconfirmed (local diff only)")
    st.items["compare"]["outcomes"] = {
        kind: sum(c["materiality"] == kind for c in changes)
        for kind in ("material", "wording", "unconfirmed")
    }

    fields: list[dict[str, Any]] = []
    old_fields = extract.interpret(old_res[0], old_text) if old_res else None
    new_fields = extract.interpret(new_res[0], new_text) if new_res else None
    if old_fields is not None and new_fields is not None:
        fields = extract.diff_fields(old_fields, new_fields)
        st.ok("extract", note="old and new version read")
    else:
        st.fail("extract", old_err or new_err, "key-facts table not available")
    st.items["extract"]["outcomes"] = {
        "old": _extract_outcomes(old_fields), "new": _extract_outcomes(new_fields),
    }

    # Phase 2: Rerank orders what is left by impact on this entrant.
    ranks, action_needed = None, None
    rr_payloads, shortened, lossy = rerank.build_payloads(changes, profile, query)
    rerank_gap = False   # some change got no usable judgment -> the overall answer is not complete
    if not rr_payloads:
        st.skip("rerank", "no rule changes to rank")
    else:
        parts, rr_err = [], None
        for p in rr_payloads:
            res, rr_err = st.call("rerank", client, "rerank", p)
            if not res:
                break
            r, verdict = rerank.interpret(res[0])
            parts.append(r)
            action_needed = action_needed or verdict
        if rr_err is None:
            ranks = rerank.merge(parts)
            sent = {d["id"] for p in rr_payloads for d in p["documents"]}
            missing = sorted(sent - set(ranks))
            note = f"{len(ranks)} of {len(sent)} changes ranked" + (
                f" in {len(rr_payloads)} requests" if len(rr_payloads) > 1 else "")
            if shortened:
                note += f"; {len(shortened)} long change(s) ranked on a shortened passage"
            if missing:
                note += f"; no judgment for {len(missing)} change(s)"
            if lossy:
                note += f"; {len(lossy)} change(s) too long to rank in full"
            rerank_gap = bool(missing or lossy)
            st.ok("rerank", note=note, partial=rerank_gap)
        else:
            action_needed = None
            st.fail("rerank", rr_err, "changes are ordered by a local rule (material first)")
    for c in changes:
        c["shortened_for_rerank"] = c["id"] in shortened
    changes = rerank.order(changes, ranks)
    st.items["rerank"]["outcomes"] = {
        label: sum((c.get("rank") or {}).get("label") == label for c in changes)
        for label in ("act", "read_section", "low_impact", "not_your_case")
    }
    st.items["rerank"]["outcomes"]["not_judged"] = sum(
        not c.get("rank") for c in changes if c["materiality"] in rerank.RANKABLE)

    # Phase 3: the brief (model, or template when there is no model / the model fails).
    brief_source = "template"
    try:
        try:
            lines, brief_source = summarize.write_brief(changes, fields, llm)
        except LLMError as first:
            if not first.retryable:
                raise
            lines, brief_source = summarize.write_brief(changes, fields, llm)  # one more try, then template
            first_note = f"; first reply unusable ({first})"
        else:
            first_note = ""
        st.ok("brief", note=brief_source + first_note)
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
                  "calls": len(st.usage), "replayed": any(u.replayed for u in st.usage),
                  "ledger_replays": sum(1 for u in st.usage if u.ledger_replay)},
        "elapsed_ms": round((time.monotonic() - t0) * 1000),
    }


def _extract_outcomes(reads: dict[str, extract.FieldRead] | None) -> dict[str, int] | None:
    """Missing fields are not stated; a failed read is unavailable, not an empty result.

    Low confidence is a subset of found fields, matching the key-facts table's flag.
    """
    if reads is None:
        return None
    found = [reads[name] for name in extract.DEFAULT_FIELDS if name in reads and reads[name].stated]
    return {"found": len(found), "not_stated": len(extract.DEFAULT_FIELDS) - len(found),
            "low_confidence": sum(r.confidence < extract.LOW_CONFIDENCE for r in found)}


def _verify_outcomes(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count the initial Verify judgments; repair reports its later result separately."""
    return {"supported": sum(r["status"] == "verified" for r in rows),
            "weak": sum(r["status"] == "check" for r in rows),
            "pulled": sum(r["status"] in verify.PULLED for r in rows),
            "not_judged": sum(r["status"] == "unverified" for r in rows)}


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
    """Overall answer + whether it can be trusted. Used by the UI banner and CLI exit codes.

    A stage that ran only in part (Rerank judged some changes but not all, Verify answered for
    some lines) makes the run incomplete. An all-clear is never given on partial coverage: a
    "yes" from part of the changes still stands, but "no" becomes "unknown"."""
    failed = [k for k, v in stages.items() if v["status"] == "failed"]
    partial = [k for k, v in stages.items() if v["status"] == "partial"]
    incomplete = bool(failed or partial) or bool(brief.get("partial"))
    ranking_gap = "rerank" in failed or "compare" in failed or "rerank" in partial
    if action_needed and isinstance(action_needed.get("decision"), bool) and not (
            ranking_gap and not action_needed["decision"]):
        action = "yes" if action_needed["decision"] else "no"
    elif any((c.get("rank") or {}).get("label") == "act" for c in changes):
        action = "yes"
    elif ranking_gap:
        action = "unknown"
    else:
        action = "no"
    return {"action": action, "complete": not incomplete, "failed_stages": failed, "partial_stages": partial,
            "relevant_changes": sum(1 for c in changes if relevant(c))}


def _verify_brief(client: Client, st: _Stages, old_text: str, new_text: str,
                  lines: list[dict[str, Any]], llm: LLM | None) -> dict[str, Any]:
    texts = [ln["text"] for ln in lines][:verify.MAX_CLAIMS]
    if not texts:
        st.skip("verify", "nothing to verify")
        st.items["verify"]["outcomes"] = _verify_outcomes([])
        return {"lines": [], "rejected": [], "grounded": None, "verified": False, "partial": False}
    res, err = st.call("verify", client, "grounding", verify.build_payload(old_text, new_text, texts))
    if not res:
        st.fail("verify", err, "brief is shown as NOT VERIFIED")
        v = verify.unverified(texts)
        st.items["verify"]["outcomes"] = _verify_outcomes(v["claims"])
        return {"lines": _join(lines, v["claims"]), "rejected": [], "grounded": None, "verified": False,
                "partial": False}
    v = verify.interpret(res[0], texts)
    st.ok("verify", note=f"grounded {v['grounded']}")
    st.items["verify"]["outcomes"] = _verify_outcomes(v["claims"])
    rows = _join(lines, v["claims"])

    bad = [i for i, r in enumerate(rows) if r["status"] in verify.PULLED]
    if bad and llm is not None:
        _repair(client, st, old_text, new_text, rows, bad, llm)
    else:
        st.skip("repair", "no rejected lines" if not bad else "no model available to rewrite them")
    kept = [r for r in rows if r["status"] not in verify.PULLED]
    rejected = [r for r in rows if r["status"] in verify.PULLED]
    missing = sum(1 for r in kept if r["status"] == "unverified")
    if missing:  # Verify answered, but not for every line: say so instead of claiming a checked brief
        st.items["verify"]["note"] += f"; {missing} line(s) had no judgment"
        st.items["verify"]["status"] = "partial"
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
    res, err = st.call("repair", client, "grounding", verify.build_payload(old_text, new_text, [t for _, t in todo]))
    if not res:
        st.fail("repair", err, "rewritten lines could not be re-verified, so they stay out")
        return
    v = verify.interpret(res[0], [t for _, t in todo])
    unjudged = 0
    for (i, t), claim in zip(todo, v["claims"]):
        original = rows[i]["claim"]
        if claim["status"] in ("verified", "check"):
            rows[i] = {**rows[i], **claim, "text": t, "repaired_from": original}
        elif claim["status"] == "unverified":
            unjudged += 1   # Verify gave no judgment: the rewrite stays out, and the run is not complete
            rows[i]["repair"] = "rewrite got no judgment from Verify, so it stays out"
        else:
            rows[i]["repair"] = "rewrite was rejected again"
    note = f"{len(todo)} line(s) rewritten and re-verified"
    if unjudged:
        note += f"; {unjudged} rewrite(s) had no judgment"
    st.ok("repair", note=note, partial=bool(unjudged))


def check_claims(old_text: str, new_text: str, claims: list[str], *, client: Client) -> dict[str, Any]:
    """Verify claims someone else made (a forum post, a chat message) against both versions.

    Each claim is its own Verify request (run in parallel), so one claim's result never
    depends on which other claims were pasted with it.
    """
    _check_inputs(old_text, new_text)
    claims = [c.strip() for c in claims if c and c.strip()]
    if not claims:
        raise InputError("enter at least one claim")
    if len(claims) > MAX_CHECK_CLAIMS:  # refuse before spending anything, never drop claims silently
        raise InputError(f"{len(claims)} claims pasted; check at most {MAX_CHECK_CLAIMS} at a time")
    st = _Stages()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda c: st.call("check", client, "grounding", verify.build_payload(old_text, new_text, [c])), claims))
    rows = []
    for claim, (res, err) in zip(claims, results):
        if res:
            rows.append(verify.interpret(res[0], [claim])["claims"][0])
        else:
            rows.append({**verify.unverified([claim])["claims"][0], "error": str(err)})
    return {"claims": rows, "usage": {"credits": round(sum(u.credits for u in st.usage), 2),
                                      "calls": len(st.usage), "failed_calls": len(claims) - len(st.usage)}}
