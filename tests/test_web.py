"""Pure browser helpers, exercised with Node when available; no browser dependency."""
import json
import shutil
import subprocess

import pytest

from conftest import ROOT


@pytest.fixture(scope="module")
def dom_module(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("Node is optional; needed only to test the pure JavaScript helper")
    path = tmp_path_factory.mktemp("web") / "dom.mjs"
    path.write_text((ROOT / "rule_radar/web/dom.js").read_text("utf-8"), encoding="utf-8")
    return path.as_uri()


@pytest.mark.parametrize("edit,expected", [
    ({}, {"rules": False, "profile": False, "claims": False}),
    ({"old": "Before. "}, {"rules": True, "profile": False, "claims": False}),
    ({"new": "Now.\n"}, {"rules": True, "profile": False, "claims": False}),
    ({"profile": {"country": "Canada", "team_size": "1"}},
     {"rules": False, "profile": True, "claims": False}),
    ({"profile": {"country": "Taiwan ", "team_size": "1"}},
     {"rules": False, "profile": True, "claims": False}),
    ({"profile": {"country": "Taiwan"}}, {"rules": False, "profile": True, "claims": False}),
    ({"profile": {"country": "Taiwan", "team_size": "1", "extra": "edited"}},
     {"rules": False, "profile": True, "claims": False}),
    ({"claims": ["Second rumour.", "First rumour.", "Second rumour."]},
     {"rules": False, "profile": False, "claims": False}),
    ({"claims": ["First rumour.", "First rumour!"]}, {"rules": False, "profile": False, "claims": True}),
])
def test_replay_differences(dom_module, edit, expected):
    sample = {"old": "Before.", "new": "Now.", "profile": {"country": "Taiwan", "team_size": "1"},
              "claims": ["First rumour.", "Second rumour."]}
    current = {**sample, "claims": [], **edit}
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         "const {replayDifferences} = await import(process.argv[1]); "
         "const {sample, current} = JSON.parse(process.argv[2]); "
         "process.stdout.write(JSON.stringify(replayDifferences(sample, current)));",
         dom_module, json.dumps({"sample": sample, "current": current})],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == expected


def test_replay_without_a_loaded_sample_is_unsupported(dom_module):
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         "const {replayDifferences} = await import(process.argv[1]); "
         "process.stdout.write(JSON.stringify(replayDifferences(null, "
         "{old: 'Before.', new: 'Now.', profile: {}, claims: ['A rumour.']})));", dom_module],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {"rules": True, "profile": True, "claims": True}
