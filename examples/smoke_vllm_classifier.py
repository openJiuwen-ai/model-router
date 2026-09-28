"""Smoke-test the in-process vLLM classifier without the native router extension.

    CUDA_VISIBLE_DEVICES=GPU_INDEX python examples/smoke_vllm_classifier.py \
      /path/to/Qwen3-0.6B "What is 2 + 2?"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python"))

from openjiuwen.x_router import backend_from_config, build_classifier_request, parse_complexity  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_path", help="classifier weights or Hugging Face model ID")
    parser.add_argument("question", nargs="?", default="What is 2 + 2?")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.2)
    args = parser.parse_args(argv)

    backend = backend_from_config({
        "kind": "vllm",
        "model_path": args.model_path,
        "gpu_memory_utilization": args.gpu_memory_utilization,
    })
    started = time.perf_counter()
    backend.warmup()
    print("model load: {0:.1f}s".format(time.perf_counter() - started), flush=True)

    request = build_classifier_request([{"role": "user", "content": args.question}], 6000)
    started = time.perf_counter()
    label = parse_complexity(backend.classify(request))
    print("{0}: {1:.1f}ms".format(label.name, (time.perf_counter() - started) * 1000))
    return 0


if __name__ == "__main__":
    sys.exit(main())
