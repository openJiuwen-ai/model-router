"""x-router service: the loop closed inside the package.

Critical paths only: the hint stays under the kernel's byte cap, the quality
report has the shape the store reads, ``report`` files call feedback now and
scores later, scoring failures are counted rather than raised, the queue drops
rather than blocks, ``close`` drains, and the whole loop runs through the
kernel with two host calls.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from concurrent.futures import Future

import pytest  # type: ignore[import-not-found]

from openjiuwen.x_router import (
    EXTENSION_SCHEMA,
    SERVED,
    JudgeError,
    JudgeRequest,
    Scorer,
    XRouterParams,
    XRouterService,
    build_bandit_feedback,
    build_hint,
    build_service,
    parse_reasoning,
)
from openjiuwen.x_router.service import QUERY_MAX_TEXT_BYTES

requires_kernel = pytest.mark.skipif(
    importlib.util.find_spec("openjiuwen._openjiuwen") is None,
    reason="run `maturin develop` to build the native extension",
)

TIER_MODELS = {"COMPLEX": "cloud-a", "RESEARCH": "cloud-b", "REASONING": "cloud-c"}
CATALOG = ["local", "cloud-a", "cloud-b", "cloud-c"]
MESSAGES = [{"role": "user", "content": "rotate the logs weekly"}]


def params(bandit={"min_neighbors": 2}):
    return XRouterParams.from_mapping(
        {"local_capability_level": "MEDIUM", "local_model": "local", "tier_models": TIER_MODELS, "bandit": bandit})


class Selection:
    def __init__(self, decision_id="d1", tier="COMPLEX", model="cloud-a"):
        self.selected_model_id = model
        self.decision_id = decision_id
        self.reasoning = "x-router: rule=escalate_cloud tier={0} source=llm".format(tier)


class RecordingRouter:
    def __init__(self):
        self.routes = []
        self.reports = []
        self.lock = threading.Lock()

    def route_sync(self, request, hint=None):
        with self.lock:
            self.routes.append((request, hint))
        return Selection(decision_id="d{0}".format(len(self.routes)))

    def report_sync(self, feedback):
        with self.lock:
            self.reports.append(feedback)


class FixedJudge:
    def __init__(self, reply='{"task_progress": 1, "correctness": 1, "grounding": 0}', delay=0.0):
        self.reply = reply
        self.delay = delay

    def score(self, request):
        assert isinstance(request, JudgeRequest)
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Inline:
    """Runs the job on submit, so tests see the outcome immediately."""

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True):
        pass


class Parked:
    """Holds jobs until released; models a stalled judge."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))
        return Future()

    def release(self):
        for fn, args in self.jobs:
            fn(*args)
        self.jobs = []

    def shutdown(self, wait=True):
        pass


def service(judge=FixedJudge, bandit={"min_neighbors": 2}, **kwargs):
    router = RecordingRouter()
    kwargs.setdefault("executor", Inline())
    judge = judge() if callable(judge) else judge
    return router, XRouterService(router, params(bandit), judge=judge, **kwargs)


# ---- the pieces ------------------------------------------------------------


def test_hint_uses_the_classifier_preview_and_stays_under_the_kernel_byte_cap():
    assert build_hint([{"role": "system", "content": "sys"}] + MESSAGES, params()) == {
        "state_query": {"text": "[user]: rotate the logs weekly"}}
    assert build_hint([{"role": "user", "content": ""}], params()) is None
    # 6000 CJK characters are 18 000 bytes: over the limit the runtime would silently drop.
    text = build_hint([{"role": "user", "content": "路" * 6000}], params())["state_query"]["text"]
    assert len(text.encode("utf-8")) <= QUERY_MAX_TEXT_BYTES and text.endswith("路")


def test_quality_report_has_the_shape_the_store_reads_and_rejects_bad_input():
    fb = build_bandit_feedback(Selection(), {SERVED: (0.8, 0.004), "medium": 0.1}, session_id="s", agent_id="a")
    assert fb == {
        "session_id": "s", "agent_id": "a", "selected_model_id": "cloud-a", "decision_id": "d1", "call": None,
        "extensions": [{"schema": EXTENSION_SCHEMA, "version": "1", "data": {"observations": {
            "COMPLEX": {"quality": 0.8, "cost_usd": 0.004}, "MEDIUM": {"quality": 0.1, "cost_usd": None}}}}],
    }
    assert parse_reasoning(Selection().reasoning)["tier"] == "COMPLEX"
    for selection, observations in [(Selection(decision_id=None), {SERVED: 0.5}), (Selection(), {}),
                                    (Selection(), {SERVED: 1.5}), (Selection(), {SERVED: (0.5, -1)}),
                                    (Selection(), {"BOGUS": 0.5}), (Selection(), {SERVED: 0.5, "COMPLEX": 0.6})]:
        with pytest.raises(ValueError):
            build_bandit_feedback(selection, observations)


def test_scorer_judges_and_files_the_second_report_or_raises():
    router = RecordingRouter()
    quality = Scorer(router, FixedJudge()).settle(Selection(), MESSAGES, "done", cost_usd=0.004,
                                                 session_id="s", agent_id="a")
    assert quality == pytest.approx(0.8)
    assert router.reports[-1]["extensions"][0]["data"]["observations"] == {
        "COMPLEX": {"quality": pytest.approx(0.8), "cost_usd": 0.004}}
    with pytest.raises(JudgeError):
        Scorer(router, FixedJudge(JudgeError("down"))).settle(Selection(), MESSAGES, "done")
    assert len(router.reports) == 1, "no report on failure"


# ---- the loop ----------------------------------------------------------------


def test_report_files_call_feedback_now_and_scores_after():
    router, svc = service()
    selection = svc.route(MESSAGES, session_id="s", agent_id="a")
    assert router.routes[-1][1] == {"state_query": {"text": "[user]: rotate the logs weekly"}}
    assert svc.report(selection, latency_ms=42, messages=MESSAGES, response_text="done", cost_usd=0.0,
                      session_id="s", agent_id="a") is True
    call_fb, bandit_fb = router.reports
    assert call_fb["call"] == {"outcome": "ok", "latency_ms": 42} and call_fb["decision_id"] == "d1"
    assert bandit_fb["decision_id"] == "d1" and bandit_fb["call"] is None
    assert svc.stats["settled"] == 1 and svc.stats["pending"] == 0

    # Nothing to score: a failed call, or no transcript / response, or no judge.
    for kwargs in [dict(outcome="unavailable", messages=MESSAGES, response_text="x"),
                   dict(messages=MESSAGES), dict(response_text="x")]:
        assert svc.report(svc.route(MESSAGES), **kwargs) is False
    router, plain = service(judge=None, bandit=None)
    assert plain.route(MESSAGES) and router.routes[-1][1] is None            # no bandit: no hint
    assert plain.report(plain.route(MESSAGES), messages=MESSAGES, response_text="done") is False


def test_scoring_failures_are_counted_and_reported_never_raised():
    seen = []
    router, svc = service(judge=lambda: FixedJudge(JudgeError("503")),
                          on_settle_error=lambda exc, sel: seen.append(type(exc).__name__))
    assert svc.report(svc.route(MESSAGES), messages=MESSAGES, response_text="done") is True
    assert len(router.reports) == 1 and svc.stats["judge_failed"] == 1 and seen == ["JudgeError"]
    with pytest.raises(JudgeError):
        svc.settle(svc.route(MESSAGES), MESSAGES, "done")                     # the synchronous path raises


def test_full_queue_drops_scores_and_close_drains():
    parked = Parked()
    router, svc = service(queue_size=2, executor=parked)
    scheduled = [svc.report(svc.route(MESSAGES), messages=MESSAGES, response_text="done") for _ in range(4)]
    assert scheduled == [True, True, False, False]
    assert svc.stats["dropped_full"] == 2 and len(router.reports) == 4, "call feedback is never dropped"
    parked.release()
    assert svc.stats["settled"] == 2

    router = RecordingRouter()
    svc = XRouterService(router, params(), judge=FixedJudge(delay=0.05), workers=2)
    for _ in range(3):
        svc.report(svc.route(MESSAGES), messages=MESSAGES, response_text="done")
    svc.close()
    assert svc.stats["settled"] == 3 and svc.stats["pending"] == 0
    assert svc.report(svc.route(MESSAGES), messages=MESSAGES, response_text="done") is False


# ---- through the kernel --------------------------------------------------------

PROFILE = {
    "algorithm": "x-router-service",
    "state": {"backend": "x-router-bandit"},
    "targets": {"models": CATALOG},
    "x-router": {
        "local_capability_level": "MEDIUM", "local_model": "local", "tier_models": TIER_MODELS,
        "classifier_model": {"enabled": False},
        "bandit": {"min_neighbors": 3, "lambda_c": 0.0, "store": {"retriever_dim": 512, "min_similarity": 0.3}},
        "judge_model": {"kind": "api", "base_url": "http://judge", "model": "m", "api_key_env": "SERVICE_JUDGE_KEY"},
    },
}


@requires_kernel
def test_the_loop_closes_through_the_kernel_with_two_host_calls(monkeypatch):
    pytest.importorskip("numpy")
    from openjiuwen.x_router import ApiJudgeBackend, BanditStore

    monkeypatch.setenv("SERVICE_JUDGE_KEY", "sk")
    assembled = build_service(PROFILE)
    assert isinstance(assembled.store, BanditStore) and isinstance(assembled.judge, ApiJudgeBackend)
    assembled.close()

    class Scripted:
        """Classifier stub: the tiers it will answer, in order, then MEDIUM."""

        def __init__(self, *tiers):
            self.tiers = list(tiers)

        def classify(self, request):
            return self.tiers.pop(0) if self.tiers else "MEDIUM"

    class ByResponse:
        """Judge stub: a curt refusal scores badly, anything else well."""

        def score(self, request):
            bad = "I cannot do that" in request.user_prompt
            return '{"task_progress": %d, "correctness": %d, "grounding": 1}' % ((-1, -1) if bad else (1, 1))

    # The store learns both arms from the classifier's own variation: turns it
    # sent local (MEDIUM) went badly, turns it escalated (COMPLEX) went well.
    svc = build_service(PROFILE, backend=Scripted(*["MEDIUM", "COMPLEX"] * 3), judge=ByResponse(), executor=Inline())
    try:
        text = "migrate the billing schema without downtime, attempt {0}"
        for i in range(6):
            messages = [{"role": "user", "content": text.format(i)}]
            selection = svc.route(messages, session_id="s", agent_id="host")
            served_cloud = selection.selected_model_id == "cloud-a"
            svc.report(selection, latency_ms=10, messages=messages,
                       response_text="Done: migration plan applied." if served_cloud else "I cannot do that.",
                       cost_usd=0.003 if served_cloud else 0.0, session_id="s", agent_id="host")
        assert svc.stats["settled"] == 6 and svc.stats["store"]["closed"] == 6

        selection = svc.route([{"role": "user", "content": text.format(6)}], session_id="s", agent_id="host")
        parsed = parse_reasoning(selection.reasoning)
        assert (selection.selected_model_id, parsed["tier_llm"], parsed["tier"], parsed["bandit"]) == (
            "cloud-a", "MEDIUM", "COMPLEX", "override")
    finally:
        svc.close()
