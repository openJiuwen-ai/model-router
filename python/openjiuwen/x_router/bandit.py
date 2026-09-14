"""The bandit half of x-router: override the classifier's tier on neighbour evidence.

Pure. The only inputs are the classifier's tier, ``RouteContext.retrieved`` and
the parameters, so unlike the classifier half this is table-testable.

``retrieved`` is produced by a state that answers ``StateQuery`` with the
outcomes of similar past conversations (``BanditStore`` ships one; anything that
returns the same item shape works). Each item carries

    score = similarity, already discounted by the state for age
    data  = {"observations": {"<TIER>": {"quality": q, "cost_usd": c}, ...}}

and this module turns those into a per-tier utility

    U(t) = mean_quality(t) - lambda_c * mean_cost(t) / cost_ref_usd

weighting every neighbour by its score. The classifier's tier is replaced only
when enough neighbours were found, the incumbent tier itself has evidence, and
the best tier beats it by more than ``margin``. Evidence that is missing or
malformed is skipped rather than raised on: state data is a hint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .types import BanditParams, ComplexityLevel, field, finite

__all__ = [
    "BANDIT_COLD",
    "BANDIT_IGNORE",
    "BANDIT_OFF",
    "BANDIT_OVERRIDE",
    "BANDIT_SAME",
    "OBSERVATIONS_KEY",
    "TierEvidence",
    "aggregate",
    "choose_tier",
    "parse_observations",
    "utilities",
]

# What the bandit did, relative to the classifier's tier. Reported in
# Decision.reasoning as ``bandit=<code>``.
BANDIT_OFF = "off"            # not configured; classifier tier used as is
BANDIT_COLD = "cold"          # fewer usable neighbours than min_neighbors
BANDIT_SAME = "same"          # the evidence's best tier is the classifier's
BANDIT_IGNORE = "ignore"      # evidence pointed elsewhere but not past margin, or had nothing on the classifier's tier
BANDIT_OVERRIDE = "override"  # the classifier's tier was replaced

# Key under RetrievedItem.data holding the per-tier observations.
OBSERVATIONS_KEY = "observations"


@dataclass
class TierEvidence:
    """Weighted evidence for one tier, accumulated over the neighbours."""

    count: int = 0
    weight: float = 0.0
    quality_sum: float = 0.0     # sum of weight * quality
    cost_weight: float = 0.0     # weight of neighbours that reported a cost
    cost_sum: float = 0.0        # sum of weight * cost over those

    def add(self, weight, quality, cost):
        # type: (float, float, Optional[float]) -> None
        self.count += 1
        self.weight += weight
        self.quality_sum += weight * quality
        if cost is not None:
            self.cost_weight += weight
            self.cost_sum += weight * cost

    @property
    def mean_quality(self):
        # type: () -> Optional[float]
        return self.quality_sum / self.weight if self.weight > 0 else None

    @property
    def mean_cost(self):
        # type: () -> Optional[float]
        return self.cost_sum / self.cost_weight if self.cost_weight > 0 else None


def parse_observations(data):
    # type: (Any) -> Dict[ComplexityLevel, Tuple[float, Optional[float]]]
    """Parse ``{"observations": {tier: {quality, cost_usd}}}``; malformed entries are dropped.

    Shared by the algorithm (reading ``RetrievedItem.data``) and the store
    (reading the ``x-router.bandit`` feedback extension), so the two never
    disagree about what an observation is. A negative or non-finite cost counts
    as unknown; a non-finite quality drops the entry.
    """
    if not isinstance(data, Mapping):
        return {}
    raw = data.get(OBSERVATIONS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    parsed = {}  # type: Dict[ComplexityLevel, Tuple[float, Optional[float]]]
    for name, observation in raw.items():
        if not isinstance(observation, Mapping):
            continue
        try:
            tier = ComplexityLevel.parse(name)
        except ValueError:
            continue
        quality = finite(observation.get("quality"))
        if quality is None:
            continue
        cost = finite(observation.get("cost_usd"))
        if cost is not None and cost < 0:
            cost = None
        parsed[tier] = (quality, cost)
    return parsed


def aggregate(retrieved):
    # type: (Sequence[Any]) -> Tuple[int, Dict[ComplexityLevel, TierEvidence]]
    """Fold neighbours into per-tier evidence.

    Returns ``(neighbours, evidence)`` where ``neighbours`` counts the items that
    contributed at least one usable observation. Items with a non-positive or
    non-finite score carry no weight and are not counted.
    """
    evidence = {}  # type: Dict[ComplexityLevel, TierEvidence]
    neighbours = 0
    for item in retrieved or ():
        weight = finite(field(item, "score"))
        if weight is None or weight <= 0:
            continue
        observations = parse_observations(field(item, "data"))
        if not observations:
            continue
        neighbours += 1
        for tier, (quality, cost) in observations.items():
            evidence.setdefault(tier, TierEvidence()).add(weight, quality, cost)
    return neighbours, evidence


def utilities(evidence, params):
    # type: (Mapping[ComplexityLevel, TierEvidence], BanditParams) -> Dict[ComplexityLevel, float]
    """Per-tier utility. Tiers without a usable mean are left out.

    When cost carries weight (``lambda_c > 0``), a tier whose neighbours never
    reported a cost is left out too: scoring it at zero cost would hand it the
    whole cost term for free. Hosts should report ``cost_usd = 0.0`` for free
    tiers rather than omit it.
    """
    result = {}  # type: Dict[ComplexityLevel, float]
    for tier, agg in evidence.items():
        quality = agg.mean_quality
        if quality is None:
            continue
        cost = agg.mean_cost
        if params.lambda_c > 0:
            if cost is None:
                continue
            result[tier] = quality - params.lambda_c * cost / params.cost_ref_usd
        else:
            result[tier] = quality
    return result


def choose_tier(tier, retrieved, params):
    # type: (ComplexityLevel, Sequence[Any], BanditParams) -> Tuple[ComplexityLevel, str, int]
    """Keep the classifier's tier unless the neighbours argue strongly for another.

    Returns ``(tier, code, neighbours)``. ``code`` is one of the ``BANDIT_*``
    constants except ``BANDIT_OFF``, which is the caller's to report when it
    never calls this.
    """
    neighbours, evidence = aggregate(retrieved)
    if neighbours < params.min_neighbors:
        return tier, BANDIT_COLD, neighbours

    scores = utilities(evidence, params)
    incumbent = scores.get(tier)
    if incumbent is None:
        # No evidence about what the classifier chose: nothing to compare against.
        return tier, BANDIT_IGNORE, neighbours

    best = tier
    best_score = incumbent
    # Ascending tier order breaks ties toward the cheaper tier deterministically.
    for candidate in sorted(scores):
        if candidate == tier:
            continue
        if scores[candidate] > best_score:
            best, best_score = candidate, scores[candidate]

    if best == tier:
        return tier, BANDIT_SAME, neighbours
    if best_score - incumbent <= params.margin:
        return tier, BANDIT_IGNORE, neighbours
    return best, BANDIT_OVERRIDE, neighbours
