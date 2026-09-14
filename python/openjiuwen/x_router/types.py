"""Public types for x-router: tiers, classifier transport, and parameters.

Python 3.8 compatible, matching the floor declared by this project's
``pyproject.toml``. No ``slots=``, no PEP 604 unions, no builtin generics at
runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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
    "BanditParams",
    "BanditStoreParams",
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


# Two helpers every module in this package needs. Everything that crosses the
# PyO3 boundary arrives either as a typed object or as a plain dict, depending
# on how the host built it, so "read a field from whichever shape this is" and
# "is this a usable number" are re-implemented otherwise.


def field(obj, name, default=None):
    # type: (Any, str, Any) -> Any
    """Read ``name`` from a mapping or an attribute-bearing object."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def finite(value):
    # type: (Any) -> Optional[float]
    """``value`` as a finite float, or ``None`` if it is not a real number.

    Bools are not numbers here: ``True`` arriving where a score was expected is
    a shape error to drop, not a 1.0.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


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


def _number(value, name, minimum=None, exclusive=False):
    # type: (Any, str, Optional[float], bool) -> float
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParamsError("{0} must be a number".format(name))
    number = float(value)
    if number != number:  # NaN
        raise ParamsError("{0} must be a number".format(name))
    if minimum is not None:
        if exclusive and not number > minimum:
            raise ParamsError("{0} must be greater than {1}".format(name, minimum))
        if not exclusive and number < minimum:
            raise ParamsError("{0} must be at least {1}".format(name, minimum))
    return number


@dataclass(frozen=True)
class BanditParams:
    """How neighbour evidence may override the classifier's tier.

    Absent (``XRouterParams.bandit is None``) the algorithm never reads
    ``RouteContext.retrieved`` and its output is byte-for-byte what it was before
    the bandit existed. See ``bandit.py`` for the arithmetic these feed.
    """

    min_neighbors: int = 5
    margin: float = 0.3
    lambda_c: float = 0.2
    cost_ref_usd: float = 0.005

    def __post_init__(self):
        # type: () -> None
        if isinstance(self.min_neighbors, bool) or not isinstance(self.min_neighbors, int):
            raise ParamsError("bandit.min_neighbors must be an int")
        if self.min_neighbors < 1:
            raise ParamsError("bandit.min_neighbors must be at least 1")
        object.__setattr__(self, "margin", _number(self.margin, "bandit.margin", 0.0))
        object.__setattr__(self, "lambda_c", _number(self.lambda_c, "bandit.lambda_c", 0.0))
        object.__setattr__(
            self,
            "cost_ref_usd",
            _number(self.cost_ref_usd, "bandit.cost_ref_usd", 0.0, exclusive=True),
        )

    @classmethod
    def from_mapping(cls, config):
        # type: (Optional[Mapping[str, Any]]) -> Optional[BanditParams]
        """Build from a ``[x-router.bandit]`` table; ``None`` when absent or disabled.

        The table's ``store`` sub-table belongs to the state side and is not read
        here.
        """
        if config is None:
            return None
        if not isinstance(config, Mapping):
            raise ParamsError("[x-router.bandit] must be a table")
        enabled = config.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ParamsError("bandit.enabled must be a bool")
        if not enabled:
            return None
        defaults = cls()
        return cls(
            min_neighbors=config.get("min_neighbors", defaults.min_neighbors),
            margin=config.get("margin", defaults.margin),
            lambda_c=config.get("lambda_c", defaults.lambda_c),
            cost_ref_usd=config.get("cost_ref_usd", defaults.cost_ref_usd),
        )


def _positive_int(value, name):
    # type: (Any, str) -> int
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ParamsError("{0} must be a positive int".format(name))
    return value


@dataclass(frozen=True)
class BanditStoreParams:
    """What ``BanditStore`` keeps and how it retrieves. The ``[x-router.bandit.store]`` table.

    Storage parameters only. How the neighbours are used lives in
    :class:`BanditParams`; the two are read from the same table so that one
    feature has one configuration entry, and split here because they belong to
    different slots.
    """

    retriever_dim: int = 4096
    max_entries: int = 2000
    top_k: int = 10
    min_similarity: float = 0.5
    forgetting_gamma: float = 1.0
    pending_ttl_secs: float = 7200.0
    exclusion_ttl_secs: float = 300.0
    persist: bool = False

    def __post_init__(self):
        # type: () -> None
        for name in ("retriever_dim", "max_entries", "top_k"):
            _positive_int(getattr(self, name), "bandit.store." + name)
        # The kernel caps a query's top_k at 256 and a snapshot's hits at 256.
        if self.top_k > 256:
            raise ParamsError("bandit.store.top_k must be at most 256")
        similarity = _number(self.min_similarity, "bandit.store.min_similarity", 0.0)
        if similarity > 1.0:
            raise ParamsError("bandit.store.min_similarity must be at most 1")
        gamma = _number(self.forgetting_gamma, "bandit.store.forgetting_gamma", 0.0, exclusive=True)
        if gamma > 1.0:
            raise ParamsError("bandit.store.forgetting_gamma must be at most 1")
        object.__setattr__(self, "min_similarity", similarity)
        object.__setattr__(self, "forgetting_gamma", gamma)
        object.__setattr__(
            self,
            "pending_ttl_secs",
            _number(self.pending_ttl_secs, "bandit.store.pending_ttl_secs", 0.0, exclusive=True),
        )
        object.__setattr__(
            self,
            "exclusion_ttl_secs",
            _number(self.exclusion_ttl_secs, "bandit.store.exclusion_ttl_secs", 0.0, exclusive=True),
        )
        if not isinstance(self.persist, bool):
            raise ParamsError("bandit.store.persist must be a bool")
        if self.persist:
            raise ParamsError("bandit.store.persist is not implemented yet; set it to false")

    @classmethod
    def from_mapping(cls, config):
        # type: (Optional[Mapping[str, Any]]) -> Optional[BanditStoreParams]
        """Build from a ``[x-router.bandit]`` table's ``store`` sub-table; ``None`` when absent."""
        if config is None:
            return None
        if not isinstance(config, Mapping):
            raise ParamsError("[x-router.bandit] must be a table")
        store = config.get("store")
        if store is None:
            return None
        if not isinstance(store, Mapping):
            raise ParamsError("[x-router.bandit.store] must be a table")
        defaults = cls()
        return cls(
            **{
                name: store.get(name, getattr(defaults, name))
                for name in (
                    "retriever_dim",
                    "max_entries",
                    "top_k",
                    "min_similarity",
                    "forgetting_gamma",
                    "pending_ttl_secs",
                    "exclusion_ttl_secs",
                    "persist",
                )
            }
        )


@dataclass(frozen=True)
class XRouterParams:
    """Validated routing parameters.

    Constructed once at assembly time and attached to a generated subclass, since
    the algorithm slot requires no-argument construction. Validation happens here
    so a bad profile fails at ``from_config`` rather than on a live request.
    """

    local_model: str = "local"
    local_capability: Optional[ComplexityLevel] = ComplexityLevel.MEDIUM
    tier_models: Mapping[str, str] = dataclass_field(default_factory=dict)
    classifier_preview_chars: int = 6000
    backend: Optional[ComplexityBackend] = None
    bandit: Optional[BanditParams] = None

    def __post_init__(self):
        # type: () -> None
        if not isinstance(self.local_model, str) or not self.local_model.strip():
            raise ParamsError("local_model must be a non-empty string")
        if self.bandit is not None and not isinstance(self.bandit, BanditParams):
            raise ParamsError("bandit must be a BanditParams or None")

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
            bandit=BanditParams.from_mapping(config.get("bandit")),
        )
