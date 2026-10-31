from conftest import REPLAY
from rule_radar.clocked import ReplayClient
from rule_radar.snapshots import html_to_text, store_dir, watch


def test_html_to_text_keeps_headings_and_drops_scripts():
    html = ("<html><head><style>x{}</style></head><body><nav>Menu</nav><h2>1. Dates</h2>"
            "<p>Close on <b>May 1</b>.</p><script>evil()</script><ul><li>One</li><li>Two</li></ul></body></html>")
    t = html_to_text(html)
    assert "## 1. Dates" in t and "Close on May 1." in t and "- One" in t
    assert "evil" not in t and "Menu" not in t


def test_watch_first_same_and_changed(tmp_path, v1, v2, capsys):
    pages = iter([v1, v1, v2])
    clock = iter([1_000_000, 1_000_100, 1_000_200])
    args = dict(client=ReplayClient(REPLAY), profile=None, fetch=lambda url: next(pages), now=lambda: next(clock))
    url = "https://example.test/rules"
    assert watch(url, tmp_path, **args) == 0
    assert "first snapshot" in capsys.readouterr().out
    assert watch(url, tmp_path, **args) == 0
    assert "no change" in capsys.readouterr().out
    code = watch(url, tmp_path, **args)
    out = capsys.readouterr().out
    assert len(list(store_dir(tmp_path, url).glob("*.txt"))) == 2
    assert "Changes, most important first" in out
    assert code in (0, 3)   # replay has no profile-free rerank recording; the run still completes


def test_watch_fetch_error(tmp_path, capsys):
    def boom(url):
        raise OSError("dns")
    assert watch("https://x.test", tmp_path, client=None, fetch=boom) == 2
