"""Regression tests for issues found in code review (Codex round 1)."""
import json
import threading
import time

import pytest

from conftest import FakeClient, FakeLLM
from rule_radar import summarize
from rule_radar.clauses import align, split_clauses
from rule_radar.clocked.client import ClockedClient, ClockedError, RawResponse
from rule_radar.llm.base import LLMError
from rule_radar.pipeline import analyze, exit_code
from rule_radar.server import App, GatedClient
from rule_radar.stages import compare, rerank


def test_numbered_rules_are_clauses_not_headings():
    cl = split_clauses("1. Teams must be public\n2. Videos under three minutes\n", "new")
    assert [c.text for c in cl] == ["Teams must be public", "Videos under three minutes"]
    cl = split_clauses("1. Prizes\nFirst place gets $500.\n", "new")
    assert [(c.section, c.text) for c in cl] == [("1. Prizes", "1. Prizes"), ("1. Prizes", "First place gets $500.")]


def test_closing_quote_and_sentence_final_pm():
    cl = split_clauses('He said "Private repositories allowed." Teams need four people.', "n")
    assert [c.text for c in cl] == ['He said "Private repositories allowed."', "Teams need four people."]
    cl = split_clauses("Close at 5 p.m. Teams follow. Close 11:59 p.m. Pacific Time.", "n")
    assert len(cl) == 3 and cl[2].text == "Close 11:59 p.m. Pacific Time."


def test_text_without_headings_aligns():
    ch = align(split_clauses("Same line here. Old bit.", "old"), split_clauses("Same line here. New bit.", "new"))
    assert sorted(c.status for c in ch) == ["edited", "unchanged"]


def _resp(old, new, rel, oq, nq, kind):
    a, b = old.index(oq), new.index(nq)
    bucket = "conflicts" if rel == "conflict" else "duplicates"
    return {"pairs": [{"a": "old", "b": "new", bucket: [{"kind": kind, "confidence": 0.99,
            "a": {"start": a, "end": a + len(oq)}, "b": {"start": b, "end": b + len(nq)}}]}]}


def _interp(old, new, resp):
    o, n = split_clauses(old, "old"), split_clauses(new, "new")
    return compare.interpret(resp, align(o, n), o, n)


def test_partial_restatement_does_not_hide_a_change():
    old = "## R\nTeams must be public; submissions close on Friday at noon UTC."
    new = "## R\nTeams must be public; submissions close on Monday at noon UTC."
    out = _interp(old, new, _resp(old, new, "dup", "Teams must be public", "Teams must be public", "restated"))
    assert out[0]["materiality"] == "unconfirmed"


def test_elaborated_duplicate_is_not_cosmetic():
    old, new = "## R\nVideos are short.", "## R\nVideos are short, at most two minutes."
    out = _interp(old, new, _resp(old, new, "dup", "Videos are short.", "Videos are short, at most two minutes.",
                                  "elaborated"))
    assert out[0]["materiality"] == "unconfirmed"


def test_short_conflict_span_inside_clause_counts():
    old, new = "## T\nTeams of up to six people.", "## T\nTeams of up to four people."
    out = _interp(old, new, _resp(old, new, "conflict", "six", "four", "value"))
    assert out[0]["materiality"] == "material"


def test_template_brief_covers_edits_outside_extract_fields():
    changes = [{"id": "new:1.1", "status": "edited", "materiality": "material", "section": "Judging",
                "old": {"text": "Usefulness is 30%.", "start": 0, "end": 18},
                "new": {"text": "Usefulness is 50%.", "start": 0, "end": 18}, "rank": {"label": "act"}}]
    lines = summarize.offline_brief(changes, [])
    assert lines and "Usefulness is 50%." in lines[0]["text"] and lines[0]["change_ids"] == ["new:1.1"]


def test_extract_facts_follow_rerank_exclusions():
    changes = [{"id": "new:2.1", "status": "edited", "materiality": "material", "section": "Eligibility",
                "old": {"text": "Open to all.", "start": 0, "end": 12},
                "new": {"text": "Open to all except Quebec.", "start": 0, "end": 26},
                "rank": {"label": "not_your_case"}}]
    fields = [{"name": "excluded_residents", "label": "Excluded residents", "status": "added", "uncertain": False,
               "old": None, "new": {"value": ["Quebec"], "spans": [[19, 25]]}}]
    assert summarize._facts(changes, fields)["key_facts"] == []
    assert summarize.offline_brief(changes, fields) == []


@pytest.mark.parametrize("reply", ['{"lines": [{"text": "x", "change_ids": 123}]}',
                                   '{"lines": ["just a string", {"text": "ok"}]}'])
def test_odd_model_json_does_not_crash(reply):
    lines, _ = summarize.write_brief([], [], FakeLLM([reply]))
    assert lines[-1]["text"] in ("x", "ok")


def test_odd_repair_json_is_ignored():
    fixes = summarize.repair([{"index": 0, "text": "a"}], FakeLLM(['{"lines": [{"index": [], "text": "b"}, '
                                                                 '{"index": true, "text": "c"}]}']))
    assert fixes == {}


def test_missing_verify_judgments_mark_the_brief_partial(v1, v2):
    fc = FakeClient({"compare": {"pairs": []}, "extract": {"fields": {}},
                     "rerank": lambda p: {"results": []}, "grounding": {"claims": []}})
    rep = analyze(v1, v2, client=fc, llm=None)
    assert rep["brief"]["verified"] is False and rep["brief"]["partial"] is True
    assert rep["assessment"]["complete"] is False and exit_code(rep) == 4


def test_all_failures_give_unknown_action(v1, v2):
    rep = analyze(v1, v2, client=FakeClient(), llm=None)
    assert rep["assessment"]["action"] == "unknown" and exit_code(rep) == 4


def test_gated_client_caps_concurrency():
    active, peak, lock = [0], [0], threading.Lock()

    class Slow:
        def call(self, endpoint, payload):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return {}, None

    gate = threading.BoundedSemaphore(4)
    ts = [threading.Thread(target=GatedClient(Slow(), gate).call, args=("x", {})) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 4


def test_app_uses_one_gate_for_all_requests():
    from rule_radar.config import Settings
    app = App(Settings.from_env({"CLOCKED_MODE": "replay"}))
    assert app.client().gate is app.client().gate


def test_409_after_unknown_outcome_is_recovered():
    seq = [OSError("timeout"), RawResponse(409, b'{"error":"still running"}'),
           RawResponse(200, b'{"ok": 1}', {"x-clocked-credits": "2"})]
    keys = []

    def transport(url, body, headers, timeout):
        keys.append(headers["Idempotency-Key"])
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    c = ClockedClient("k", transport=transport, sleep=lambda s: None)
    data, usage = c.call("compare", {})
    assert data == {"ok": 1} and usage.attempts == 3 and len(set(keys)) == 1


def test_409_without_unknown_outcome_is_not_retried():
    seq = [RawResponse(409, b'{"error":"key reused"}'), RawResponse(200, b"{}")]
    c = ClockedClient("k", transport=lambda *a: seq.pop(0), sleep=lambda s: None)
    with pytest.raises(ClockedError) as e:
        c.call("compare", {})
    assert e.value.status == 409


def _chg(i, text="x"):
    return {"id": f"new:{i}", "status": "added", "materiality": "new", "section": "S", "new": {"text": text}}


def test_rerank_batches_every_change_and_flags_long_passages():
    changes = [_chg(i) for i in range(300)]
    changes.append({"id": "new:big", "status": "edited", "materiality": "material", "section": "S",
                    "old": {"text": "o" * 15000}, "new": {"text": "n" * 15000}})
    payloads, short, lossy = rerank.build_payloads(changes, {})
    assert [len(p["documents"]) for p in payloads] == [256, 45]
    assert all("questions" not in p for p in payloads)          # no partial yes/no answer
    big = next(d for p in payloads for d in p["documents"] if d["id"] == "new:big")
    assert short == {"new:big"} and "Now: nnn" in big["text"] and len(big["text"]) <= rerank.MAX_PASSAGE
    assert lossy == {"new:big"}   # every character differs, so a window cannot keep the whole change


def test_rerank_merge_orders_by_relevance_across_batches():
    merged = rerank.merge([{"a": {"position": 0, "relevance": 0.2}}, {"b": {"position": 0, "relevance": 0.9}}])
    assert merged["b"]["position"] == 0 and merged["a"]["position"] == 1


def test_model_lines_are_capped():
    reply = json.dumps({"lines": [{"text": f"line {i}"} for i in range(20)]})
    lines, _ = summarize.write_brief([], [], FakeLLM([reply]))
    assert len(lines) == summarize.MAX_LINES


def test_llm_error_type_on_garbage():
    with pytest.raises(LLMError):
        summarize.write_brief([], [], FakeLLM(['{"lines": "nope"}']))
