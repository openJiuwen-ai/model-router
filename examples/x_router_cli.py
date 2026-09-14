"""Run x-router by hand: classify a request and see which model it picks.

    python examples/x_router_cli.py "write a bash script to rotate logs"
    python examples/x_router_cli.py "hi" "refactor this" "prove FLT"     # several at once
    echo "some long request" | python examples/x_router_cli.py           # from stdin

Environment:
    XR_CONFIG   profile to load           (default: config/x-router-example.toml)
    XR_DEVICE   override the classifier   (default: whatever the profile says)

The interpreter has to match the configured device: a CUDA device needs a torch
built with CUDA support. Mismatched, the classifier fails to load and routing
degrades to the heuristic — which this script reports rather than hiding.

    XR_DEVICE=cuda:0 path/to/cuda-python examples/x_router_cli.py "..."
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python"))

from openjiuwen import x_router  # noqa: E402


def load_profile(path):
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        import tomli as tomllib
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def main(argv):
    profile = load_profile(
        os.environ.get("XR_CONFIG", str(REPO / "config" / "x-router-example.toml"))
    )
    device = os.environ.get("XR_DEVICE")
    if device:
        profile.setdefault("x-router", {}).setdefault("classifier_model", {})["device"] = device

    started = time.time()
    try:
        router = x_router.build_router(profile)
    except Exception as exc:
        print("could not assemble the router: {0}".format(exc), file=sys.stderr)
        return 1
    settings = profile.get("x-router", {}).get("classifier_model") or {}
    model = settings.get("model_path", "-")

    print(
        "[classifier {0} device={1}  ready in {2:.1f}s]".format(
            model, settings.get("device", "auto"), time.time() - started
        ),
        file=sys.stderr,
    )

    requests = argv or [sys.stdin.read()]
    for text in requests:
        if not text.strip():
            continue
        started = time.time()
        selection = router.route_sync(
            x_router.build_request([{"role": "user", "content": text}], session_id="manual")
        )
        print(
            "{0:6.0f}ms  {1:<28} {2}".format(
                (time.time() - started) * 1000, selection.selected_model_id, selection.reasoning
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
