"""x-router bandit: the override rule and its effect on the decision.

Critical paths only: the truth table over neighbours, that a missing bandit
leaves the decision byte-for-byte unchanged, and that retrieved neighbours
reach the algorithm through the kernel. No weights needed.
"""

from __future__ import annotations

import importlib.util

import pytest  # type: ignore[import-not-found]

from openjiuwen.x_router import (
    BANDIT_COLD,
    BANDIT_IGNORE,
    BANDIT_OVERRIDE,
    BANDIT_SAME,
    BanditParams,
    ComplexityLevel as L,
    ParamsError,
    XRouterParams,
    aggregate,
    choose_tier,
    specialize,
)

requires_kernel = pytest.mark.skipif(
    importlib.util.find_spec("openjiuwen._openjiuwen") is None,
    reason="run `maturin develop` to build the native extension",
)

TIER_MODELS = {"COMPLEX": "cloud-a", "RESEARCH": "cloud-b", "REASONING": "cloud-c"}
CATALOG = ["local", "cloud-a", "cloud-b", "cloud-c"]


def item(score=1.0, **observations):
    """A retrieved neighbour. ``MEDIUM=(quality, cost)`` or ``MEDIUM=quality`` (cost 0)."""
    data = {}
    for tier, value in observations.items():
        quality, cost = value if isinstance(value, tuple) else (value, 0.0)
        data[tier] = {"quality": quality, "cost_usd": cost}
    return {"id": "n", "score": score, "data": {"observations": data}}


def bandit(**overrides):
    settings = {"min_neighbors": 2, "margin": 0.3, "lambda_c": 0.0, "cost_ref_usd": 0.01}
    settings.update(overrides)
    return BanditParams(**settings)


class Stub:
    def __init__(self, reply):
        self.reply = reply

    def classify(self, request):
        return self.reply


STRONG_COMPLEX = [item(1.0, MEDIUM=0.0, COMPLEX=0.9) for _ in range(3)]


def test_params_absent_or_disabled_means_no_bandit_and_bad_values_fail_at_assembly():
    for table in [None, {"enabled": False, "margin": 9}]:
        assert XRouterParams.from_mapping({"tier_models": TIER_MODELS, "bandit": table}).bandit is None
    assert BanditParams.from_mapping({"margin": 0.5, "store": {"top_k": 3}}).margin == 0.5
    for bad in [dict(min_neighbors=0), dict(margin=-0.1), dict(cost_ref_usd=0), dict(margin="0.3")]:
        with pytest.raises(ParamsError):
            BanditParams(**bad)


def test_malformed_state_data_is_skipped_not_raised():
    retrieved = [
        item(1.0, MEDIUM=(0.0, 0.0)),
        item(0.5, MEDIUM=(1.0, 0.02)),
        item(0.0, MEDIUM=(1.0, 0.0)),                                             # no weight
        {"id": "x", "score": 1.0, "data": None},                                   # no observations
        {"id": "x", "score": 1.0, "data": {"observations": {"BOGUS": {"quality": 1}}}},
        {"id": "x", "score": 1.0, "data": {"observations": {"MEDIUM": {"quality": "1"}}}},
        {"id": "x", "score": 1.0, "data": {"observations": {"MEDIUM": {"quality": True}}}},
    ]
    neighbours, evidence = aggregate(retrieved)
    assert neighbours == 2
    assert evidence[L.MEDIUM].mean_quality == pytest.approx(0.5 / 1.5)
    assert evidence[L.MEDIUM].mean_cost == pytest.approx(0.01 / 1.5)


@pytest.mark.parametrize(
    "tier, retrieved, params, expected",
    [
        (L.MEDIUM, [], bandit(), (L.MEDIUM, BANDIT_COLD, 0)),
        (L.MEDIUM, STRONG_COMPLEX[:1], bandit(min_neighbors=2), (L.MEDIUM, BANDIT_COLD, 1)),
        (L.MEDIUM, STRONG_COMPLEX, bandit(), (L.COMPLEX, BANDIT_OVERRIDE, 3)),
        (L.COMPLEX, STRONG_COMPLEX, bandit(), (L.COMPLEX, BANDIT_SAME, 3)),
        # Gap equal to margin does not override (dyadic values, exact subtraction).
        (L.MEDIUM, [item(1.0, MEDIUM=0.5, COMPLEX=0.75)] * 2, bandit(margin=0.25), (L.MEDIUM, BANDIT_IGNORE, 2)),
        # No evidence about the incumbent: nothing to compare against.
        (L.RESEARCH, STRONG_COMPLEX, bandit(), (L.RESEARCH, BANDIT_IGNORE, 3)),
        # Steps down as well as up.
        (L.REASONING, [item(1.0, REASONING=0.1, MEDIUM=0.9)] * 2, bandit(), (L.MEDIUM, BANDIT_OVERRIDE, 2)),
        # Cost flips the verdict: after the cost term the incumbent is best.
        (L.MEDIUM, [item(1.0, MEDIUM=(0.0, 0.0), COMPLEX=(0.9, 0.05))] * 2,
         bandit(lambda_c=0.2, cost_ref_usd=0.005), (L.MEDIUM, BANDIT_SAME, 2)),
        # A tier with unknown cost is not a candidate while cost carries weight.
        (L.MEDIUM, [{"id": "x", "score": 1.0, "data": {"observations": {
            "MEDIUM": {"quality": 0.0, "cost_usd": 0.0}, "COMPLEX": {"quality": 0.9}}}}] * 2,
         bandit(lambda_c=0.2), (L.MEDIUM, BANDIT_SAME, 2)),
        # Similarity weights decide close calls.
        (L.MEDIUM, [item(0.95, MEDIUM=0.0, COMPLEX=1.0), item(0.1, MEDIUM=1.0, COMPLEX=0.0),
                    item(0.1, MEDIUM=1.0, COMPLEX=0.0)], bandit(min_neighbors=3), (L.COMPLEX, BANDIT_OVERRIDE, 3)),
    ],
)
def test_choose_tier(tier, retrieved, params, expected):
    assert choose_tier(tier, retrieved, params) == expected


class Ctx:
    def __init__(self, retrieved=(), targets=CATALOG):
        self.targets = list(targets)
        self.retrieved = list(retrieved)
        self.view = None
        self.seed = 0


def decide(bandit_table, ctx):
    params = XRouterParams.from_mapping(
        {"local_capability_level": "MEDIUM", "local_model": "local",
         "tier_models": TIER_MODELS, "bandit": bandit_table},
        backend=Stub("MEDIUM"),
    )
    return specialize(params, name="x-router-bandit-unit")().decide(
        {"messages": [{"role": "user", "content": "do the thing"}]}, ctx)


def test_decide_with_and_without_the_bandit():
    # Unconfigured: neighbours present, decision and reasoning untouched.
    off = decide(None, Ctx(STRONG_COMPLEX))
    assert off["selected_model_id"] == "local"
    assert off["reasoning"] == "x-router: rule=within_local_capability tier=MEDIUM source=llm"
    # Configured: override escalates and reports both tiers.
    on = decide({"min_neighbors": 2, "lambda_c": 0.0}, Ctx(STRONG_COMPLEX))
    assert on["selected_model_id"] == "cloud-a"
    assert on["reasoning"] == ("x-router: rule=escalate_cloud tier=COMPLEX source=llm "
                               "tier_llm=MEDIUM bandit=override neighbors=3")
    # The overridden tier's model can still be excluded: degrade, never escalate further.
    degraded = decide({"min_neighbors": 2, "lambda_c": 0.0}, Ctx(STRONG_COMPLEX, targets=["local", "cloud-b"]))
    assert degraded["selected_model_id"] == "local"
    assert "rule=target_excluded_degrade tier=COMPLEX" in degraded["reasoning"]


@requires_kernel
def test_retrieved_from_a_python_state_drives_the_override():
    from openjiuwen import RouteHint, StateProvider, StateQuery, x_router

    class Neighbours(StateProvider):
        name = "bandit-neighbours"

        def __init__(self):
            self.route_ids = []

        def snapshot(self, key):
            return {}

        def query(self, key, query):
            self.route_ids.append(query.route_id)
            return {"view": {}, "retrieved": [dict(it, id="n{0}".format(i)) for i, it in enumerate(STRONG_COMPLEX)]}

        def report(self, feedback):
            pass

    state = Neighbours()
    router = x_router.build_router({
        "algorithm": "x-router-bandit-kernel", "state": {"backend": "memory"},
        "targets": {"models": CATALOG},
        "x-router": {"local_capability_level": "MEDIUM", "local_model": "local",
                     "tier_models": TIER_MODELS, "bandit": {"min_neighbors": 2, "lambda_c": 0.0}},
    }, backend=Stub("MEDIUM"), state=state)
    request = x_router.build_request([{"role": "user", "content": "do the thing"}], session_id="s", agent_id="host")

    cold = router.route_sync(request)                       # no hint: state not queried, bandit cold
    assert cold.selected_model_id == "local" and cold.reasoning.endswith("bandit=cold neighbors=0")
    assert state.route_ids == []
    warm = router.route_sync(request, RouteHint(state_query=StateQuery(text="do the thing")))
    assert warm.selected_model_id == "cloud-a" and "bandit=override neighbors=3" in warm.reasoning
    assert state.route_ids == [warm.route_id]
