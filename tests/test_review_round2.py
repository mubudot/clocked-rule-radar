"""Regression tests for issues found in code review (Codex round 2)."""
import json

import pytest

from conftest import FakeClient, FakeLLM
from rule_radar import summarize
from rule_radar.clauses import align, split_clauses
from rule_radar.clocked import ClockedError, Usage
from rule_radar.clocked.client import ClockedClient, RawResponse
from rule_radar.pipeline import InputError, MAX_CHECK_CLAIMS, analyze, check_claims, exit_code
from rule_radar.server import _same_origin
from rule_radar.snapshots import store_dir, watch
from rule_radar.stages import compare, rerank, verify

BOUND = ("127.0.0.1", 8765)


# --- server: paid work only for this app's own pages -------------------------------------------

@pytest.mark.parametrize("host,origin,ok", [
    ("127.0.0.1:8765", None, True),
    ("127.0.0.1:8765", "http://127.0.0.1:8765", True),
    ("localhost:8765", "http://localhost:8765", True),
    ("127.0.0.1:8765", "https://evil.example", False),       # foreign page posting to localhost
    ("evil.example:8765", "http://evil.example:8765", False),  # DNS rebinding
    ("127.0.0.1:9999", None, False),
    (None, None, False),
])
def test_same_origin(host, origin, ok):
    assert _same_origin(host, origin, BOUND) is ok


def test_server_rejects_safelisted_content_type_and_foreign_origin():
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from conftest import REPLAY
    from rule_radar.config import Settings
    from rule_radar.server import App, make_handler
    s = Settings.from_env({"CLOCKED_MODE": "replay", "RADAR_REPLAY_DIR": str(REPLAY)})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(s)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/check"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def status(headers):
        req = urllib.request.Request(url, data=b'{"old":"a","new":"b","claims":["x"]}', headers=headers, method="POST")
        try:
            with opener.open(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert status({"Content-Type": "text/plain; application/json"}) == 415
        assert status({"Content-Type": "application/json", "Origin": "https://evil.example"}) == 403
    finally:
        httpd.shutdown()


# --- clauses ------------------------------------------------------------------------------------

def test_numbered_requirement_with_continuation_is_a_clause():
    old = split_clauses("1. Teams must be public\nThis applies to every entry.\n", "old")
    new = split_clauses("1. Teams must be private\nThis applies to every entry.\n", "new")
    assert any(c.status == "edited" for c in align(old, new))


# --- compare ------------------------------------------------------------------------------------

def _dup(old, new, pairs):
    out = []
    for oq, nq in pairs:
        a, b = old.index(oq), new.index(nq)
        out.append({"kind": "verbatim", "confidence": 0.99, "a": {"start": a, "end": a + len(oq)},
                    "b": {"start": b, "end": b + len(nq)}})
    return {"pairs": [{"a": "old", "b": "new", "duplicates": out, "conflicts": []}]}


def _interp(old, new, resp):
    o, n = split_clauses(old, "old"), split_clauses(new, "new")
    return compare.interpret(resp, align(o, n), o, n)


def test_unpaired_duplicates_do_not_hide_swapped_deadlines():
    old = "## R\nDrafts are due on Friday. Finals are due on Monday."
    new = "## R\nDrafts are due on Monday. Finals are due on Friday."
    # "Friday" in old draft rule matches "Friday" in new finals rule, and vice versa
    resp = _dup(old, new, [("Friday", "Friday"), ("Monday", "Monday")])
    assert {c["materiality"] for c in _interp(old, new, resp)} == {"unconfirmed"}


def test_duplicate_order_does_not_change_the_result():
    old, new = "## R\nVideos are short.", "## R\nVideos are brief."
    oq, nq = "Videos are short.", "Videos are brief."
    a, b = old.index(oq), new.index(nq)

    def f(kind):
        return {"kind": kind, "confidence": 0.9, "a": {"start": a, "end": a + len(oq)}, "b": {"start": b, "end": b + len(nq)}}
    r1 = {"pairs": [{"a": "old", "b": "new", "duplicates": [f("elaborated"), f("restated")]}]}
    r2 = {"pairs": [{"a": "old", "b": "new", "duplicates": [f("restated"), f("elaborated")]}]}
    assert _interp(old, new, r1)[0]["materiality"] == _interp(old, new, r2)[0]["materiality"] == "wording"


# --- template brief / field exclusions -----------------------------------------------------------

def _payout_change():
    old = "Prizes are paid by bank transfer."
    new = "Prizes are paid through Wise; winners must submit a tax form."
    return {"id": "new:1.1", "status": "edited", "materiality": "material", "section": "Prizes",
            "rank": {"label": "act"}, "old": {"text": old, "start": 0, "end": len(old)},
            "new": {"text": new, "start": 0, "end": len(new)}}, old, new


def test_template_keeps_obligations_outside_the_extracted_field():
    ch, old, new = _payout_change()
    o, n = old.index("bank transfer"), new.index("through Wise")
    field = {"name": "prize_payout", "label": "Prize payout", "status": "changed",
             "old": {"value": "bank transfer", "spans": [[o, o + 13]]},
             "new": {"value": "through Wise", "spans": [[n, n + 12]]}}
    text = " ".join(ln["text"] for ln in summarize.offline_brief([ch], [field]))
    assert "tax form" in text and "through Wise" in text


def test_field_spanning_an_excluded_clause_is_left_out():
    a = {"id": "new:1.1", "status": "added", "materiality": "new", "section": "E", "rank": {"label": "act"},
         "new": {"text": "Residents of X are excluded.", "start": 0, "end": 28}}
    b = {"id": "new:1.2", "status": "added", "materiality": "new", "section": "E",
         "rank": {"label": "not_your_case"}, "new": {"text": "Residents of Y are excluded.", "start": 29, "end": 57}}
    field = {"name": "excluded", "label": "Excluded residents", "status": "added", "old": None,
             "new": {"value": ["X", "Y"], "spans": [[13, 14], [42, 43]]}}
    facts = summarize._facts([a, b], [field])
    assert facts["key_facts"] == []
    assert all("Y" not in ln["text"] for ln in summarize.offline_brief([a, b], [field]))


# --- snapshots ----------------------------------------------------------------------------------

def test_same_second_snapshots_do_not_overwrite(tmp_path):
    pages = iter(["## A\nTeams of six.\n", "## A\nTeams of four.\n"])
    client = FakeClient()   # every Clocked call fails -> incomplete, but the fetch must not be lost
    watch("https://x.test/r", tmp_path, client=client, fetch=lambda u: next(pages), now=lambda: 1000.25)
    code = watch("https://x.test/r", tmp_path, client=client, fetch=lambda u: next(pages), now=lambda: 1000.75)
    assert code == 4 and len(client.calls) > 0          # it really compared the two versions
    assert len(list(store_dir(tmp_path, "https://x.test/r").glob("*.txt"))) == 2


# --- rerank -------------------------------------------------------------------------------------

def test_long_edit_keeps_the_changed_region_in_view():
    prefix = "Shared text. " * 1200
    ch = {"id": "new:1", "status": "edited", "materiality": "material", "section": "S",
          "old": {"text": prefix + "Open to residents of every country."},
          "new": {"text": prefix + "Open to residents of every country except Quebec."}}
    text, cut, lost = rerank.passage(ch)
    assert cut and not lost and "except Quebec" in text and "every country." in text
    assert len(text) <= rerank.MAX_PASSAGE


def _base_client(rerank_resp):
    return FakeClient({
        "compare": {"pairs": []}, "extract": {"fields": {}}, "rerank": rerank_resp,
        "grounding": lambda p: {"grounded": 0.9, "claims": [{"verdict": "supported", "supported": 0.95} for _ in p["claims"]]},
    })


OLD, NEW = "## T\nTeams of up to six.\n", "## T\nTeams of up to four.\n"


def test_missing_rerank_results_are_not_an_all_clear():
    rep = analyze(OLD, NEW, client=_base_client({"results": []}), llm=None)
    assert rep["stages"]["rerank"]["status"] == "partial"
    assert rep["assessment"]["action"] == "unknown" and rep["assessment"]["complete"] is False
    assert exit_code(rep) == 4


def test_unknown_rerank_action_is_not_a_judgment():
    resp = lambda p: {"results": [{"id": d["id"], "action": "something_new"} for d in p["documents"]],
                      "answers": {"action_needed": {"noul": 0.1, "decision": False}}}
    rep = analyze(OLD, NEW, client=_base_client(resp), llm=None)
    assert rep["assessment"]["action"] == "unknown"


# --- usage accounting ---------------------------------------------------------------------------

def test_successful_calls_count_even_when_their_stage_fails():
    calls = {"n": 0}

    def extract(p):
        calls["n"] += 1
        if calls["n"] == 2:
            return ClockedError("down", status=503, endpoint="extract")
        return {"fields": {}}
    c = FakeClient({"compare": {"pairs": []}, "extract": extract,
                    "rerank": lambda p: {"results": [{"id": d["id"], "action": "use"} for d in p["documents"]]},
                    "grounding": lambda p: {"claims": [{"verdict": "supported", "supported": 0.9} for _ in p["claims"]]}})
    rep = analyze(OLD, NEW, client=c, llm=None)
    ex = rep["stages"]["extract"]
    assert ex["status"] == "failed" and ex["usage"]["calls"] == 1 and ex["usage"]["failed_calls"] == 1
    assert rep["usage"]["credits"] == sum(1.0 for _ in range(rep["usage"]["calls"]))


# --- rumour checker -----------------------------------------------------------------------------

def test_too_many_claims_are_refused_before_any_call():
    c = FakeClient()
    with pytest.raises(InputError):
        check_claims(OLD, NEW, [f"claim {i}" for i in range(MAX_CHECK_CLAIMS + 1)], client=c)
    assert c.calls == []


def test_not_a_claim_is_its_own_status_and_leaves_the_brief():
    assert verify.classify({"verdict": "not_a_claim", "supported": 0.9}) == "not_a_claim"
    c = FakeClient({"compare": {"pairs": []}, "extract": {"fields": {}},
                    "rerank": lambda p: {"results": [{"id": d["id"], "action": "use"} for d in p["documents"]]},
                    "grounding": lambda p: {"claims": [{"verdict": "not_a_claim"} for _ in p["claims"]]}})
    rep = analyze(OLD, NEW, client=c, llm=None)
    assert rep["brief"]["lines"] == [] and rep["brief"]["rejected"][0]["status"] == "not_a_claim"


# --- client -------------------------------------------------------------------------------------

def test_ledger_replay_header_is_recognised():
    def transport(url, body, headers, timeout):
        return RawResponse(200, b"{}", {"x-clocked-transport": "replay", "x-clocked-credits": "0"})
    _, usage = ClockedClient("k", transport=transport).call("compare", {})
    assert usage.ledger_replay is True and usage.replayed is False


# --- brief --------------------------------------------------------------------------------------

def test_unparseable_model_reply_is_retried_once():
    good = json.dumps({"lines": [{"text": "Team size limit is now four.", "change_ids": []}]})
    llm = FakeLLM(["Sure! Here is the brief:", good])
    c = _base_client(lambda p: {"results": [{"id": d["id"], "action": "use"} for d in p["documents"]]})
    rep = analyze(OLD, NEW, client=c, llm=llm)
    assert rep["brief"]["source"] == "fake:model" and len(llm.prompts) == 2
    assert "first reply unusable" in rep["stages"]["brief"]["note"]
