"""Watch x-router's bandit learn: the same request family, turn after turn.

    python examples/x_router_bandit.py            # 12 turns, default settings
    python examples/x_router_bandit.py 30         # more turns
    XR_MARGIN=0.6 python examples/x_router_bandit.py

Everything is simulated so it runs anywhere in a second: the classifier, the
models, the judge. What is real is the router — kernel, BanditStore, bandit
rule, XRouterService — and the two calls a host makes.

The story: for one family of requests the classifier is unsure and says
MEDIUM (local) two times out of three, COMPLEX (cloud) the third. The local
model does badly on them, the cloud model does well. After a few scored turns
the store has seen both, and the bandit starts escalating the MEDIUM ones on
evidence — `bandit=override` in the reasoning line.

To run it against real components instead: drop `backend=` to load the
classifier from the profile, drop `judge=` to use `[x-router.judge_model]`,
and call your model where `Models.answer` is called.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import Future
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python"))

from openjiuwen import x_router  # noqa: E402

PROFILE = {
    "algorithm": "x-router-bandit-demo",
    "state": {"backend": "x-router-bandit"},
    "targets": {"models": ["local", "cloud-fast", "cloud-deep", "cloud-reasoning"]},
    "x-router": {
        "local_capability_level": "MEDIUM",
        "local_model": "local",
        "tier_models": {"COMPLEX": "cloud-fast", "RESEARCH": "cloud-deep", "REASONING": "cloud-reasoning"},
        "classifier_model": {"enabled": False},          # a stub is injected below
        "bandit": {
            "min_neighbors": 3,
            "margin": float(os.environ.get("XR_MARGIN", "0.3")),
            "lambda_c": 0.2,
            "cost_ref_usd": 0.005,
            "store": {"retriever_dim": 1024, "min_similarity": 0.3},
        },
    },
}

REQUESTS = [
    "migrate the billing schema to the new tenant model without downtime",
    "migrate the billing tables to the new tenant layout with zero downtime",
    "plan a zero-downtime migration of the billing schema for multi-tenancy",
    "move billing to the multi-tenant schema without taking the service down",
]


class UnsureClassifier:
    """Says MEDIUM twice, then COMPLEX once, and repeats."""

    def __init__(self):
        self.calls = 0

    def classify(self, request):
        self.calls += 1
        return "COMPLEX" if self.calls % 3 == 0 else "MEDIUM"


class Models:
    """Two mock models: local waffles, cloud delivers. Cost is what the bandit weighs."""

    @staticmethod
    def answer(model_id, messages):
        if model_id == "local":
            return "I cannot safely plan that migration from here.", 0.0
        return "Plan: shadow tables, dual writes, backfill, cut over behind a flag.", 0.003


class SimJudge:
    """Stands in for a judge model: reads the response and grades it."""

    def score(self, request):
        good = "Plan:" in request.user_prompt
        return '{"task_progress": %s, "correctness": %s, "grounding": 1}' % ((1, 1) if good else (-1, -1))


class Inline:
    """Score on the calling thread so the printout is in order. A real host keeps the default pool."""

    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    def shutdown(self, wait=True):
        pass


def main(argv):
    turns = int(argv[0]) if argv else 12
    try:
        svc = x_router.build_service(PROFILE, backend=UnsureClassifier(), judge=SimJudge(), executor=Inline())
    except Exception as exc:  # numpy missing, most likely
        print("could not assemble the service: {0}".format(exc), file=sys.stderr)
        return 1

    print("{0:>4}  {1:<9} {2:<8} {3:<9} {4:>4}  {5}".format("turn", "tier_llm", "bandit", "tier", "nbrs", "model"))
    with svc:
        for turn in range(turns):
            messages = [{"role": "user", "content": REQUESTS[turn % len(REQUESTS)]}]
            selection = svc.route(messages, session_id="demo", agent_id="example")
            response, cost = Models.answer(selection.selected_model_id, messages)
            svc.report(selection, messages=messages, response_text=response, cost_usd=cost,
                       latency_ms=120, session_id="demo", agent_id="example")

            r = x_router.parse_reasoning(selection.reasoning)
            print("{0:>4}  {1:<9} {2:<8} {3:<9} {4:>4}  {5}".format(
                turn + 1, r["tier_llm"], r["bandit"], r["tier"], r["neighbors"], selection.selected_model_id))
        stats = svc.stats
    print("\nscored {settled}, judge failures {judge_failed}; store: {closed} records, "
          "{unknown} unknown".format(closed=stats["store"]["closed"], unknown=stats["store"]["unknown"], **stats))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
