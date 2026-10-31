from rule_radar.clauses import align, split_clauses
from rule_radar.stages import compare, extract, rerank, verify


def _changes(old, new):
    return align(split_clauses(old, "old"), split_clauses(new, "new"))


OLD = "## Dates\nSubmissions close on May 1.\n\n## Teams\nTeams may have up to six people.\n"
NEW = "## Dates\nSubmissions now close on May 1.\n\n## Teams\nTeams may have up to four people.\n"


def finding(old_doc, new_doc, old_q, new_q, kind="value", conf=0.99):
    a, b = old_doc.index(old_q), new_doc.index(new_q)
    return {"kind": kind, "confidence": conf,
            "a": {"document": "old", "quote": old_q, "start": a, "end": a + len(old_q)},
            "b": {"document": "new", "quote": new_q, "start": b, "end": b + len(new_q)}}


def test_compare_marks_conflict_material_and_restatement_wording():
    resp = {"pairs": [{"a": "old", "b": "new",
                       "conflicts": [finding(OLD, NEW, "up to six people", "up to four people")],
                       "duplicates": [finding(OLD, NEW, "Submissions close on May 1.", "Submissions now close on May 1.",
                                              kind="restated")]}]}
    old_cl, new_cl = split_clauses(OLD, "old"), split_clauses(NEW, "new")
    out = {c["section"]: c for c in compare.interpret(resp, align(old_cl, new_cl), old_cl, new_cl)}
    assert out["Teams"]["materiality"] == "material"
    assert out["Teams"]["compare"]["kind"] == "value"
    assert out["Dates"]["materiality"] == "wording"


def test_compare_handles_swapped_pair_order():
    f = finding(OLD, NEW, "up to six people", "up to four people")
    swapped = {"kind": "value", "confidence": 0.9, "a": f["b"], "b": f["a"]}
    resp = {"pairs": [{"a": "new", "b": "old", "conflicts": [swapped], "duplicates": []}]}
    old_cl, new_cl = split_clauses(OLD, "old"), split_clauses(NEW, "new")
    out = {c["section"]: c for c in compare.interpret(resp, align(old_cl, new_cl), old_cl, new_cl)}
    assert out["Teams"]["materiality"] == "material"


def test_compare_fallback_marks_edits_unconfirmed():
    out = compare.local_only(_changes(OLD, NEW))
    assert {c["materiality"] for c in out} == {"unconfirmed"}


def test_compare_ignores_bad_offsets():
    resp = {"pairs": [{"a": "old", "b": "new", "conflicts": [
        {"kind": "value", "a": {"start": "x", "end": None}, "b": {"start": 5, "end": 2}}]}]}
    old_cl, new_cl = split_clauses(OLD, "old"), split_clauses(NEW, "new")
    out = compare.interpret(resp, align(old_cl, new_cl), old_cl, new_cl)
    assert {c["materiality"] for c in out} == {"unconfirmed"}


def test_added_clause_with_conflict_is_material():
    old = "## Work\nOld projects are allowed."
    new = "## Work\nOld projects are allowed.\nOld projects are not eligible after all."
    resp = {"pairs": [{"a": "old", "b": "new", "conflicts": [
        finding(old, new, "Old projects are allowed.", "Old projects are not eligible after all.")]}]}
    o, n = split_clauses(old, "old"), split_clauses(new, "new")
    out = compare.interpret(resp, align(o, n), o, n)
    assert out[0]["status"] == "added" and out[0]["materiality"] == "material"
    assert out[0]["conflicts_with"] == ["old:1.1"]


def test_extract_interpret_and_diff():
    text_old, text_new = "Deadline: May 1. Teams: six.", "Deadline: May 9. Teams: six."
    ro = {"fields": {"submission_deadline": {"value": "May 1", "start": 10, "end": 15, "confidence": 0.99},
                     "max_team_size": {"value": "six", "start": 24, "end": 27, "confidence": 0.4},
                     "prize_pool": {"value": None, "confidence": 1}}}
    rn = {"fields": {"submission_deadline": {"value": "May 9", "start": 10, "end": 15, "confidence": 0.99},
                     "max_team_size": {"value": "Six", "start": 24, "end": 27, "confidence": 0.99},
                     "prize_pool": {"value": "$5", "start": 999, "end": 1001, "confidence": 0.9},
                     "excluded_residents": {"value": ["Quebec"], "items": [{"start": 0, "end": 3}], "confidence": 1}}}
    fo, fn = extract.interpret(ro, text_old), extract.interpret(rn, text_new)
    assert fo["submission_deadline"].spans == [(10, 15)]
    assert fn["prize_pool"].spans == []           # offset outside the text is dropped
    rows = {r["name"]: r for r in extract.diff_fields(fo, fn)}
    assert rows["submission_deadline"]["status"] == "changed"
    assert rows["max_team_size"]["status"] == "same" and rows["max_team_size"]["uncertain"]
    assert rows["prize_pool"]["status"] == "added"
    assert rows["excluded_residents"]["status"] == "added"
    assert rows["video_limit"]["status"] == "not_stated"


def test_extract_payload_has_all_fields():
    p = extract.build_payload("text")
    assert set(p["fields"]) == set(extract.DEFAULT_FIELDS)


def test_rerank_payload_skips_wording_and_carries_profile():
    changes = [{"id": "new:1.1", "status": "edited", "section": "Dates", "materiality": "material",
                "old": {"text": "May 1"}, "new": {"text": "May 9"}},
               {"id": "new:1.2", "status": "edited", "section": "Dates", "materiality": "wording",
                "old": {"text": "a"}, "new": {"text": "b"}}]
    p = rerank.build_payload(changes, {"country": "Taiwan", "empty": ""})
    assert [d["id"] for d in p["documents"]] == ["new:1.1"]
    assert p["context"] == {"country": "Taiwan"}
    assert "Before: May 1 Now: May 9" in p["documents"][0]["text"]
    assert rerank.build_payload(changes[1:], None) is None


def test_rerank_interpret_and_order():
    resp = {"results": [
        {"id": "b", "action": "use", "relevance_score": 0.9},
        {"id": "c", "action": "skip", "reason": "different_case", "relevance_score": 0.1},
        {"id": "a", "action": "expand", "relevance_score": 0.2}],
        "answers": {"action_needed": {"noul": 0.93, "decision": True}}}
    ranks, verdict = rerank.interpret(resp)
    assert ranks["b"]["label"] == "act" and ranks["c"]["label"] == "not_your_case"
    assert ranks["a"]["label"] == "read_section"
    assert verdict == {"probability": 0.93, "decision": True}
    changes = [{"id": x, "materiality": "material"} for x in ("a", "b", "c", "d")]
    assert [c["id"] for c in rerank.order(changes, ranks)] == ["b", "a", "c", "d"]


def test_verify_classify_and_interpret():
    resp = {"grounded": 0.7, "verdict": "contradicted", "claims": [
        {"verdict": "supported", "supported": 0.95},
        {"verdict": "supported", "supported": 0.6},
        {"verdict": "contradicted", "supported": 0.01, "evidence": {"document": "new", "quote": "q", "start": 0, "end": 1}},
        {"verdict": "not_stated", "supported": 0.02}]}
    out = verify.interpret(resp, ["a", "b", "c", "d", "e"])
    assert [c["status"] for c in out["claims"]] == ["verified", "check", "rejected", "rejected", "unverified"]
    assert out["claims"][2]["evidence"]["quote"] == "q"


def test_verify_payload_limits():
    p = verify.build_payload("o", "n", [" a ", "", "b"] + ["x"] * 40)
    assert p["claims"][:2] == ["a", "b"] and len(p["claims"]) == verify.MAX_CLAIMS
    assert [d["id"] for d in p["documents"]] == ["old", "new"]
