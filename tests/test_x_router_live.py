"""Smoke test against real weights: does the classifier deploy and produce a tier?

Everything else in the suite substitutes the engine. This file does not — it is
the only place that answers "can this model actually be loaded and asked a
question here". It is therefore slow, needs weights on disk, and skips itself
whenever they are absent, so a clone without the model still runs green.

    X_ROUTER_CLASSIFIER_MODEL   path to the weights; unset means skip
    X_ROUTER_CLASSIFIER_DEVICE  "cpu" (default), "cuda:0", …

There is no default model path: weights live in a different place on every
machine, and a path baked in here would mean the suite silently skips on all but
one of them. CPU is the default device on purpose — picking a GPU automatically
would land on whichever card happens to be busiest. Skip the whole file with
`--ignore=tests/test_x_router_live.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest  # type: ignore[import-not-found]

from openjiuwen import x_router
from openjiuwen.x_router import ComplexityLevel, LocalBackend, build_classifier_request

MODEL_PATH = os.environ.get("X_ROUTER_CLASSIFIER_MODEL", "")
DEVICE = os.environ.get("X_ROUTER_CLASSIFIER_DEVICE", "cpu")

# Not configured is a skip; misconfigured is a failure. Setting the variable is
# a request to run these, so a path that does not exist is a typo to surface,
# not an absence to tolerate — and by default pytest renders both the same way.
_skip = None
if not MODEL_PATH:
    _skip = "X_ROUTER_CLASSIFIER_MODEL is not set"
else:
    _absent = []
    for module in ("torch", "transformers"):
        try:
            __import__(module)
        except ImportError:
            _absent.append(module)
    if _absent:
        _skip = "the x-router extra is not installed (missing {0})".format(", ".join(_absent))

pytestmark = pytest.mark.skipif(bool(_skip), reason=_skip or "")


@pytest.fixture(scope="module")
def backend():
    """One load for the whole module; loading is the expensive part."""
    if not Path(MODEL_PATH).is_dir():
        pytest.fail(
            "X_ROUTER_CLASSIFIER_MODEL points at {0!r}, which is not a directory".format(
                MODEL_PATH
            )
        )
    instance = LocalBackend(model_path=MODEL_PATH, device=DEVICE, max_tokens=16)
    instance.warmup()
    return instance


def _tier(backend, text):
    raw = backend.classify(build_classifier_request([{"role": "user", "content": text}], 6000))
    return raw, x_router.parse_complexity(raw)


# --------------------------------------------------------------------------


def test_model_deploys(backend):
    """The weights load and land on the requested device."""
    assert backend.engine.loaded
    assert str(backend.engine._model.device).startswith(DEVICE.split(":")[0])


def test_classifier_produces_a_parseable_tier(backend):
    """The output is a bare label — no punctuation, no preamble.

    This is what the strict parser assumes; if a model ever stops honouring it,
    every request quietly falls back to the heuristic and only `source` shows it.
    """
    raw, tier = _tier(backend, "Prove that the square root of 2 is irrational.")
    assert isinstance(tier, ComplexityLevel)
    assert raw.strip() == tier.name


def test_tiers_are_ordered_by_difficulty(backend):
    """A trivial turn must not outrank a hard one — the routing rule reads this
    ordering directly, so an inverted classifier would route backwards."""
    _, trivial = _tier(backend, "hi")
    _, hard = _tier(backend, "Prove by induction that 1+3+...+(2n-1) equals n squared.")
    assert trivial < hard


def test_decoding_is_deterministic(backend):
    """`temperature = 0` means greedy here, not a small positive floor. Without
    it the same request could route to different models on retry."""
    request = build_classifier_request(
        [{"role": "user", "content": "Design a multi-region failover architecture."}], 6000
    )
    answers = {backend.classify(request) for _ in range(3)}
    assert len(answers) == 1


def test_router_routes_end_to_end(backend):
    """Full path through the Rust kernel with a real classifier behind it."""
    pytest.importorskip("openjiuwen._openjiuwen")

    profile = {
        "algorithm": "x-router-live",
        "state": {"backend": "memory"},
        "targets": {"models": ["local", "cloud-reasoning"]},
        "x-router": {
            "local_capability_level": "MEDIUM",
            "local_model": "local",
            "tier_models": {"COMPLEX": "cloud-reasoning"},
        },
    }
    router = x_router.build_router(profile, backend=backend)
    selection = router.route_sync(
        x_router.build_request(
            [{"role": "user", "content": "Prove that the square root of 2 is irrational."}],
            session_id="live",
        )
    )

    assert selection.selected_model_id in profile["targets"]["models"]
    assert "source=llm" in selection.reasoning
    tier = selection.reasoning.split("tier=")[1].split()[0]
    assert tier in ComplexityLevel.__members__
