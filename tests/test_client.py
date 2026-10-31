import json

import pytest

from rule_radar.clocked.client import ClockedClient, ClockedError, RawResponse


def make(responses, **kw):
    seen = []

    def transport(url, body, headers, timeout):
        seen.append((url, json.loads(body), dict(headers)))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    ids = iter(f"id-{i}" for i in range(100))
    c = ClockedClient("k-123", transport=transport, sleep=lambda s: None, new_id=lambda: next(ids), **kw)
    return c, seen


def ok(body, credits="4", cost="0.0008", ms="90"):
    return RawResponse(200, json.dumps(body).encode(), {"x-clocked-credits": credits, "x-clocked-cost-usd": cost,
                                                          "x-clocked-model-ms": ms})


def test_success_reads_usage_headers():
    c, seen = make([ok({"fields": {}})])
    data, usage = c.call("extract", {"text": "x", "fields": {"a": "b"}})
    assert data == {"fields": {}}
    assert (usage.credits, usage.cost_usd, usage.model_ms, usage.attempts) == (4.0, 0.0008, 90.0, 1)
    url, body, headers = seen[0]
    assert url.endswith("/clocked/extract")
    assert headers["Authorization"] == "Bearer k-123" and headers["Idempotency-Key"] == "id-0"


def test_retries_reuse_the_same_idempotency_key():
    c, seen = make([RawResponse(503, b'{"error":"busy"}'), RawResponse(429, b"{}"), ok({"a": 1})])
    data, usage = c.call("compare", {"documents": []})
    assert data == {"a": 1} and usage.attempts == 3
    assert {h["Idempotency-Key"] for _, _, h in seen} == {"id-0"}
    assert len({json.dumps(b, sort_keys=True) for _, b, _ in seen}) == 1


def test_network_error_is_retried():
    c, seen = make([OSError("reset"), ok({"ok": True})])
    assert c.call("rerank", {})[0] == {"ok": True}
    assert len(seen) == 2


@pytest.mark.parametrize("status,kind", [(400, "invalid_request"), (401, "auth"), (402, "credits"),
                                         (403, "auth"), (422, "invalid_request")])
def test_client_errors_are_not_retried(status, kind):
    c, seen = make([RawResponse(status, b'{"error":"nope"}'), ok({})])
    with pytest.raises(ClockedError) as e:
        c.call("grounding", {})
    assert len(seen) == 1 and e.value.status == status and e.value.kind == kind
    assert "nope" in str(e.value)


def test_retry_as_new_stops_retries():
    c, seen = make([RawResponse(503, b'{"error":"ended","retryAsNew":true}'), ok({})])
    with pytest.raises(ClockedError) as e:
        c.call("compare", {})
    assert e.value.retry_as_new and len(seen) == 1


def test_gives_up_after_max_attempts():
    c, seen = make([RawResponse(500, b"oops")] * 3, max_attempts=3)
    with pytest.raises(ClockedError) as e:
        c.call("compare", {})
    assert len(seen) == 3 and e.value.kind == "service"


def test_invalid_json_body_is_an_error():
    c, _ = make([RawResponse(200, b"<html>")])
    with pytest.raises(ClockedError):
        c.call("compare", {})


def test_missing_key_is_rejected():
    with pytest.raises(ValueError):
        ClockedClient("")
