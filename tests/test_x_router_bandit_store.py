"""x-router BanditStore: the memory behind the bandit.

Critical paths only: a scored turn becomes a retrievable neighbour, unscored
and unknown ones are accounted for, call feedback keeps the kernel's exclusion
semantics, version ageing works, a broken store never breaks routing, and a
half-declared store is refused at assembly. Skipped without numpy.
"""

from __future__ import annotations

import importlib.util

import pytest  # type: ignore[import-not-found]

np = pytest.importorskip("numpy")

from openjiuwen.x_router import (  # noqa: E402
    EXTENSION_SCHEMA,
    POLICY_SLOT,
    SERVED,
    STORE_BACKEND,
    BanditStore,
    BanditStoreParams,
    HashedNgramRetriever,
    ParamsError,
    XRouterParams,
    build_bandit_feedback,
    build_hint,
    build_store,
    parse_reasoning,
)

requires_kernel = pytest.mark.skipif(
    importlib.util.find_spec("openjiuwen._openjiuwen") is None,
    reason="run `maturin develop` to build the native extension",
)

TIER_MODELS = {"COMPLEX": "cloud-a", "RESEARCH": "cloud-b", "REASONING": "cloud-c"}
CATALOG = ["local", "cloud-a", "cloud-b", "cloud-c"]


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Key:
    def __init__(self, session_id="s", agent_id="a"):
        self.session_id = session_id
        self.agent_id = agent_id


class Query:
    def __init__(self, text=None, decision_id=None, top_k=None):
        self.text = text
        self.decision_id = decision_id
        self.top_k = top_k
        self.vector = None
        self.extensions = []


class Ext:
    def __init__(self, data, schema=EXTENSION_SCHEMA):
        self.schema = schema
        self.version = "1"
        self.data = data


class Call:
    def __init__(self, outcome):
        self.outcome = outcome


class Fb:
    def __init__(self, decision_id=None, call=None, extensions=(), model="local"):
        self.decision_id = decision_id
        self.call = call
        self.extensions = list(extensions)
        self.selected_model_id = model
        self.key = Key()


def outcome(**tiers):
    return Ext({"observations": {t: {"quality": q, "cost_usd": c} for t, (q, c) in tiers.items()}})


def store(clock=None, **overrides):
    settings = {"retriever_dim": 512, "top_k": 5, "min_similarity": 0.5}
    settings.update(overrides)
    return BanditStore(BanditStoreParams(**settings), clock=clock or Clock())


def warm(store_, text, decision_id, **tiers):
    """query → report for one turn."""
    store_.query(Key(), Query(text=text, decision_id=decision_id))
    store_.report(Fb(decision_id=decision_id, extensions=[outcome(**tiers)]))


def test_params_bounds_and_the_store_subtable():
    assert BanditStoreParams.from_mapping({"margin": 0.3}) is None
    assert BanditStoreParams.from_mapping({"enabled": False, "store": {"top_k": 3}}).top_k == 3
    for bad in [dict(retriever_dim=0), dict(top_k=257), dict(min_similarity=1.5),
                dict(forgetting_gamma=0), dict(pending_ttl_secs=0), dict(persist=True)]:
        with pytest.raises(ParamsError):
            BanditStoreParams(**bad)


def test_retriever_matches_edgetrl_hashing():
    # Same FNV-1a 3-gram construction as EdgeTRL and agent-xrouter: neighbours agree across the three.
    retriever = HashedNgramRetriever(dim=256)
    assert np.allclose(retriever.encode("Compare  Raft and Paxos"), retriever.encode("compare raft and paxos"))
    assert retriever.encode("abc")[0x1A47E90B % 256] == pytest.approx(1.0)   # FNV-1a("abc")
    assert not np.linalg.norm(retriever.encode("   "))


def test_scored_turn_becomes_a_neighbour_and_unscored_ones_are_accounted_for():
    clock = Clock()
    s = store(clock, pending_ttl_secs=100)

    # query opens a pending record; nothing retrievable yet.
    assert s.query(Key(), Query(text="rotate the logs weekly with logrotate", decision_id="d1"))["retrieved"] == []
    assert s.stats["pending"] == 1
    # report closes it; a similar query now finds it, an unrelated one does not.
    s.report(Fb(decision_id="d1", extensions=[outcome(MEDIUM=(0.2, 0.0), COMPLEX=(0.9, 0.004))]))
    hits = s.query(Key(), Query(text="rotate the logs weekly with logrotate please", decision_id="d2"))["retrieved"]
    assert len(hits) == 1 and 0.5 <= hits[0]["score"] <= 1.0
    assert hits[0]["data"] == {"observations": {"MEDIUM": {"quality": 0.2, "cost_usd": 0.0},
                                                "COMPLEX": {"quality": 0.9, "cost_usd": 0.004}}}
    assert s.query(Key(), Query(text="prove Fermat's last theorem", decision_id="d3"))["retrieved"] == []

    # d2 and d3 were never scored: they expire and are counted; a late score is unknown.
    clock.now += 101
    assert s.expire_pending() == 2
    s.report(Fb(decision_id="d2", extensions=[outcome(MEDIUM=(0.5, 0.0))]))
    s.report(Fb(decision_id="never-queried", extensions=[outcome(MEDIUM=(0.5, 0.0))]))
    assert s.stats == {"pending": 0, "closed": 1, "dropped": 2, "unknown": 2, "version": 0}


def test_call_feedback_keeps_memorystate_semantics():
    clock = Clock()
    s = store(clock, exclusion_ttl_secs=60)
    s.report(Fb(call=Call("unavailable"), model="cloud-a"))
    s.report(Fb(call=Call("overflow"), model="cloud-b"))
    s.report(Fb(call=Call("rejected"), model="cloud-c"))
    s.report(Fb(call=Call("ok"), model="local"))
    s.report(Fb(call=None))                                             # delayed feedback: no change
    assert s.snapshot(Key()) == {"affinity": "local", "exclusions": ["cloud-a", "cloud-b"],
                                 "stats": {"sample_count": 4}}
    clock.now += 61
    assert s.snapshot(Key())["exclusions"] == []


def test_publish_ages_records_without_clearing_them():
    s = store(forgetting_gamma=0.5)
    warm(s, "an ordinary request", "d1", MEDIUM=(0.5, 0.0))
    before = s.query(Key(), Query(text="an ordinary request", decision_id="q1"))["retrieved"][0]["score"]
    s.publish(POLICY_SLOT, b"", 2)
    after = s.query(Key(), Query(text="an ordinary request", decision_id="q2"))["retrieved"][0]["score"]
    assert after == pytest.approx(before * 0.25) and s.stats["closed"] == 1


def test_record_carries_the_version_it_was_routed_under():
    """A publish between query and report ages the record: the score belongs to the old policy."""
    s = store(forgetting_gamma=0.5)
    s.query(Key(), Query(text="an ordinary request", decision_id="d1"))
    s.publish(POLICY_SLOT, b"", 1)
    s.report(Fb(decision_id="d1", extensions=[outcome(MEDIUM=(0.5, 0.0))]))
    warm(s, "an ordinary request", "d2", MEDIUM=(0.5, 0.0))
    scores = {r["id"]: r["score"] for r in s.query(Key(), Query(text="an ordinary request", decision_id="q"))["retrieved"]}
    assert scores["r0"] == pytest.approx(scores["r1"] * 0.5)  # r0 routed under v0, r1 under v1


def test_a_broken_store_never_breaks_routing():
    class Broken:
        dim = 512

        def encode(self, text):
            raise RuntimeError("boom")

    s = BanditStore(BanditStoreParams(retriever_dim=512), retriever=Broken())
    result = s.query(Key(), Query(text="anything", decision_id="d1"))
    assert result["retrieved"] == [] and "view" in result and s.stats["pending"] == 0


PROFILE = {
    "algorithm": "x-router-bandit-assembly",
    "state": {"backend": STORE_BACKEND},
    "targets": {"models": CATALOG},
    "x-router": {
        "local_capability_level": "MEDIUM", "local_model": "local", "tier_models": TIER_MODELS,
        "classifier_model": {"enabled": False},
        "bandit": {"min_neighbors": 3, "lambda_c": 0.0, "store": {"retriever_dim": 512, "min_similarity": 0.3}},
    },
}


@requires_kernel
def test_half_declared_store_is_refused_at_assembly():
    import copy

    from openjiuwen.x_router import build_router

    no_backend = copy.deepcopy(PROFILE)
    no_backend["state"]["backend"] = "memory"
    with pytest.raises(ParamsError, match="backend"):
        build_router(no_backend)
    no_table = copy.deepcopy(PROFILE)
    del no_table["x-router"]["bandit"]["store"]
    with pytest.raises(ParamsError, match="store"):
        build_router(no_table)


@requires_kernel
def test_route_report_route_closes_the_loop():
    from openjiuwen import Feedback
    from openjiuwen.x_router import build_request, build_router

    class Judge:
        def classify(self, request):
            return "MEDIUM"

    memory = build_store(PROFILE)
    router = build_router(PROFILE, backend=Judge(), state=memory)
    xp = XRouterParams.from_mapping(PROFILE["x-router"])

    def turn(text):
        messages = [{"role": "user", "content": text}]
        return router.route_sync(build_request(messages, session_id="s", agent_id="host"), build_hint(messages, xp))

    # Three similar turns the classifier calls MEDIUM, each scored much better on COMPLEX.
    for i in range(3):
        selection = turn("migrate the billing schema without downtime, attempt {0}".format(i))
        assert selection.selected_model_id == "local"
        router.report_sync(Feedback.ok(selection, 10, session_id="s", agent_id="host"))
        router.report_sync(build_bandit_feedback(
            selection, {SERVED: (-0.2, 0.0), "COMPLEX": (0.9, 0.003)}, session_id="s", agent_id="host"))
    assert memory.stats["closed"] == 3 and memory.stats["unknown"] == 0

    # The fourth is escalated on evidence.
    selection = turn("migrate the billing schema without downtime, attempt 3")
    assert selection.selected_model_id == "cloud-a"
    assert parse_reasoning(selection.reasoning)["bandit"] == "override"

    # Call feedback still drives exclusions through the same state: degrade, keep the override mark.
    router.report_sync({"session_id": "s", "agent_id": "host", "selected_model_id": "cloud-a",
                        "call": {"outcome": "unavailable", "latency_ms": 1}})
    degraded = turn("migrate the billing schema without downtime, attempt 4")
    assert degraded.selected_model_id == "local"
    assert parse_reasoning(degraded.reasoning)["rule"] == "target_excluded_degrade"
