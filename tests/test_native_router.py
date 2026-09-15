from __future__ import annotations

import pytest  # type: ignore[import-not-found]

pytest.importorskip("openjiuwen._openjiuwen")

from openjiuwen import Feedback, Message, Outcome, RequestMetadata, RouteHint, RouteRequest, Router


PROFILE = {
    "algorithm": "passthrough",
    "state": {"backend": "memory"},
    "targets": {"models": ["fast-local", "strong-cloud"]},
}


def test_from_config_dict_routes_first_target():
    router = Router.from_config(PROFILE)
    assert router.algorithm_name() == "passthrough"
    decision = router.route_sync(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "session_id": "s1",
            "agent_id": "a1",
        }
    )
    assert decision.selected_model_id == "fast-local"
    assert decision.target == "fast-local"
    assert decision.is_answer_call is True


def test_report_unavailable_excludes_on_next_route():
    router = Router.from_config(PROFILE)
    req = RouteRequest(
        messages=[Message("user", "hi")],
        metadata=RequestMetadata(session_id="s1", agent_id="a1"),
    )
    first = router.route_sync(req, RouteHint())
    assert first.selected_model_id == "fast-local"
    router.report_sync(
        Feedback.ok(
            first,
            latency_ms=1,
            key=req.routing_key(),
            outcome=Outcome.UNAVAILABLE,
        )
    )
    second = router.route_sync(req)
    assert second.selected_model_id == "strong-cloud"


def test_async_route_and_report():
    import asyncio

    async def body():
        router = Router.from_config(PROFILE)
        decision = await router.route({"session_id": "async", "agent_id": "a"})
        assert decision.selected_model_id == "fast-local"
        await router.report(
            Feedback.ok(decision, latency_ms=3, session_id="async", agent_id="a")
        )

    asyncio.run(body())


def test_python_algorithm_subclass_routes():
    from openjiuwen.test_algo.cost_aware import CostAwareAlgorithm

    class AlphaBeta(CostAwareAlgorithm):
        name = "python_alpha_beta"
        costs = {"alpha": 1.0, "beta": 10.0}

    router = Router.from_toml(
        """
algorithm = "python_alpha_beta"
[state]
backend = "memory"
[targets]
models = ["alpha", "beta"]
"""
    )
    decision = router.route_sync({"exclusions": ["alpha"]})
    assert decision.selected_model_id == "beta"
    assert "python_cost_aware" in decision.reasoning


def test_stage_router_reads_openai_tool_calls():
    router = Router.from_config(
        {
            "algorithm": "stage_router",
            "state": {"backend": "memory"},
            "targets": {"models": ["efficient", "capable"]},
        }
    )
    decision = router.route_sync(
        {
            "messages": [
                {"role": "user", "content": "fix it"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "Bash", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": "fatal: out of memory"},
            ]
        }
    )
    assert decision.selected_model_id == "capable"
    assert "source=override" in decision.reasoning


def test_remote_state_from_profile():
    router = Router.from_config(
        {
            "algorithm": "passthrough",
            "state": {
                "backend": "remote",
                "endpoint": "http://127.0.0.1:9",
                "timeout_ms": 5,
            },
            "targets": {"models": ["only"]},
        },
    )
    assert router.route_sync({}).selected_model_id == "only"
