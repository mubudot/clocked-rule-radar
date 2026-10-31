from conftest import FakeClient
from rule_radar.snapshots import html_to_text, store_dir, watch


def test_html_to_text_keeps_headings_and_drops_scripts():
    html = ("<html><head><style>x{}</style></head><body><nav>Menu</nav><h2>1. Dates</h2>"
            "<p>Close on <b>May 1</b>.</p><script>evil()</script><ul><li>One</li><li>Two</li></ul></body></html>")
    t = html_to_text(html)
    assert "## 1. Dates" in t and "Close on May 1." in t and "- One" in t
    assert "evil" not in t and "Menu" not in t


OLD = "## Teams\nTeams may have up to six people.\n"
NEW = "## Teams\nTeams may have up to four people.\n"


def _ok_client():
    def compare(p):
        o, n = p["documents"][0]["text"], p["documents"][1]["text"]
        return {"pairs": [{"a": "old", "b": "new", "duplicates": [], "conflicts": [{
            "kind": "value", "confidence": 0.99,
            "a": {"start": o.index("six"), "end": o.index("six") + 3},
            "b": {"start": n.index("four"), "end": n.index("four") + 4}}]}]}
    return FakeClient({
        "compare": compare,
        "extract": {"fields": {}},
        "rerank": lambda p: {"results": [{"id": d["id"], "action": "use", "relevance_score": 0.9} for d in p["documents"]],
                             "answers": {"action_needed": {"noul": 0.9, "decision": True}}},
        "grounding": lambda p: {"grounded": 0.9, "claims": [{"verdict": "supported", "supported": 0.95} for _ in p["claims"]]},
    })


def _run(tmp_path, pages, client, clock):
    return watch("https://example.test/rules", tmp_path, client=client, fetch=lambda url: next(pages),
                 now=lambda: next(clock))


def test_watch_first_same_changed_then_settled(tmp_path, capsys):
    pages, clock = iter([OLD, OLD, NEW, NEW]), iter(range(1_000_000, 1_000_100, 10))
    c = _ok_client()
    assert _run(tmp_path, pages, c, clock) == 0 and "first snapshot" in capsys.readouterr().out
    assert _run(tmp_path, pages, c, clock) == 0 and "no change" in capsys.readouterr().out
    assert _run(tmp_path, pages, c, clock) == 3
    assert "Changes, most important first" in capsys.readouterr().out
    assert _run(tmp_path, pages, c, clock) == 0          # baseline advanced after a complete analysis
    d = store_dir(tmp_path, "https://example.test/rules")
    assert len(list(d.glob("*.txt"))) == 2


def test_failed_analysis_keeps_the_baseline(tmp_path, capsys):
    pages, clock = iter([OLD, NEW, NEW]), iter(range(1_000_000, 1_000_100, 10))
    assert _run(tmp_path, pages, _ok_client(), clock) == 0
    assert _run(tmp_path, pages, FakeClient(), clock) == 4                 # every Clocked call fails
    assert "keeping baseline" in capsys.readouterr().out
    assert _run(tmp_path, pages, _ok_client(), clock) == 3                  # the comparison is retried
    d = store_dir(tmp_path, "https://example.test/rules")
    assert len(list(d.glob("*.txt"))) == 2


def test_watch_fetch_error(tmp_path):
    def boom(url):
        raise OSError("dns")
    assert watch("https://x.test", tmp_path, client=None, fetch=boom) == 2
