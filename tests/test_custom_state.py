from __future__ import annotations

import pytest  # type: ignore[import-not-found]

from openjiuwen import Outcome, StateProvider
from openjiuwen.test_algo.cost_aware import CostAwareAlgorithm


class ExclusionStore(StateProvider):
    """把 UNAVAILABLE 写入排除表；与 Rust MemoryState 行为对齐的最小 Python 实现。"""

    name = "python_exclusion_store"

    def __init__(self):
        self._exclusions = {}

    def snapshot(self, key):
        slot = (key.session_id, key.agent_id)
        return {"exclusions": list(self._exclusions.get(slot, [])), "affinity": None}

    def report(self, feedback):
        if feedback.outcome != Outcome.UNAVAILABLE:
            return
        slot = (feedback.key.session_id, feedback.key.agent_id)
        seen = self._exclusions.setdefault(slot, [])
        if feedback.selected_model_id not in seen:
            seen.append(feedback.selected_model_id)


class FeedbackRecorder(StateProvider):
    name = "feedback_recorder"

    def __init__(self):
        self.events = []

    def snapshot(self, key):
        return {}

    def report(self, feedback):
        self.events.append(feedback)


def feedback_router():
    from openjiuwen import Router
    store = FeedbackRecorder()
    router = Router.from_config({
        "algorithm": "passthrough", "state": {"backend": "memory"},
        "targets": {"models": ["model"]},
    }, state=store)
    return router, store


def test_feedback_cross_language_roundtrip():
    from openjiuwen import CallFeedback, Extension, Feedback, RoutingKey
    router, store = feedback_router()
    decision = router.route_sync({})
    assert decision.route_id
    assert router.route_sync({}).route_id != decision.route_id
    data = {"all": [None, True, False, -(2**63), 2**63 - 1, 1.25, "中文", {"nested": []}]}
    fb = Feedback(RoutingKey("s", "a"), "model", call=CallFeedback("ok", None, False),
                  event_id="evt", route_id=decision.route_id, observed_at_ms=2**64 - 1,
                  extensions=[Extension("unknown.vendor", "1", data)])
    expected = fb.to_dict()
    router.report_sync(fb)
    assert store.events[-1].to_dict() == expected
    router.report_sync(expected)
    assert store.events[-1].to_dict() == expected
    assert Feedback.from_dict(expected).to_dict() == expected
    assert store.events[-1].extensions[0].data == data
    router.report_sync(Feedback(RoutingKey(), "model", call=None, extensions=[]))
    assert store.events[-1].call is None
    assert store.events[-1].outcome is None


def test_feedback_legacy_apis():
    from openjiuwen import Feedback, RoutingKey, ModelSelection
    router, store = feedback_router()
    fb = Feedback(RoutingKey("s", "a"), "model", "unavailable", 12, False)
    fb.outcome = "ok"
    fb.latency_ms = 14
    fb.cache_valid = True
    router.report_sync(fb)
    assert store.events[-1].call.outcome == "ok"
    assert store.events[-1].latency_ms == 14
    assert store.events[-1].cache_valid is True
    router.report_sync({"session_id": "s", "agent_id": "a", "selected_model_id": "model"})
    assert store.events[-1].latency_ms == 0
    assert store.events[-1].event_id is None
    decision = router.route_sync({})
    assert Feedback.ok(decision, 1).route_id == decision.route_id
    assert Feedback.ok(ModelSelection("model", "old"), 1).route_id is None
    assert Feedback.ok({"target": "model", "route_id": "dict-id"}, 1).route_id == "dict-id"


@pytest.mark.parametrize("data", [float("nan"), float("inf"), -float("inf"), 2**63, -(2**63)-1,
                                  {1: "bad"}, (1, 2), {1, 2}, b"bytes", object(), "x" * 65537,
                                  [None] * 256])
def test_feedback_invalid_json_rejected(data):
    from openjiuwen import Extension, Feedback, RoutingKey
    router, store = feedback_router()
    raw = {"selected_model_id": "model", "call": None,
           "extensions": [{"schema": "unknown", "version": "1", "data": data}]}
    for operation in [lambda: router.report_sync(raw), lambda: Feedback.from_dict(raw),
                      lambda: Feedback(RoutingKey(), "model", call=None, extensions=raw["extensions"]),
                      lambda: Extension("unknown", "1", data)]:
        with pytest.raises((ValueError, TypeError, OverflowError)):
            operation()
    assert store.events == []


def test_feedback_json_boundary_and_typed_validation():
    from openjiuwen import CallFeedback, Extension, Feedback, RoutingKey
    value = None
    for _ in range(7):
        value = [value]
    assert Extension("s", "1", value).data == value
    assert len(Extension("s", "1", "x" * 65534).data) == 65534
    for value in [True, -1, 2**64, 1.5]:
        for operation in [lambda: CallFeedback("ok", value),
                          lambda: Feedback(RoutingKey(), "m", latency_ms=value),
                          lambda: Feedback.ok({"target": "m"}, value)]:
            with pytest.raises((ValueError, TypeError, OverflowError)):
                operation()
    for operation in [lambda: CallFeedback("ok", cache_valid=1),
                      lambda: Feedback(RoutingKey(), "m", cache_valid=1)]:
        with pytest.raises((ValueError, TypeError)):
            operation()


def test_feedback_depth_cycles_and_total_limits():
    from openjiuwen import Feedback
    router, store = feedback_router()
    cycle = []
    cycle.append(cycle)
    deep = None
    for _ in range(8):
        deep = [deep]
    for data in [cycle, deep]:
        with pytest.raises(ValueError):
            router.report_sync({"selected_model_id": "m", "extensions": [
                {"schema": "s", "version": "1", "data": data}]})
    for extensions in [
        [{"schema": "s", "version": "1", "data": None}] * 33,
        [{"schema": "s", "version": "1", "data": "x" * 40000}] * 2,
        [{"schema": "s", "version": "1", "data": [None] * 150}] * 2,
    ]:
        with pytest.raises(ValueError):
            Feedback.from_dict({"selected_model_id": "m", "extensions": extensions})
    assert store.events == []


@pytest.mark.parametrize("fields", [
    {"version": 2}, {"version": 2**32}, {"version": True}, {"observed_at_ms": -1},
    {"observed_at_ms": 2**64}, {"call": {"outcome": "bad"}},
    {"call": {"outcome": "ok", "latency_ms": -1}},
    {"call": {"outcome": "ok", "latency_ms": True}},
    {"call": {"outcome": "ok", "cache_valid": 1}},
    {"call": None, "outcome": "ok"},
    {"extensions": [{"schema": "", "version": "1", "data": None}]},
])
def test_feedback_invalid_fields(fields):
    from openjiuwen import Feedback
    router, store = feedback_router()
    raw = {"selected_model_id": "model", **fields}
    with pytest.raises((ValueError, TypeError, OverflowError)):
        router.report_sync(raw)
    with pytest.raises((ValueError, TypeError, OverflowError)):
        Feedback.from_dict(raw)
    assert store.events == []


def test_python_state_provider_is_not_an_algorithm():
    assert not hasattr(ExclusionStore(), "decide")
    assert issubclass(ExclusionStore, StateProvider)


def test_custom_state_injected_on_from_config():
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import Feedback, Router

    store = ExclusionStore()
    router = Router.from_config(
        {
            "algorithm": "passthrough",
            "state": {"backend": "memory"},
            "targets": {"models": ["fast-local", "strong-cloud"]},
        },
        state=store,
    )
    req = {"session_id": "s-py-state", "agent_id": "host"}
    first = router.route_sync(req)
    assert first.selected_model_id == "fast-local"
    router.report_sync(
        Feedback.ok(
            first,
            latency_ms=1,
            session_id="s-py-state",
            agent_id="host",
            outcome=Outcome.UNAVAILABLE,
        )
    )
    second = router.route_sync(req)
    assert second.selected_model_id == "strong-cloud"


def test_register_state_selected_by_backend_name():
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import Feedback, Router, register_state

    store = ExclusionStore()
    assert register_state(store) == "python_exclusion_store"
    router = Router.from_config(
        {
            "algorithm": "passthrough",
            "state": {"backend": "python_exclusion_store"},
            "targets": {"models": ["fast-local", "strong-cloud"]},
        }
    )
    req = {"session_id": "s-named-state", "agent_id": "host"}
    first = router.route_sync(req)
    router.report_sync(
        Feedback.ok(
            first,
            latency_ms=1,
            session_id="s-named-state",
            agent_id="host",
            outcome=Outcome.UNAVAILABLE,
        )
    )
    second = router.route_sync(req)
    assert second.selected_model_id == "strong-cloud"


def test_custom_state_works_with_python_algorithm():
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import Router

    router = Router.from_config(
        {
            "algorithm": "python_cost_aware",
            "state": {"backend": "memory"},
            "targets": {"models": ["fast-expensive", "slow-cheap"]},
        },
        state=ExclusionStore(),
    )
    assert router.algorithm_name() == CostAwareAlgorithm.name
    decision = router.route_sync({"session_id": "s-mix", "agent_id": "host"})
    assert decision.selected_model_id == "fast-expensive"


class RetrievalStore(StateProvider):
    """实现了可选 `query`，按文本命中固定条目并回传检索结果。"""

    name = "python_retrieval_store"

    def __init__(self):
        self.calls = []

    def snapshot(self, key):
        return {"exclusions": [], "affinity": "snapshot-affinity"}

    def report(self, feedback):
        pass

    def query(self, key, query):
        self.calls.append((key.session_id, query.text, query.top_k))
        return {
            "view": {"affinity": "query-affinity"},
            "retrieved": [
                {"id": "doc-1", "score": 0.9, "data": {"model": "strong-cloud"}},
                {"id": "doc-2", "score": 0.5},
            ][: query.top_k or 2],
        }


class ExplodingQueryStore(StateProvider):
    """`query` 抛异常：验证 runtime 降级为 snapshot，不阻断路由。"""

    name = "python_exploding_query"

    def snapshot(self, key):
        return {"exclusions": [], "affinity": "fallback-affinity"}

    def report(self, feedback):
        pass

    def query(self, key, query):
        raise RuntimeError("backend down")


def retrieval_router(store):
    from openjiuwen import Router
    return Router.from_config(
        {
            "algorithm": "python_cost_aware",
            "state": {"backend": "memory"},
            "targets": {"models": ["fast-expensive", "slow-cheap"]},
        },
        state=store,
    )


def test_legacy_state_without_query_still_degrades():
    """旧 Python StateProvider 不实现 query：结果只受 exclusions 影响。"""
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import RouteHint, Router, StateQuery

    store = ExclusionStore()
    router = Router.from_config(
        {
            "algorithm": "passthrough",
            "state": {"backend": "memory"},
            "targets": {"models": ["fast-local", "strong-cloud"]},
        },
        state=store,
    )
    req = {"session_id": "s-legacy-query", "agent_id": "host"}
    # 即便携带检索入参，未实现 query 的旧插件也必须照常工作。
    decision = router.route_sync(req, RouteHint(state_query=StateQuery.text_query("hi")))
    assert decision.selected_model_id == "fast-local"
    assert router.route_sync(req).selected_model_id == "fast-local"


def test_state_query_reaches_python_and_returns_retrieved():
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import RetrievedItem, RouteHint, Router, StateQuery

    store = RetrievalStore()
    router = retrieval_router(store)
    hint = RouteHint(state_query=StateQuery(text="caching strategy", top_k=2))
    decision = router.route_sync({"session_id": "s-knn", "agent_id": "host"}, hint)
    assert decision.selected_model_id == "fast-expensive"
    assert store.calls == [("s-knn", "caching strategy", 2)]

    # dict 形式入参同样被解析。
    router.route_sync(
        {"session_id": "s-knn2", "agent_id": "host"},
        {"state_query": {"text": "vectors", "vector": [0.1, 0.2], "top_k": 1}},
    )
    assert store.calls[-1] == ("s-knn2", "vectors", 1)

    item = RetrievedItem("doc-1", 0.9, {"model": "strong-cloud"})
    assert item.id == "doc-1"
    assert item.score == pytest.approx(0.9)
    assert item.data == {"model": "strong-cloud"}


def test_state_query_carries_the_route_id_the_selection_returns():
    """runtime 在调 `query` 前生成 route_id 并注入，state 与宿主看到的是同一个值。"""
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import Feedback, RouteHint, Router, StateProvider, StateQuery

    class IdRecorder(StateProvider):
        name = "python_id_recorder"

        def __init__(self):
            self.query_ids = []
            self.report_ids = []

        def snapshot(self, key):
            return {}

        def query(self, key, query):
            self.query_ids.append(query.route_id)
            return {"view": {}, "retrieved": []}

        def report(self, feedback):
            self.report_ids.append(feedback.route_id)

    store = IdRecorder()
    router = Router.from_config({
        "algorithm": "passthrough", "state": {"backend": "memory"},
        "targets": {"models": ["model"]},
    }, state=store)

    # 宿主构造的查询里 route_id 始终为空，且不可赋值。
    query = StateQuery(text="hello")
    assert query.route_id is None
    with pytest.raises(AttributeError):
        query.route_id = "host-filled"

    selection = router.route_sync({"session_id": "s", "agent_id": "a"}, RouteHint(state_query=query))
    assert selection.route_id
    assert store.query_ids == [selection.route_id]

    # 同一个 id 随 Feedback 回到 state，形成 query → report 的关联。
    router.report_sync(Feedback.ok(selection, 5, session_id="s", agent_id="a"))
    assert store.report_ids == [selection.route_id]

    # 没有检索意图：不调 query，id 照常生成且不同。
    plain = router.route_sync({"session_id": "s", "agent_id": "a"})
    assert plain.route_id and plain.route_id != selection.route_id
    assert store.query_ids == [selection.route_id]


def test_query_failure_falls_back_to_snapshot():
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import RouteHint, Router, StateQuery

    router = retrieval_router(ExplodingQueryStore())
    decision = router.route_sync(
        {"session_id": "s-fallback", "agent_id": "host"},
        RouteHint(state_query=StateQuery.text_query("boom")),
    )
    # 降级后仍是正常路由结果，不被后端异常阻断。
    assert decision.selected_model_id == "fast-expensive"


def test_state_query_validation_rejects_out_of_bounds():
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import StateQuery

    for kwargs in [
        {"top_k": 0},
        {"top_k": 257},
        {"text": "x" * 16385},
        {"vector": []},
        {"vector": [float("nan")]},
        {"vector": [0.0] * 4097},
    ]:
        with pytest.raises((ValueError, TypeError, OverflowError)):
            StateQuery(**kwargs)


def test_retrieved_item_validation_rejects_bad_payload():
    pytest.importorskip("openjiuwen._openjiuwen")
    from openjiuwen import RetrievedItem

    for args in [
        ("doc", float("inf"), None),
        ("doc", float("nan"), None),
        ("doc", 0.0, {1: "non-string-key"}),
        ("doc", 0.0, "x" * 65537),
    ]:
        with pytest.raises((ValueError, TypeError, OverflowError)):
            RetrievedItem(*args)

