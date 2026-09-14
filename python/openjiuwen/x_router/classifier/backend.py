"""Adapts the engine to the algorithm's ``ComplexityBackend`` protocol.

The classifier runs in the router's own process. There is no server and no
transport: classification is a function call. A host that needs something else —
a remote classifier, a different model stack — injects its own backend instead,
which is what the protocol is for.
"""

from __future__ import annotations

from typing import Any, Optional

from ..types import ParamsError
from .engine import ClassifierEngine

__all__ = ["LocalBackend", "backend_from_config"]


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


def backend_from_config(config):
    # type: (Any) -> Optional[LocalBackend]
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
    model_path = section.get("model_path")
    if not model_path:
        raise ParamsError(
            "[classifier_model] has no model_path; set one, or set "
            "enabled = false to run on the heuristic classifier instead"
        )
    return LocalBackend(
        model_path=model_path,
        max_tokens=section.get("max_tokens", 16),
        device=section.get("device", "auto"),
        dtype=section.get("dtype", "auto"),
        max_input_tokens=section.get("max_input_tokens", 4096),
    )
