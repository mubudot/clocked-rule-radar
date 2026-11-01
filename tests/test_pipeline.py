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
    assert rep["usage"]["calls"] == 5 and rep["usage"]["credits"] == 44
    assert rep["counts"] == {"material": 12, "new": 2, "wording": 1}
    assert rep["stages"]["compare"]["outcomes"] == {"material": 12, "wording": 1, "unconfirmed": 0}
    assert rep["stages"]["extract"]["outcomes"] == {
        "old": {"found": 10, "not_stated": 1, "low_confidence": 0},
        "new": {"found": 11, "not_stated": 0, "low_confidence": 0},
    }
    assert rep["stages"]["rerank"]["outcomes"] == {
        "act": 10, "read_section": 0, "low_impact": 3, "not_your_case": 1, "not_judged": 0,
    }
    assert rep["stages"]["verify"]["outcomes"] == {"supported": 7, "weak": 1, "pulled": 0, "not_judged": 0}
    weak = [ln for ln in rep["brief"]["lines"] if ln["status"] == "check"]
    assert len(rep["brief"]["lines"]) == 8 and rep["brief"]["rejected"] == []
    assert [(ln["text"], ln["supported"]) for ln in weak] == [
        ("Demo video limit shortened from five minutes to three minutes.", 0.6976)]


def test_every_stage_failing_still_returns_a_report(v1, v2):
    rep = analyze(v1, v2, client=FakeClient(), llm=None)
    st = rep["stages"]
    assert [st[k]["status"] for k in ("compare", "extract", "rerank", "verify")] == ["failed"] * 4
    assert st["verify"]["fallback"].endswith("NOT VERIFIED")
    assert rep["brief"]["verified"] is False
    assert {c["materiality"] for c in rep["changes"]} <= {"unconfirmed", "new", "removed"}
    assert rep["fields"] == []
    assert st["compare"]["outcomes"]["unconfirmed"] > 0
    assert st["extract"]["outcomes"] == {"old": None, "new": None}
    assert st["rerank"]["outcomes"]["not_judged"] == len(rep["changes"])
    assert st["verify"]["outcomes"] == {
        "supported": 0, "weak": 0, "pulled": 0, "not_judged": len(rep["brief"]["lines"]),
    }
    for name in ("compare", "extract", "rerank", "verify"):
        assert st[name]["usage"]["calls"] == 0
        assert st[name]["usage"]["failed_calls"] >= 1
        assert st[name]["usage"]["credits"] == 0


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
    # The Verify outcome describes the initial gate, even after repair accepts a rewrite.
    assert rep["stages"]["verify"]["outcomes"] == {"supported": 1, "weak": 0, "pulled": 1, "not_judged": 0}
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


def test_extract_outcomes_distinguish_missing_and_low_confidence_per_version():
    old, new = "## T\nTeams of six.\n", "## T\nTeams of four.\n"
    fc = FakeClient({
        "extract": lambda p: {"fields": {
            "max_team_size": {"value": "six" if p["text"] == old else "four",
                              "confidence": 0.59 if p["text"] == old else 0.6},
            "excluded_residents": {"value": [], "confidence": 0.1},
            "prize_pool": {"value": None, "confidence": 0.1},
            "video_limit": {"value": "", "confidence": 0.1},
        }},
    })
    rep = analyze(old, new, client=fc)
    assert rep["stages"]["extract"]["status"] == "ok"
    assert rep["stages"]["extract"]["outcomes"] == {
        "old": {"found": 1, "not_stated": 10, "low_confidence": 1},
        "new": {"found": 1, "not_stated": 10, "low_confidence": 0},
    }


def test_partial_verify_outcomes_keep_status_and_usage(v1, v2, profile):
    fc = FakeClient({"grounding": {"claims": [
        {"verdict": "supported", "supported": 0.8},
        {"verdict": "supported", "supported": 0.7999},
        {"verdict": "contradicted"},
        {"verdict": "not_stated"},
        {"verdict": "not_a_claim"},
    ]}}, fallback=ReplayClient(REPLAY))
    rep = analyze(v1, v2, client=fc, profile=profile,
                  llm=FakeLLM([_brief("Strong.", "Weak.", "Wrong.", "Absent.", "Opinion.", "Unjudged.")]),
                  do_repair=False)
    stage = rep["stages"]["verify"]
    assert stage["outcomes"] == {"supported": 1, "weak": 1, "pulled": 3, "not_judged": 1}
    assert stage["status"] == "partial" and "no judgment" in stage["note"]
    assert stage["usage"]["calls"] == 1 and stage["usage"]["credits"] == 1
    assert rep["brief"]["partial"] and not rep["assessment"]["complete"]
    assert [ln["status"] for ln in rep["brief"]["lines"]] == ["verified", "check", "unverified"]
