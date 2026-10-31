from rule_radar.clauses import align, section_key, similarity, split_clauses


def test_offsets_are_exact(v1, v2):
    for doc, ver in ((v1, "old"), (v2, "new")):
        for c in split_clauses(doc, ver):
            assert doc[c.start:c.end] == c.text
            assert c.id.startswith(ver + ":")


def test_abbreviations_do_not_split_sentences():
    doc = "## Dates\nSubmissions close at 11:59 p.m. Pacific Time on Nov. 10. Winners follow e.g. by email.\n"
    texts = [c.text for c in split_clauses(doc, "new")]
    assert texts == ["Submissions close at 11:59 p.m. Pacific Time on Nov. 10.", "Winners follow e.g. by email."]


def test_sentence_after_year_is_split():
    doc = "The period ends November 20, 2026. Submissions close the same day."
    assert len(split_clauses(doc, "old")) == 2


def test_plain_numbered_headings_and_bullets():
    doc = "1. Prizes\nFirst place gets $500.\n\n2. Eligibility\n- Must be 18 or older.\n- One entry per person.\n"
    cl = split_clauses(doc, "new")
    assert [c.section for c in cl] == ["1. Prizes", "2. Eligibility", "2. Eligibility"]
    assert cl[1].text == "Must be 18 or older."


def test_headings_are_not_clauses():
    cl = split_clauses("# Title (version of May 1)\n\n## Teams\nUp to four people.", "new")
    assert [c.text for c in cl] == ["Up to four people."]


def test_section_key_ignores_numbering():
    assert section_key("Section 7 What to submit") == "what to submit"
    assert section_key("3. Teams") == section_key("12. Teams")


def test_align_sample(v1, v2):
    changes = align(split_clauses(v1, "old"), split_clauses(v2, "new"))
    by = {}
    for c in changes:
        by.setdefault(c.status, []).append(c)
    assert len(by["unchanged"]) >= 4
    edited = {c.new.text.split(" ")[0] for c in by["edited"]}
    assert "Submissions" in edited and "First" in edited
    added = [c.new.text for c in by["added"]]
    assert "Private repositories are not accepted." in added
    assert any("community award" in t for t in added)
    assert any("public or private" in c.old.text for c in by["removed"])


def test_renamed_section_still_pairs():
    old = split_clauses("## Prize money\nFirst place gets $500.", "old")
    new = split_clauses("## Prize monies\nFirst place gets $700.", "new")
    ch = align(old, new)
    assert [c.status for c in ch] == ["edited"]


def test_removed_section():
    old = split_clauses("## A\nKeep me.\n\n## B\nGone soon.", "old")
    new = split_clauses("## A\nKeep me.", "new")
    assert sorted(c.status for c in align(old, new)) == ["removed", "unchanged"]


def test_similarity_bounds():
    assert similarity("abc", "abc") == 1.0
    assert similarity("abc", "xyz") < 0.2
