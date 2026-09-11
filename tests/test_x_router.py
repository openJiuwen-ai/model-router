"""x-router: routing rules, classification, and assembly.

Everything here runs without weights and without an accelerator, because the
classifier is injected rather than constructed inside the code under test. The
tests that need a real model live in test_x_router_live.py.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest  # type: ignore[import-not-found]

from openjiuwen import check_purity
from openjiuwen.x_router import (
    PROMPT_TEMPLATE,
    RULE_ESCALATE_CLOUD,
    RULE_TARGET_EXCLUDED_DEGRADE,
    RULE_WITHIN_LOCAL_CAPABILITY,
    SOURCE_HEURISTIC,
    SOURCE_HEURISTIC_FALLBACK,
    SOURCE_LLM,
    SOURCE_PARSE_FAILED,
    ClassifierRequest,
    ComplexityLevel,
    LocalBackend,
    ParamsError,
    XRouterParams,
    backend_from_config,
    build_classifier_request,
    classify,
    classify_heuristic,
    conversation_preview,
    decide_by_tier,
    parse_complexity,
    specialize,
)
from openjiuwen.x_router.classifier import ClassifierEngine

TIER_MODELS = {"COMPLEX": "cloud-a", "RESEARCH": "cloud-b", "REASONING": "cloud-c"}
CATALOG = ["local", "cloud-a", "cloud-b", "cloud-c"]

requires_kernel = pytest.mark.skipif(
    importlib.util.find_spec("openjiuwen._openjiuwen") is None,
    reason="run `maturin develop` to build the native extension",
)


def params(capability="MEDIUM", backend=None, **overrides):
    settings = {
        "local_capability_level": capability,
        "local_model": "local",
        "tier_models": TIER_MODELS,
    }
    settings.update(overrides)
    return XRouterParams.from_mapping(settings, backend=backend)


class Stub:
    """A classifier that answers with whatever it was given."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def classify(self, request):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


# ==========================================================================
# Routing rules — pure, and the only place the decision table is checked
# ==========================================================================


@pytest.mark.parametrize(
    "tier,model,rule",
    [
        (ComplexityLevel.SIMPLE, "local", RULE_WITHIN_LOCAL_CAPABILITY),
        (ComplexityLevel.MEDIUM, "local", RULE_WITHIN_LOCAL_CAPABILITY),
        (ComplexityLevel.COMPLEX, "cloud-a", RULE_ESCALATE_CLOUD),
        (ComplexityLevel.RESEARCH, "cloud-b", RULE_ESCALATE_CLOUD),
        (ComplexityLevel.REASONING, "cloud-c", RULE_ESCALATE_CLOUD),
    ],
)
def test_truth_table(tier, model, rule):
    """The capability boundary is inclusive: a tier equal to it stays local."""
    assert decide_by_tier(tier, CATALOG, params("MEDIUM")) == (model, rule)


@pytest.mark.parametrize("tier", [ComplexityLevel.SIMPLE, ComplexityLevel.REASONING])
def test_capability_none_disables_the_local_tier(tier):
    model, rule = decide_by_tier(tier, CATALOG, params("NONE"))
    assert rule == RULE_ESCALATE_CLOUD
    assert model != "local"


def test_excluded_target_degrades_never_escalates():
    """A failed cloud model degrades to local rather than to another cloud model.

    Escalating on failure is how a routing bug turns into a cost incident.
    """
    assert decide_by_tier(ComplexityLevel.RESEARCH, ["local", "cloud-a"], params()) == (
        "local",
        RULE_TARGET_EXCLUDED_DEGRADE,
    )


def test_complex_is_the_required_fallback_tier():
    """Tiers with no model of their own fall back to COMPLEX, so it must exist."""
    partial = XRouterParams.from_mapping(
        {"local_capability_level": "SIMPLE", "tier_models": {"COMPLEX": "cloud-a"}}
    )
    assert decide_by_tier(ComplexityLevel.REASONING, CATALOG, partial)[0] == "cloud-a"

    with pytest.raises(ParamsError, match="COMPLEX"):
        XRouterParams.from_mapping({"tier_models": {"RESEARCH": "cloud-b"}})


def test_selectable_models_must_be_in_the_catalog():
    with pytest.raises(ParamsError, match="absent from"):
        params().validate_against(["local", "cloud-a"])


# ==========================================================================
# What the classifier is shown
# ==========================================================================

# Update only deliberately, and re-measure when you do. See DESIGN.md section 4.
PREVIEW_SHA256 = "aeeda2cf22fa18ee29bb871c349befd2a1f9acc9a4fa278ca06ceb9d706e96d3"

# Exercises every preview rule at once: scaffolding removal, the recent window,
# the latest-user rescue, tool annotation, and middle truncation.
CANONICAL_MESSAGES = (
    [{"role": "system", "content": "ignored"}, {"role": "user", "content": "THE TASK"}]
    + [
        {
            "role": "assistant",
            "content": "step {0}".format(i),
            "tool_calls": [{"function": {"name": "tool{0}".format(i)}}],
        }
        for i in range(9)
    ]
    + [{"role": "user", "content": "follow up " * 40}]
)


def test_preview_rules_are_frozen():
    """Guards window size, truncation strategy and formatting as one unit.

    Unlike the prompt, the preview is computed, so it drifts from edits that look
    local: the window constant, the scaffolding filter, content flattening, the
    truncation helper. Each changes every classifier input without anyone editing
    anything that looks like classifier input.
    """
    digest = hashlib.sha256(
        conversation_preview(CANONICAL_MESSAGES, 512).encode("utf-8")
    ).hexdigest()
    assert digest == PREVIEW_SHA256, (
        "preview construction changed; measurements taken with the old preview no "
        "longer describe this router. Re-measure, then update PREVIEW_SHA256 "
        "deliberately. New digest: " + digest
    )


def test_prompt_carries_the_conversation():
    """The prompt is a tunable, so its wording is not frozen — but losing the
    ``{content}`` placeholder is silent: ``format()`` would still return a
    perfectly well-formed prompt describing no conversation at all.
    """
    assert "{content}" in PROMPT_TEMPLATE
    request = build_classifier_request([{"role": "user", "content": "unmistakable"}], 6000)
    assert "[user]: unmistakable" in request.prompt


def test_preview_drops_system_and_injected_scaffolding():
    scaffolding = "<system-reminder>\n<prompt-attachment>x</prompt-attachment></system-reminder>"
    preview = conversation_preview(
        [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": scaffolding},
            {"role": "user", "content": "real task"},
        ],
        6000,
    )
    assert preview == "[user]: real task"


def test_preview_keeps_the_latest_user_message_outside_the_window():
    """An agent loop's tail is all assistant traffic; the task must still survive."""
    messages = [{"role": "user", "content": "THE TASK"}]
    messages += [{"role": "assistant", "content": "step {0}".format(i)} for i in range(8)]
    assert conversation_preview(messages, 6000).startswith("[user]: THE TASK")


def test_preview_flattens_list_content():
    preview = conversation_preview(
        [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url"}]}],
        6000,
    )
    assert preview == "[user]: a"


# ==========================================================================
# What the classifier is allowed to answer
# ==========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("\n research \n", ComplexityLevel.RESEARCH),
        ("I think COMPLEX", None),
        ("COMPLEX RESEARCH", None),
        ("COMPLEX.", None),
    ],
)
def test_parsing_accepts_only_a_bare_label(raw, expected):
    """A trained classifier emitting prose is drift, not noise to be salvaged."""
    if expected is None:
        with pytest.raises(ValueError, match="exactly one complexity label"):
            parse_complexity(raw)
    else:
        assert parse_complexity(raw) is expected


# ==========================================================================
# Heuristic fallback
# ==========================================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hi", ComplexityLevel.SIMPLE),
        ("please implement a retry helper", ComplexityLevel.MEDIUM),
        ("do a root cause analysis of the outage", ComplexityLevel.COMPLEX),
        ("summarise the literature review on decoherence", ComplexityLevel.RESEARCH),
        ("prove the theorem by induction", ComplexityLevel.REASONING),
    ],
)
def test_heuristic_tiers(text, expected):
    assert classify_heuristic([{"role": "user", "content": text}]) is expected


def test_heuristic_escalates_on_volume():
    assert classify_heuristic([{"role": "user", "content": "word " * 500}]) is ComplexityLevel.COMPLEX


# ==========================================================================
# Degradation — one case per path, asserting where the tier came from
# ==========================================================================


@pytest.mark.parametrize(
    "backend,source",
    [
        (Stub("RESEARCH"), SOURCE_LLM),
        (None, SOURCE_HEURISTIC),
        (Stub(RuntimeError("down")), SOURCE_HEURISTIC_FALLBACK),
        (Stub("probably COMPLEX"), SOURCE_PARSE_FAILED),
    ],
)
def test_classify_reports_where_the_tier_came_from(backend, source):
    """A classifier that is down and one that has drifted are different problems
    with different owners; one code for both would hide both."""
    assert classify([{"role": "user", "content": "hi"}], params(backend=backend))[1] == source


def test_classify_never_raises_on_unusable_input():
    assert classify([], params(backend=Stub("COMPLEX"))) == (
        ComplexityLevel.SIMPLE,
        SOURCE_HEURISTIC_FALLBACK,
    )


def test_check_purity_does_not_apply_here():
    """It probes classifier stability, not purity, and it performs real I/O.

    Against a steady stub it passes while proving nothing; against an unstable
    classifier it fails — which it can only detect because `source` is part of
    the decision. Never run it against a live classifier: serving stacks do not
    all decode deterministically, so it becomes flaky.
    """

    class Ctx:
        targets = ["local", "cloud-a"]

    request = {"messages": [{"role": "user", "content": "hi"}]}
    settings = {"local_capability_level": "NONE", "tier_models": {"COMPLEX": "cloud-a"}}

    steady = Stub("COMPLEX")
    algo = specialize(XRouterParams.from_mapping(settings, backend=steady), "purity-steady")()
    check_purity(algo, request, Ctx(), rounds=3)
    assert steady.calls == 3, "each round is a real backend call, not a replay"

    class Flaky(Stub):
        def classify(self, request):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("classifier down")
            return "COMPLEX"

    unstable = specialize(
        XRouterParams.from_mapping(settings, backend=Flaky("COMPLEX")), "purity-flaky"
    )()
    with pytest.raises(AssertionError, match="not pure"):
        check_purity(unstable, request, Ctx(), rounds=2)


# ==========================================================================
# The classifier component
# ==========================================================================


class FakeEngine:
    model_path = "/models/classifier"
    loaded = True

    def __init__(self):
        self.calls = []

    def load(self):
        self.calls.append(("load",))

    def classify_text(self, text, max_new_tokens=16, temperature=0.0):
        self.calls.append(("classify_text", text, max_new_tokens))
        return "COMPLEX"


def test_engine_construction_is_cheap_and_lazy():
    """Constructing must not touch weights: a bad path should surface before a
    multi-gigabyte load does."""
    assert ClassifierEngine("/models/classifier").loaded is False


def test_local_backend_runs_the_engine_in_process():
    engine = FakeEngine()
    assert LocalBackend(engine=engine, max_tokens=8).classify(ClassifierRequest("PROMPT")) == "COMPLEX"
    assert engine.calls == [("classify_text", "PROMPT", 8)]


@pytest.mark.parametrize("section", [{"enabled": False}, {"enabled": False, "model_path": "/m"}])
def test_disabling_the_classifier_is_allowed(section):
    """Running on the heuristic is a choice an operator can make explicitly."""
    assert backend_from_config(section) is None


@pytest.mark.parametrize("section", [None, {}, {"enabled": True}])
def test_a_missing_classifier_is_an_error_not_heuristic_mode(section):
    """Falling back silently would produce a router that never escalates and
    looks healthy. Opting out has to be deliberate."""
    with pytest.raises(ParamsError, match="enabled = false"):
        backend_from_config(section)


def test_importing_the_package_pulls_in_no_heavy_dependency():
    """``discover`` imports every module under this package to find algorithms,
    so ``import openjiuwen`` reaches the engine module. That is only harmless
    because ``import torch`` sits inside ``load()``; at module scope it would
    give the whole kernel a multi-second import, including for callers that
    never touch x_router.

    Checked in a fresh interpreter so the result cannot depend on test ordering.
    """
    source = Path(__file__).resolve().parents[1] / "python"
    probe = (
        "import sys, json; sys.path.insert(0, {0!r}); import openjiuwen;"
        "print(json.dumps({{"
        "'walked': 'openjiuwen.x_router.classifier.engine' in sys.modules,"
        "'leaked': [n for n in ('torch','transformers') if n in sys.modules]"
        "}}))"
    ).format(str(source))
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert report["walked"], "discover no longer walks this package; this guard measures nothing"
    assert not report["leaked"], (
        "importing openjiuwen pulled in {0}; move that import inside the function "
        "that needs it".format(report["leaked"])
    )


# ==========================================================================
# Through the kernel — only what the pure tests cannot reach
# ==========================================================================

PROFILE = {
    "algorithm": "x-router-test",
    "state": {"backend": "memory"},
    "targets": {"models": CATALOG},
    "x-router": {
        "local_capability_level": "MEDIUM",
        "local_model": "local",
        "tier_models": TIER_MODELS,
    },
}


def build(profile=None):
    from openjiuwen import x_router

    return x_router.build_router(dict(profile or PROFILE), backend=Stub("RESEARCH"))


def request(session_id="s1"):
    from openjiuwen import x_router

    return x_router.build_request(
        [{"role": "user", "content": "do the thing"}], session_id=session_id, agent_id="host"
    )


@requires_kernel
def test_route_returns_the_tier_model_through_the_kernel():
    router = build()
    assert router.algorithm_name() == "x-router-test"
    selection = router.route_sync(request())
    assert selection.selected_model_id == "cloud-b"
    assert selection.reasoning == "x-router: rule=escalate_cloud tier=RESEARCH source=llm"


@requires_kernel
def test_unavailable_feedback_degrades_the_next_route():
    """route -> failure -> report -> the excluded model is not chosen again."""
    from openjiuwen import Feedback, RoutingKey

    router = build()
    req = request("s-degrade")
    assert router.route_sync(req).selected_model_id == "cloud-b"

    router.report_sync(Feedback(RoutingKey("s-degrade", "host"), "cloud-b", "unavailable", 25))

    selection = router.route_sync(req)
    assert selection.selected_model_id == "local"
    assert "rule=target_excluded_degrade" in selection.reasoning


@requires_kernel
def test_assembly_rejects_models_missing_from_the_catalog():
    profile = dict(PROFILE)
    profile["targets"] = {"models": ["local", "cloud-a"]}
    with pytest.raises(ParamsError, match="absent from"):
        build(profile)


@requires_kernel
def test_list_content_must_be_normalized_before_routing():
    """The protocol rejects non-string content, and a host that catches routing
    errors would then route everything locally while looking healthy."""
    router = build()
    raw = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "session_id": "s1",
        "agent_id": "host",
    }
    with pytest.raises(TypeError):
        router.route_sync(raw)
    assert router.route_sync(request()).selected_model_id == "cloud-b"
