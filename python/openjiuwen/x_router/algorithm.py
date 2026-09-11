"""The algorithm-slot plugin.

Registration happens at class definition (``AlgorithmProvider.__init_subclass__``),
so importing this module is enough to make ``algorithm = "x-router"`` resolvable.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence, Tuple, Type

from ..algorithm_provider import AlgorithmProvider
from .complexity import classify
from .types import ComplexityLevel, ParamsError, XRouterParams

__all__ = [
    "RULE_ESCALATE_CLOUD",
    "RULE_TARGET_EXCLUDED_DEGRADE",
    "RULE_WITHIN_LOCAL_CAPABILITY",
    "XRouter",
    "decide_by_tier",
    "specialize",
]

_UNSAFE = re.compile(r"[^0-9A-Za-z_]+")

# Which routing rule fired. Reported in Decision.reasoning; see README.
RULE_WITHIN_LOCAL_CAPABILITY = "within_local_capability"
RULE_ESCALATE_CLOUD = "escalate_cloud"
RULE_TARGET_EXCLUDED_DEGRADE = "target_excluded_degrade"


def decide_by_tier(tier, targets, params):
    # type: (ComplexityLevel, Sequence[str], XRouterParams) -> Tuple[str, str]
    """Pick a model for a tier. Returns (model_id, rule_code).

    The deterministic half of the decision: no I/O, no clock, no global
    randomness. ``local_capability is None`` means the local tier is disabled,
    so everything escalates and the local model survives only as a degrade
    target.
    """
    capability = params.local_capability
    if capability is not None and tier <= capability:
        return _pick(params.local_model, targets), RULE_WITHIN_LOCAL_CAPABILITY

    model = params.model_for(tier)
    if model not in targets:
        # State feedback excluded this model for the current routing key.
        # Degrading is always safe; escalating further is not.
        return _pick(params.local_model, targets), RULE_TARGET_EXCLUDED_DEGRADE
    return model, RULE_ESCALATE_CLOUD


def _pick(preferred, targets):
    # type: (str, Sequence[str]) -> str
    """The local target can itself be excluded; fall back to what is left."""
    return preferred if preferred in targets else targets[0]


class XRouter(AlgorithmProvider):
    """Route by classified conversation complexity.

    ``decide()`` performs LLM calls through the injected backend. That is 
    deliberate — the routing decision is what the I/O is for — but it has two
    consequences.

    ``route()`` blocks for up to the backend's timeout, so an async host must
    not call it from a coroutine.

    ``check_purity`` must not be used here. It has no useful mode: against a
    live classifier it fails intermittently, because serving stacks do not all
    decode deterministically — some clamp temperature to a small positive floor
    instead of decoding greedily, and batched inference is not bitwise
    reproducible, so adjacent tiers flip on a near tie; against a stubbed backend
    it always passes and proves nothing. The deterministic half of the decision is covered directly instead,
    by the truth table over :func:`decide_by_tier`. See DESIGN.md section 3.7.

    The slot requires no-argument construction, so parameters live on the class.
    Use :func:`specialize` rather than setting them by hand.
    """

    name = "x-router"
    params = None  # type: Optional[XRouterParams]

    def decide(self, request, ctx):
        # type: (Any, Any) -> dict
        params = type(self).params
        if params is None:
            raise ParamsError(
                "x-router is unconfigured; assemble it with "
                "openjiuwen.x_router.build_router() or specialize() first"
            )

        targets = list(getattr(ctx, "targets", None) or ())
        if not targets:
            # Mapped to RouterError::NoTarget by the PyO3 adapter.
            raise ValueError("no available target")

        messages = getattr(request, "messages", None)
        if messages is None and isinstance(request, dict):
            messages = request.get("messages")

        tier, source = classify(messages or (), params)
        model, rule = decide_by_tier(tier, targets, params)

        return {
            "selected_model_id": model,
            "reasoning": "x-router: rule={0} tier={1} source={2}".format(
                rule, tier.name, source
            ),
            "is_answer_call": True,
        }


def specialize(params, name="x-router", base=XRouter):
    # type: (XRouterParams, str, Type[XRouter]) -> Type[XRouter]
    """Create a configured subclass and register it under ``name``.

    Works around the slot's no-argument construction rule: parameters cannot be
    passed to a constructor, so a subclass carrying them is generated instead.
    Defining the subclass is what registers it.

    The registry is a process-global ``name -> instance`` map and re-registering
    a name overwrites it, so names must be stable and unique per deployment.
    Specializing under ``"x-router"`` deliberately replaces the unconfigured
    default.
    """
    if not isinstance(params, XRouterParams):
        raise ParamsError("params must be an XRouterParams")
    if not isinstance(name, str) or not name.strip():
        raise ParamsError("algorithm name must be a non-empty string")

    class_name = "XRouter_{0}".format(_UNSAFE.sub("_", name))
    return type(class_name, (base,), {"name": name, "params": params})
