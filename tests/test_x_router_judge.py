"""x-router judge: the rubric, its parser, and the two backends behind it.

Critical paths only: scores combine as the rubric says and garbage is refused
rather than averaged in; the prompt is bounded; each backend speaks its
protocol and fails loudly; the config table picks the right one. No model.
"""

from __future__ import annotations

import json

import pytest  # type: ignore[import-not-found]

from openjiuwen.x_router import (
    ApiJudgeBackend,
    JudgeError,
    JudgeRequest,
    LocalJudgeBackend,
    ParamsError,
    build_judge,
    build_judge_request,
    judge_from_config,
    parse_judge_score,
)
from openjiuwen.x_router import judge as judge_module

REQUEST = JudgeRequest(system_prompt="rubric", user_prompt="score this")


def test_parse_combines_the_rubric_and_refuses_garbage():
    assert parse_judge_score('{"task_progress": 1, "correctness": 0, "grounding": -1}') == pytest.approx(0.25)
    # Partial rubric renormalises; out-of-range and boolean fields are ignored.
    assert parse_judge_score('{"task_progress": 2, "correctness": 0.5, "grounding": true}') == pytest.approx(0.5)
    # Found inside noise; a bare number is the fallback.
    assert parse_judge_score('Sure!\n```json\n{"task_progress": 0.5, "correctness": 0.5, "grounding": 0.5}\n```') == pytest.approx(0.5)
    assert parse_judge_score("Overall: -0.5") == -0.5
    for garbage in ["Overall: 7", "I cannot evaluate this.", '{"task_progress": 2}', ""]:
        with pytest.raises(ValueError):
            parse_judge_score(garbage)


def test_request_frames_the_turn_and_stays_bounded():
    request = build_judge_request(
        [{"role": "user", "content": [{"type": "text", "text": "task: " + "x" * 5000}]}],
        "y" * 5000,
        tool_calls=[{"name": "write_file", "arguments": {"blob": "z" * 5000}}],
        tools=[{"type": "function", "function": {"name": "write_file"}}],
    )
    prompt = request.user_prompt
    transcript = prompt.split("<transcript>\n", 1)[1].split("\n</transcript>", 1)[0]
    turn = prompt.split("<assistant_turn>\n", 1)[1].split("\n</assistant_turn>", 1)[0]
    assert transcript.startswith("[user]: task: ") and "...[truncated]..." in transcript
    assert len(transcript) <= judge_module.TRANSCRIPT_CHARS
    response, tool_line = turn.split("\nTOOL CALLS: ", 1)
    assert len(response) <= judge_module.RESPONSE_CHARS and len(tool_line) <= judge_module.TOOL_CALLS_CHARS
    assert "AVAILABLE TOOLS: write_file" in prompt


class FakeEngine:
    def __init__(self, reply='{"task_progress": 1, "correctness": 1, "grounding": 1}'):
        self.reply = reply
        self.loaded = 0
        self.prompts = []
        self.max_new_tokens = None

    def load(self):
        self.loaded += 1

    def build_prompt(self, messages):
        return json.dumps(messages)

    def generate(self, prompt, max_new_tokens=16, temperature=0.0):
        self.prompts.append(prompt)
        self.max_new_tokens = max_new_tokens
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_local_backend_renders_both_turns_with_a_judge_sized_budget_and_fails_loudly():
    engine = FakeEngine()
    judge = LocalJudgeBackend(engine=engine)
    assert parse_judge_score(judge.score(REQUEST)) == pytest.approx(1.0)
    assert json.loads(engine.prompts[-1]) == [{"role": "system", "content": "rubric"},
                                               {"role": "user", "content": "score this"}]
    assert engine.max_new_tokens == 64, "a rubric JSON does not fit the classifier's 16 tokens"
    with pytest.raises(JudgeError, match="cuda gone"):
        LocalJudgeBackend(engine=FakeEngine(RuntimeError("cuda gone"))).score(REQUEST)


class FakeTransport:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body.decode("utf-8")), timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode("utf-8")


def test_api_backend_speaks_openai_chat_completions_and_fails_loudly(monkeypatch):
    monkeypatch.setenv("JUDGE_KEY", "sk-test")
    transport = FakeTransport({"choices": [{"message": {"content": '{"task_progress": 0, "correctness": 1, "grounding": 1}'}}]})
    judge = ApiJudgeBackend("https://openrouter.ai/api/v1/", "z-ai/glm-4.7", api_key_env="JUDGE_KEY",
                            timeout_secs=12, max_tokens=48, transport=transport)
    assert parse_judge_score(judge.score(REQUEST)) == pytest.approx(0.55)
    url, headers, body, timeout = transport.calls[-1]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert body == {"model": "z-ai/glm-4.7", "temperature": 0, "max_tokens": 48,
                    "messages": [{"role": "system", "content": "rubric"}, {"role": "user", "content": "score this"}]}
    assert timeout == 12.0

    for bad in [b"not json", {"error": "rate limited"}, {"choices": []}, JudgeError("HTTP 503")]:
        with pytest.raises(JudgeError):
            ApiJudgeBackend("http://judge", "m", api_key="k", transport=FakeTransport(bad)).score(REQUEST)
    monkeypatch.delenv("MISSING_JUDGE_KEY", raising=False)
    with pytest.raises(ParamsError, match="MISSING_JUDGE_KEY"):
        ApiJudgeBackend("http://judge", "m", api_key_env="MISSING_JUDGE_KEY")


def test_config_selects_the_backend_and_build_judge_warms_it(monkeypatch):
    monkeypatch.setenv("JUDGE_KEY", "sk")

    class Classifier:
        engine = FakeEngine()

    api = judge_from_config({"kind": "api", "base_url": "http://judge", "model": "m", "api_key_env": "JUDGE_KEY"})
    assert isinstance(api, ApiJudgeBackend)
    shared = judge_from_config({"kind": "local", "share_classifier": True}, classifier=Classifier())
    assert shared.engine is Classifier.engine and Classifier.engine.loaded == 0

    for table, message in [(None, "judge_model"), ({"kind": "remote"}, "kind"), ({"kind": "local"}, "model_path"),
                           ({"kind": "local", "share_classifier": True}, "share_classifier"),
                           ({"kind": "api", "model": "m"}, "base_url")]:
        with pytest.raises(ParamsError, match=message):
            judge_from_config(table, classifier=None)

    # build_judge reads the profile and loads a local judge at assembly.
    judge = build_judge({"x-router": {"judge_model": {"kind": "local", "share_classifier": True}}},
                        classifier=Classifier())
    assert judge.engine is Classifier.engine and Classifier.engine.loaded == 1
    with pytest.raises(ParamsError, match="judge_model"):
        build_judge({"x-router": {}})
