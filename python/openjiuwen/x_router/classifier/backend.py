"""In-process classifier backends for the ``ComplexityBackend`` protocol."""

from __future__ import annotations

import threading
from typing import Any

from ..types import ComplexityLevel, ParamsError
from .engine import ClassifierEngine, EngineError

__all__ = ["LocalBackend", "VllmBackend", "backend_from_config"]


class LocalBackend(object):
    """Runs the classifier in this process.

    The prompt is wrapped as a user turn and rendered with the model's chat
    template, so what the model sees is defined here and nowhere else.
    """

    def __init__(self, model_path=None, engine=None, max_tokens=16, **engine_kwargs):
        # type: (Any, Any, int, Any) -> None
        self.engine = engine if engine is not None else ClassifierEngine(model_path, **engine_kwargs)
        self.max_tokens = int(max_tokens)

    def classify(self, request):
        # type: (Any) -> str
        return self.engine.classify_text(request.prompt, max_new_tokens=self.max_tokens)

    def warmup(self):
        # type: () -> None
        """Load weights now rather than on the first routed request."""
        self.engine.load()


class VllmBackend(object):
    """Run the classifier with vLLM's offline API, without an HTTP server."""

    def __init__(
        self,
        model_path,
        max_tokens=16,
        dtype="auto",
        max_model_len=4096,
        gpu_memory_utilization=0.9,
    ):
        # type: (str, int, str, int, float) -> None
        if not model_path or not str(model_path).strip():
            raise ParamsError("classifier_model.model_path is required")
        self.model_path = str(model_path)
        self.max_tokens = int(max_tokens)
        self.dtype = dtype
        self.max_model_len = int(max_model_len)
        self.gpu_memory_utilization = float(gpu_memory_utilization)
        self._load_lock = threading.Lock()
        self._llm = None
        self._sampling_params = None

    def warmup(self):
        # type: () -> None
        """Load weights once, before the first routed request."""
        if self._llm is not None:
            return
        with self._load_lock:
            if self._llm is not None:
                return
            try:
                from vllm import LLM, SamplingParams
                from vllm.sampling_params import StructuredOutputsParams
            except ImportError as exc:
                raise EngineError(
                    "the vLLM classifier needs the vllm package; "
                    "install with the x-router-vllm extra"
                ) from exc

            sampling_params = SamplingParams(
                temperature=0,
                max_tokens=self.max_tokens,
                structured_outputs=StructuredOutputsParams(
                    choice=[level.name for level in ComplexityLevel]
                ),
            )
            llm = LLM(
                model=self.model_path,
                dtype=self.dtype,
                max_model_len=self.max_model_len,
                gpu_memory_utilization=self.gpu_memory_utilization,
            )
            self._sampling_params = sampling_params
            self._llm = llm

    def classify(self, request):
        # type: (Any) -> str
        self.warmup()
        outputs = self._llm.chat(
            [{"role": "user", "content": request.prompt}],
            sampling_params=self._sampling_params,
            chat_template_kwargs={"enable_thinking": False},
            use_tqdm=False,
        )
        return outputs[0].outputs[0].text.strip()


def backend_from_config(config):
    # type: (Any) -> Any
    """Build the classifier from a ``[x_router.classifier_model]`` section.

    The classifier is required by default. An absent section, or one with no
    weights, is a deployment mistake rather than a request for heuristic mode:
    left to resolve itself it would produce a router that silently never
    escalates, which looks healthy from the outside.

    Returns ``None`` only for ``enabled = false``, where running without a model
    is what the operator asked for.
    """
    section = dict(config or {})
    if not section:
        raise ParamsError(
            "x-router needs a [classifier_model] section with model_path; "
            "set enabled = false to run on the heuristic classifier instead"
        )
    if not section.get("enabled", True):
        return None
    kind = section.get("kind", "local")
    if kind not in ("local", "vllm"):
        raise ParamsError("classifier_model.kind must be 'local' or 'vllm', got {0!r}".format(kind))
    model_path = section.get("model_path")
    if not model_path:
        raise ParamsError(
            "[classifier_model] has no model_path; set one, or set "
            "enabled = false to run on the heuristic classifier instead"
        )
    if kind == "vllm":
        return VllmBackend(
            model_path=model_path,
            max_tokens=section.get("max_tokens", 16),
            dtype=section.get("dtype", "auto"),
            max_model_len=section.get("max_model_len", 4096),
            gpu_memory_utilization=section.get("gpu_memory_utilization", 0.9),
        )
    return LocalBackend(
        model_path=model_path,
        max_tokens=section.get("max_tokens", 16),
        device=section.get("device", "auto"),
        dtype=section.get("dtype", "auto"),
        max_input_tokens=section.get("max_input_tokens", 4096),
    )
