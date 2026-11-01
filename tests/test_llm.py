import json

import pytest

from rule_radar import summarize
from rule_radar.config import Settings
from rule_radar.llm.anthropic_messages import AnthropicLLM
from rule_radar.llm.base import HttpResult, LLMError
from rule_radar.llm.openai_compat import OpenAICompatLLM
from rule_radar.llm.recorded import RecordingLLM, ReplayLLM


class Post:
    def __init__(self, status, body):
        self.status, self.body, self.seen = status, body, []

    def __call__(self, url, body, headers, timeout):
        self.seen.append((url, json.loads(body), headers))
        return HttpResult(self.status, json.dumps(self.body).encode() if not isinstance(self.body, bytes) else self.body)


def test_openai_format():
    p = Post(200, {"choices": [{"message": {"content": "hi"}}]})
    llm = OpenAICompatLLM("gpt-x", "sk-1", "http://localhost:11434/v1/", post=p)
    assert llm.complete("sys", "usr") == "hi"
    url, body, headers = p.seen[0]
    assert url == "http://localhost:11434/v1/chat/completions"
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
    assert "temperature" not in body and headers["authorization"] == "Bearer sk-1"


def test_openai_temperature_and_no_key():
    p = Post(200, {"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"text": "b"}]}}]})
    llm = OpenAICompatLLM("m", "", temperature=0.2, post=p)
    assert llm.complete("s", "u") == "ab"
    assert p.seen[0][1]["temperature"] == 0.2 and "authorization" not in p.seen[0][2]


def test_anthropic_format_and_auth_modes():
    p = Post(200, {"content": [{"type": "text", "text": "x"}, {"type": "thinking", "thinking": "..."}]})
    assert AnthropicLLM("claude-x", "ak", post=p).complete("sys", "usr") == "x"
    url, body, headers = p.seen[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert body["system"] == "sys" and body["messages"] == [{"role": "user", "content": "usr"}]
    assert headers["x-api-key"] == "ak" and headers["anthropic-version"]
    p2 = Post(200, {"content": [{"type": "text", "text": "y"}]})
    AnthropicLLM("c", "tok", "https://gw.example", "bearer", post=p2).complete("s", "u")
    assert p2.seen[0][2]["authorization"] == "Bearer tok" and "x-api-key" not in p2.seen[0][2]


def test_http_error_masks_the_key():
    p = Post(401, {"error": "bad key sk-secret-1"})
    with pytest.raises(LLMError) as e:
        OpenAICompatLLM("m", "sk-secret-1", post=p).complete("s", "u")
    assert "sk-secret-1" not in str(e.value) and "401" in str(e.value)


@pytest.mark.parametrize("body", [{"choices": []}, {"content": "nope"}, b"<html>"])
def test_malformed_responses(body):
    with pytest.raises(LLMError):
        OpenAICompatLLM("m", post=Post(200, body)).complete("s", "u")
    with pytest.raises(LLMError):
        AnthropicLLM("m", post=Post(200, body)).complete("s", "u")


def test_bad_settings():
    with pytest.raises(ValueError):
        AnthropicLLM("m", auth="cookie")
    with pytest.raises(ValueError):
        OpenAICompatLLM("")
    with pytest.raises(ValueError):
        Settings.from_env({"RADAR_LLM_PROVIDER": "gemini"})


def test_settings_defaults_and_public_view():
    s = Settings.from_env({})
    assert s.mode == "replay" and s.llm_provider == "offline" and s.model() is not None  # replay wrapper
    s = Settings.from_env({"CLOCKED_API_KEY": "ck", "RADAR_LLM_PROVIDER": "openai", "RADAR_LLM_MODEL": "m",
                           "OPENAI_API_KEY": "ok", "RADAR_LLM_TEMPERATURE": "0"})
    assert s.mode == "live" and s.llm_api_key == "ok" and s.llm_temperature == 0.0
    assert isinstance(s.model(), OpenAICompatLLM)
    pub = json.dumps(s.public())
    assert "ck" not in pub.replace("clocked_key_set", "") and "ok" not in pub.replace("llm_", "")


def test_record_then_replay_llm(tmp_path):
    class Inner:
        name = "x:y"
        n = 0

        def complete(self, s, u):
            Inner.n += 1
            return f"reply {u}"
    rec = RecordingLLM(Inner(), tmp_path)
    assert rec.complete("s", "u1") == "reply u1"
    assert rec.complete("s", "u1") == "reply u1" and Inner.n == 1     # second call is served from disk
    rep = ReplayLLM(tmp_path)
    assert rep.complete("s", "u1") == "reply u1" and rep.name == "replay:x:y"
    with pytest.raises(LLMError):
        rep.complete("s", "unknown")


def test_brief_parsing_keeps_only_known_ids():
    changes = [{"id": "new:1.1", "materiality": "material", "status": "edited", "section": "S",
                "old": {"text": "a"}, "new": {"text": "b"}, "rank": {"label": "act"}}]
    class L:
        name = "l"
        def complete(self, s, u):
            return 'Sure!\n```json\n{"lines": [{"text": " One. ", "change_ids": ["new:1.1", "bogus"]}, {"text": ""}]}\n```'
    lines, src = summarize.write_brief(changes, [], L())
    assert lines == [{"text": "One.", "change_ids": ["new:1.1"]}] and src == "l"


def test_brief_without_json_is_an_error():
    class L:
        name = "l"
        def complete(self, s, u):
            return "I cannot help."
    with pytest.raises(LLMError):
        summarize.write_brief([], [], L())


_CHANGE = [{"id": "new:1.1", "materiality": "material", "status": "edited", "section": "S",
            "old": {"text": "a"}, "new": {"text": "b"}, "rank": {"label": "act"}}]


def _llm(reply):
    class L:
        name = "l"
        def complete(self, s, u):
            return reply
    return L()


def test_brief_quoting_the_prompt_shape_then_answering_uses_the_answer():
    # Seen live: a model thinks aloud, quotes the shape from the prompt, then answers.
    reply = ('Reply with JSON in the shape {"lines": [{"text": "...", "change_ids": ["new:2.1"]}]}.\n'
             'Here it is:\n{"lines": [{"text": "Deadline moved.", "change_ids": ["new:1.1"]}]}')
    lines, _ = summarize.write_brief(_CHANGE, [], _llm(reply))
    assert lines == [{"text": "Deadline moved.", "change_ids": ["new:1.1"]}]


@pytest.mark.parametrize("reply", [
    # truncated thinking that only ever quotes the shape: the placeholder line is never published
    'The shape is {"lines": [{"text": "...", "change_ids": ["new:2.1"]}]} and the changes are',
    '{"lines": [{"text": "\u2026"}, {"text": " . . "}]}',
])
def test_brief_with_only_placeholder_lines_is_an_error(reply):
    with pytest.raises(LLMError) as e:
        summarize.write_brief(_CHANGE, [], _llm(reply))
    assert e.value.retryable


def test_repair_ignores_a_quoted_placeholder():
    rej = [{"index": 0, "text": "x", "verdict": "not_stated", "evidence": None}]
    reply = 'Shape: {"lines": [{"index": 0, "text": "..."}]} Answer: {"lines": [{"index": 0, "text": "Fixed."}]}'
    assert summarize.repair(rej, _llm(reply)) == {0: "Fixed."}
    # an answer followed by the quoted shape still uses the answer
    trailing = '{"lines": [{"index": 0, "text": "Fixed."}]}\nShape was {"lines": [{"index": 0, "text": "..."}]}'
    assert summarize.repair(rej, _llm(trailing)) == {0: "Fixed."}
    # only the quoted shape: an error (the repair stage reports it), not a silent "0 lines dropped"
    with pytest.raises(LLMError):
        summarize.repair(rej, _llm('Shape: {"lines": [{"index": 0, "text": "..."}]} and then'))
    # a deliberate drop is still a drop
    assert summarize.repair(rej, _llm('{"lines": [{"index": 0, "text": null}]}')) == {0: None}


def test_brief_answer_followed_by_quoted_shape_uses_the_answer():
    reply = '{"lines": [{"text": "Deadline moved.", "change_ids": ["new:1.1"]}]}\nShape: {"lines": [{"text": "..."}]}'
    lines, _ = summarize.write_brief(_CHANGE, [], _llm(reply))
    assert lines == [{"text": "Deadline moved.", "change_ids": ["new:1.1"]}]


def test_offline_brief_skips_other_cases():
    changes = [{"id": "new:2.1", "status": "added", "materiality": "new", "section": "Award",
                "new": {"text": "A $500 award."}, "rank": {"label": "not_your_case"}},
               {"id": "new:3.1", "status": "added", "materiality": "new", "section": "Repo",
                "new": {"text": "Repos must be public."}, "rank": {"label": "act"}}]
    lines = summarize.offline_brief(changes, [])
    assert [ln["text"] for ln in lines] == ["New rule in Repo: Repos must be public."]
