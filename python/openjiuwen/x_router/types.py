"""Public types for x-router: tiers, classifier transport, and parameters.

Python 3.8 compatible, matching the floor declared by this project's
``pyproject.toml``. No ``slots=``, no PEP 604 unions, no builtin generics at
runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from types import MappingProxyType
from typing import Any, Mapping, Optional

try:  # Protocol is available from 3.8; runtime_checkable keeps duck typing usable.
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - 3.7 and below are out of support
    Protocol = object  # type: ignore[assignment, misc]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls


__all__ = [
    "ComplexityLevel",
    "ClassifierRequest",
    "ComplexityBackend",
    "XRouterParams",
    "ParamsError",
]

# Fallback tier used whenever a configured tier has no model of its own.
FALLBACK_TIER = "COMPLEX"

# Below this the preview cannot hold a usable conversation window.
MIN_PREVIEW_CHARS = 256


class ParamsError(ValueError):
    """Raised at assembly time when configuration cannot produce a router."""


class ComplexityLevel(IntEnum):
    """Ordered request-complexity tiers.

    Ordering is the whole point: routing compares a classified tier against the
    configured local-capability boundary. ``IntEnum`` gives the comparisons for
    free.
    """

    SIMPLE = 1
    MEDIUM = 2
    COMPLEX = 3
    RESEARCH = 4
    REASONING = 5

    @classmethod
    def parse(cls, value):
        # type: (Any) -> ComplexityLevel
        """Parse a tier from a name or an existing member; case-insensitive."""
        if isinstance(value, cls):
            return value
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            raise ValueError("unknown complexity level: {0!r}".format(value))


@dataclass(frozen=True)
class ClassifierRequest:
    """The bounded prompt handed to a classifier backend.

    A dedicated type rather than a bare string so the transport boundary stays
    explicit: the backend receives a prompt and nothing else — no messages, no
    routing state, no host configuration.
    """

    prompt: str


@runtime_checkable
class ComplexityBackend(Protocol):
    """Classifier transport, supplied by the host.

    Synchronous on purpose: ``AlgorithmProvider.decide`` is called synchronously
    by the PyO3 adapter, so there is no event loop to await on. The host owns the
    serving stack, its endpoint shape and its credentials; x-router never sees
    them.
    """

    def classify(self, request):
        # type: (ClassifierRequest) -> str
        """Return exactly one tier label."""


@dataclass(frozen=True)
class XRouterParams:
    """Validated routing parameters.

    Constructed once at assembly time and attached to a generated subclass, since
    the algorithm slot requires no-argument construction. Validation happens here
    so a bad profile fails at ``from_config`` rather than on a live request.
    """

    local_model: str = "local"
    local_capability: Optional[ComplexityLevel] = ComplexityLevel.MEDIUM
    tier_models: Mapping[str, str] = field(default_factory=dict)
    classifier_preview_chars: int = 6000
    backend: Optional[ComplexityBackend] = None

    def __post_init__(self):
        # type: () -> None
        if not isinstance(self.local_model, str) or not self.local_model.strip():
            raise ParamsError("local_model must be a non-empty string")

        capability = self.local_capability
        if capability is not None:
            capability = ComplexityLevel.parse(capability)

        tier_models = {}
        for name, model in dict(self.tier_models).items():
            tier = ComplexityLevel.parse(name)  # rejects typos at assembly time
            if not isinstance(model, str) or not model.strip():
                raise ParamsError(
                    "tier_models[{0}] must be a non-empty model id".format(tier.name)
                )
            tier_models[tier.name] = model

        if FALLBACK_TIER not in tier_models:
            raise ParamsError(
                "tier_models must define {0}; it is the fallback for tiers with "
                "no model of their own".format(FALLBACK_TIER)
            )

        if not isinstance(self.classifier_preview_chars, int) or isinstance(
            self.classifier_preview_chars, bool
        ):
            raise ParamsError("classifier_preview_chars must be an int")
        if self.classifier_preview_chars < MIN_PREVIEW_CHARS:
            raise ParamsError(
                "classifier_preview_chars must be at least {0}".format(MIN_PREVIEW_CHARS)
            )

        if self.backend is not None and not hasattr(self.backend, "classify"):
            raise ParamsError("backend must implement classify(request) -> str")

        object.__setattr__(self, "local_capability", capability)
        object.__setattr__(self, "tier_models", MappingProxyType(tier_models))

    def model_for(self, tier):
        # type: (ComplexityLevel) -> str
        """Model configured for a tier, falling back to the fallback tier."""
        return self.tier_models.get(tier.name) or self.tier_models[FALLBACK_TIER]

    def _declared_models(self):
        # type: () -> list
        """Every model id this configuration can ever select."""
        seen = [self.local_model]
        for model in self.tier_models.values():
            if model not in seen:
                seen.append(model)
        return seen

    def validate_against(self, catalog):
        # type: (Any) -> None
        """Check every selectable model is in the router's target catalog.

        Called at assembly time so a mistyped model id fails at ``from_config``
        rather than on a live request, per the kernel's error model:
        configuration problems belong to the assembly phase.
        """
        known = set(catalog or ())
        missing = [model for model in self._declared_models() if model not in known]
        if missing:
            raise ParamsError(
                "models {0} are selectable by x-router but absent from [targets] "
                "models {1}".format(sorted(missing), sorted(known))
            )

    @classmethod
    def from_mapping(cls, config, backend=None):
        # type: (Mapping[str, Any], Optional[ComplexityBackend]) -> XRouterParams
        """Build from a ``[x-router]`` configuration section.

        The classifier is not constructed here: assembling one is a deployment
        step with its own requirements, and this is a data object. See
        ``facade.build_params``.
        """
        raw_capability = config.get("local_capability_level", "MEDIUM")
        if raw_capability is None or str(raw_capability).strip().upper() == "NONE":
            capability = None
        else:
            capability = ComplexityLevel.parse(raw_capability)

        return cls(
            local_model=config.get("local_model", "local"),
            local_capability=capability,
            tier_models=config.get("tier_models", {}) or {},
            classifier_preview_chars=int(config.get("classifier_preview_chars", 6000)),
            backend=backend,
        )
