import json

import pytest

from conftest import REPLAY, FakeClient, FakeLLM
from rule_radar.clocked import ClockedError, ReplayClient
from rule_radar.llm.base import LLMError
from rule_radar.llm.recorded import ReplayLLM
from rule_radar.pipeline import InputError, analyze, check_claims


def test_replay_run_with_template_brief(v1, v2, profile):
    rep = analyze(v1, v2, client=ReplayClient(REPLAY), llm=None, profile=profile)
    assert all(s["status"] in ("ok", "skipped") for s in rep["stages"].values()), rep["stages"]
    assert rep["usage"]["replayed"] and rep["usage"]["calls"] == 5
    assert rep["counts"]["material"] >= 8
    labels = {c["new"]["text"] if c["new"] else c["old"]["text"]: (c.get("rank") or {}).get("label")
              for c in rep["changes"]}
    assert labels["Private repositories are not accepted."] == "act"
    elig = next(t for t in labels if "except residents of Quebec" in t)
    assert labels[elig] == "not_your_case"            # a Quebec rule does not apply to an entrant in Taiwan
    assert rep["changes"][0]["rank"]["label"] == "act"
    assert rep["action_needed"]["decision"] is True
    deadline = next(f for f in rep["fields"] if f["name"] == "submission_deadline")
    assert deadline["status"] == "changed" and "November 14" in deadline["new"]["value"]
    s, e = deadline["new"]["spans"][0]
    assert v2[s:e] == deadline["new"]["value"]
    assert rep["brief"]["verified"] and rep["brief"]["source"] == "template"
    assert rep["brief"]["lines"]


def test_replay_run_with_recorded_model_brief(v1, v2, profile):
    rep = analyze(v1, v2, client=ReplayClient(REPLAY), llm=ReplayLLM(REPLAY), profile=profile)
    assert rep["brief"]["source"].startswith("replay:anthropic")
    assert rep["stages"]["verify"]["status"] == "ok"
    assert all(ln["status"] in ("verified", "check") for ln in rep["brief"]["lines"])


def test_every_stage_failing_still_returns_a_report(v1, v2):
    rep = analyze(v1, v2, client=FakeClient(), llm=None)
    st = rep["stages"]
    assert [st[k]["status"] for k in ("compare", "extract", "rerank", "verify")] == ["failed"] * 4
    assert st["verify"]["fallback"].endswith("NOT VERIFIED")
    assert rep["brief"]["verified"] is False
    assert {c["materiality"] for c in rep["changes"]} <= {"unconfirmed", "new", "removed"}
    assert rep["fields"] == []


def test_model_failure_falls_back_to_template(v1, v2):
    replay = ReplayClient(REPLAY)
    rep = analyze(v1, v2, client=FakeClient(fallback=replay), llm=FakeLLM([LLMError("boom")]),
                  profile=_profile())
    assert rep["stages"]["brief"]["status"] == "failed"
    assert rep["brief"]["source"] == "template"
    assert rep["stages"]["repair"]["status"] == "skipped"     # the failing model is not asked again


def _profile():
    from conftest import SAMPLES
    return json.loads((SAMPLES / "profile_solo_taiwan.json").read_text("utf-8"))


def _verify_handler(verdicts):
    """Fake /grounding: claim text -> verdict, so tests can drive the repair loop."""
    def handler(payload):
        out = []
        for c in payload["claims"]:
            v = verdicts.get(c, "supported")
            out.append({"verdict": v, "supported": 0.95 if v == "supported" else 0.02,
                        "evidence": {"document": "new", "quote": "Q", "start": 0, "end": 1}})
        return {"grounded": 0.5, "verdict": "x", "claims": out}
    return handler


def _brief(*texts):
    return json.dumps({"lines": [{"text": t, "change_ids": []} for t in texts]})


def test_rejected_line_is_repaired_and_reverified(v1, v2):
    fc = FakeClient({"grounding": _verify_handler({"Prize pool is $12,000.": "contradicted"})},
                    fallback=ReplayClient(REPLAY))
    llm = FakeLLM([_brief("Deadline moved to November 14.", "Prize pool is $12,000."),
                   json.dumps({"lines": [{"index": 1, "text": "Prize pool stays $10,000."}]})])
    rep = analyze(v1, v2, client=fc, llm=llm, profile=_profile())
    lines = rep["brief"]["lines"]
    assert [ln["text"] for ln in lines] == ["Deadline moved to November 14.", "Prize pool stays $10,000."]
    assert lines[1]["repaired_from"] == "Prize pool is $12,000."
    assert rep["brief"]["rejected"] == []
    assert rep["stages"]["repair"]["status"] == "ok"
    assert sum(1 for e, _ in fc.calls if e == "grounding") == 2


def test_repair_rejected_again_stays_out(v1, v2):
    fc = FakeClient({"grounding": _verify_handler({"Bad.": "contradicted", "Still bad.": "not_stated"})},
                    fallback=ReplayClient(REPLAY))
    llm = FakeLLM([_brief("Good.", "Bad."), json.dumps({"lines": [{"index": 1, "text": "Still bad."}]})])
    rep = analyze(v1, v2, client=fc, llm=llm, profile=_profile())
    assert [ln["text"] for ln in rep["brief"]["lines"]] == ["Good."]
    assert rep["brief"]["rejected"][0]["repair"] == "rewrite was rejected again"


def test_repair_is_bounded_to_one_round(v1, v2):
    fc = FakeClient({"grounding": _verify_handler({"Bad.": "contradicted", "Worse.": "contradicted"})},
                    fallback=ReplayClient(REPLAY))
    llm = FakeLLM([_brief("Bad."), json.dumps({"lines": [{"index": 0, "text": "Worse."}]})])
    analyze(v1, v2, client=fc, llm=llm, profile=_profile())
    assert len(llm.prompts) == 2
    assert sum(1 for e, _ in fc.calls if e == "grounding") == 2


def test_input_errors(v1):
    with pytest.raises(InputError):
        analyze(v1, v1, client=FakeClient())
    with pytest.raises(InputError):
        analyze("", v1, client=FakeClient())
    with pytest.raises(InputError):
        analyze(v1, "x" * 100_000, client=FakeClient())


def test_check_claims_one_request_per_claim(v1, v2):
    out = check_claims(v1, v2, ["The total prize pool grew to $12,000.", "Prizes are now paid through Wise."],
                       client=ReplayClient(REPLAY))
    assert [c["status"] for c in out["claims"]] == ["rejected", "verified"]
    assert out["usage"]["calls"] == 2


def test_check_claims_reports_a_failed_claim(v1, v2):
    fc = FakeClient({"grounding": ClockedError("busy", status=429, endpoint="grounding")})
    out = check_claims(v1, v2, ["x"], client=fc)
    assert out["claims"][0]["status"] == "unverified" and "busy" in out["claims"][0]["error"]
