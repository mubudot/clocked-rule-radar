import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import REPLAY
from rule_radar.config import Settings
from rule_radar.server import App, make_handler


@pytest.fixture(scope="module")
def base():
    settings = Settings.from_env({"CLOCKED_MODE": "replay", "RADAR_REPLAY_DIR": str(REPLAY),
                                  "CLOCKED_API_KEY": "never-shown-key"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(settings)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # talk to localhost directly


def get(url):
    try:
        with OPENER.open(url, timeout=30) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def post(url, data, ctype="application/json"):
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    try:
        with OPENER.open(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_index_and_security_headers(base):
    status, headers, body = get(base + "/")
    assert status == 200 and b"Rule Change Radar" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert get(base + "/app.js")[0] == 200


def test_config_never_contains_the_key(base):
    status, _, body = get(base + "/api/config")
    assert status == 200 and b"never-shown-key" not in body
    assert json.loads(body)["mode"] == "replay"


def test_path_traversal_is_refused(base):
    assert get(base + "/../config.py")[0] == 404
    assert get(base + "/%2e%2e/server.py")[0] == 404
    assert get(base + "/api/samples/..%2fx")[0] == 404


def test_sample_and_analyze_in_replay(base):
    status, _, body = get(base + "/api/samples/northwind")
    s = json.loads(body)
    status, rep = post(base + "/api/analyze", {"old": s["old"], "new": s["new"], "profile": s["profile"]})
    assert status == 200 and rep["usage"]["replayed"]
    assert rep["changes"][0]["rank"]["label"] == "act"


def test_check_claims(base):
    s = json.loads(get(base + "/api/samples/northwind")[2])
    status, out = post(base + "/api/check", {"old": s["old"], "new": s["new"], "claims": s["claims"][:1]})
    assert status == 200 and out["claims"][0]["status"] == "rejected"


@pytest.mark.parametrize("payload,code", [
    ({"old": "a"}, 400), ({"old": "same", "new": "same"}, 400), ({"old": 1, "new": "b"}, 400),
    ([1, 2], 400), (b"{not json", 400)])
def test_bad_requests(base, payload, code):
    assert post(base + "/api/analyze", payload)[0] == code


def test_wrong_content_type_and_unknown_route(base):
    assert post(base + "/api/analyze", b"{}", ctype="text/plain")[0] == 415
    assert post(base + "/api/nope", {})[0] == 404


def test_unknown_text_in_replay_degrades_instead_of_crashing(base):
    status, rep = post(base + "/api/analyze", {"old": "## A\nTeams of six.", "new": "## A\nTeams of four."})
    assert status == 200
    assert rep["stages"]["compare"]["status"] == "failed" and rep["brief"]["verified"] is False
