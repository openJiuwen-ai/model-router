"""BanditStore: the state behind x-router's bandit.

A ``StateProvider`` that remembers, for conversations it has seen, how each tier
turned out — and answers a ``StateQuery`` with the outcomes of the most similar
ones. The algorithm's bandit half (``bandit.py``) reads those as
``RouteContext.retrieved``.

How a record comes to exist:

    query(key, StateQuery{text, route_id})
        vector = encode(text)                        the only moment the text is seen
        pending[route_id] = vector                half a record, not retrievable
        return the nearest closed records
    ... the host serves the request and scores it, seconds later ...
    report(Feedback{route_id, extensions=[x-router.bandit {observations}]})
        closed.append(pending.pop(route_id), observations)

``route_id`` is the runtime's: it is injected into the query before the state
sees it and comes back on the feedback, so the store never needs an id of its
own. Pending records that are never closed expire after ``pending_ttl_secs``.

The store also does what the kernel's ``MemoryState`` does with ordinary call
feedback — exclusions on overflow/unavailable, affinity on success, a sample
count — because the state slot holds one provider, and losing exclusions would
disable x-router's ``target_excluded_degrade`` rule.

Privacy: only hashed vectors and numbers are kept. The text is encoded inside
``query`` and dropped. Note that the text does cross the state boundary — that
is ``StateQuery.text``'s nature, not this store's — so a remote implementation
of the same contract would see it.

numpy is imported lazily so that ``import openjiuwen`` stays light.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..state_provider import StateProvider
from .bandit import OBSERVATIONS_KEY, parse_observations
from .types import BanditStoreParams, field

__all__ = [
    "EXTENSION_SCHEMA",
    "EXTENSION_VERSION",
    "POLICY_SLOT",
    "STORE_BACKEND",
    "BanditStore",
    "HashedNgramRetriever",
]

# The state backend name a profile declares to use this store.
STORE_BACKEND = "x-router-bandit"
# The Feedback extension that closes a record.
EXTENSION_SCHEMA = "x-router.bandit"
EXTENSION_VERSION = "1"
# publish() slot whose version stamps new records and ages old ones.
POLICY_SLOT = "x-router.policy"

_WHITESPACE = re.compile(r"\s+")


def _fnv1a(text):
    # type: (str) -> int
    """Deterministic 32-bit hash; Python's hash() is salted per process."""
    h = 0x811C9DC5
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class HashedNgramRetriever:
    """Character 3-gram hashing vectorizer: no vocabulary, no model, <1 ms.

    Same construction as EdgeTRL's and agent-xrouter's retrievers (FNV-1a over
    lowercased, whitespace-collapsed 3-grams into ``dim`` buckets, L2-normalised),
    so vectors — and therefore neighbours — match across the three.
    """

    def __init__(self, dim=4096, ngram=3):
        # type: (int, int) -> None
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError("retriever dim must be a positive int")
        self.dim = dim
        self._n = ngram

    def encode(self, text):
        # type: (str) -> Any
        import numpy as np

        vector = np.zeros(self.dim, dtype=np.float32)
        normalized = _WHITESPACE.sub(" ", str(text).lower().strip())
        if not normalized:
            return vector
        n = self._n
        if len(normalized) < n:
            grams = [normalized]
        else:
            grams = (normalized[i : i + n] for i in range(len(normalized) - n + 1))
        for gram in grams:
            vector[_fnv1a(gram) % self.dim] += 1.0
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return vector


@dataclass
class _Record:
    observations: Dict[str, Dict[str, Optional[float]]]  # tier name -> {quality, cost_usd}
    version: int
    created_at: float


@dataclass
class _KeyEntry:
    """Per-RoutingKey call-feedback state, mirroring the kernel's MemoryState."""

    affinity: Optional[str] = None
    exclusions: Optional[List[str]] = None
    sample_count: int = 0
    touched_at: float = 0.0


def _slot(key):
    # type: (Any) -> Tuple[str, str]
    """A RoutingKey (typed or dict) as the tuple the per-key table is indexed by."""
    return (str(field(key, "session_id", "") or ""), str(field(key, "agent_id", "") or ""))


def _empty_view():
    # type: () -> Dict[str, Any]
    return {"affinity": None, "exclusions": [], "stats": {"sample_count": 0}}


class BanditStore(StateProvider):
    """kNN outcome memory with two-phase writes. See the module docstring."""

    name = STORE_BACKEND

    def __init__(self, params, retriever=None, clock=time.time):
        # type: (BanditStoreParams, Any, Callable[[], float]) -> None
        if not isinstance(params, BanditStoreParams):
            raise TypeError("params must be a BanditStoreParams")
        self.params = params
        self._clock = clock
        self._retriever = retriever or HashedNgramRetriever(params.retriever_dim)
        self._dim = int(self._retriever.dim)
        self._lock = threading.Lock()

        self._version = 0
        # route_id -> (vector, opened_at, policy version the decision was made under)
        self._pending = OrderedDict()  # type: OrderedDict[str, Tuple[Any, float, int]]
        self._records = []  # type: List[_Record]
        self._matrix = None  # type: Any  # (capacity, dim) float32; rows [0:_count) live
        self._count = 0
        self._dropped = 0
        self._unknown = 0
        self._sweep_every = max(1.0, min(60.0, params.pending_ttl_secs / 10.0))
        self._last_sweep = 0.0
        self._keys = {}  # type: Dict[Tuple[str, str], _KeyEntry]

    # -- introspection -------------------------------------------------------

    @property
    def version(self):
        # type: () -> int
        return self._version

    @property
    def stats(self):
        # type: () -> Dict[str, int]
        """The write funnel. A store that stays small shows up as ``dropped`` or ``unknown``."""
        with self._lock:
            return {
                "pending": len(self._pending),
                "closed": self._count,
                "dropped": self._dropped,
                "unknown": self._unknown,
                "version": self._version,
            }

    def encode(self, text):
        # type: (str) -> Any
        return self._retriever.encode(text)

    # -- StateProvider: read side --------------------------------------------

    def snapshot(self, key):
        # type: (Any) -> Dict[str, Any]
        with self._lock:
            return self._view_locked(key)

    def query(self, key, query):
        # type: (Any, Any) -> Dict[str, Any]
        """Nearest closed records, plus the ordinary view.

        Opens a pending record for ``query.route_id`` when the query carries
        usable text (or a vector of the right dimension). Any failure degrades
        to a view-only answer: a broken memory must not change routing.
        """
        try:
            vector = self._vector_for(query)
        except Exception:
            vector = None
        with self._lock:
            view = self._view_locked(key)
            if vector is None:
                return {"view": view, "retrieved": []}
            now = self._clock()
            route_id = field(query, "route_id")
            if isinstance(route_id, str) and route_id:
                self._pending[route_id] = (vector, now, self._version)
                self._sweep_locked(now)
            try:
                retrieved = self._neighbours_locked(vector, field(query, "top_k"))
            except Exception:
                retrieved = []
        return {"view": view, "retrieved": retrieved}

    def _vector_for(self, query):
        # type: (Any) -> Any
        import numpy as np

        raw = field(query, "vector")
        if raw is not None and len(raw) == self._dim:
            vector = np.asarray(raw, dtype=np.float32)
        else:
            text = field(query, "text")
            if not isinstance(text, str) or not text.strip():
                return None
            vector = self._retriever.encode(text)
        if vector.shape != (self._dim,) or float(np.linalg.norm(vector)) == 0.0:
            return None
        return vector

    def _neighbours_locked(self, vector, top_k):
        # type: (Any, Any) -> List[Dict[str, Any]]
        import numpy as np

        if self._count == 0:
            return []
        k = self.params.top_k
        if isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0:
            k = min(top_k, k)
        k = min(k, self._count)
        live = self._matrix[: self._count]
        similarities = live @ vector  # rows are L2-normalised: dot == cosine
        if k < self._count:
            indices = np.argpartition(-similarities, k - 1)[:k]
        else:
            indices = np.arange(self._count)
        gamma = self.params.forgetting_gamma
        items = []  # type: List[Tuple[float, int]]
        for index in indices:
            similarity = float(similarities[index])
            if similarity < self.params.min_similarity:
                continue
            record = self._records[int(index)]
            weight = similarity * (gamma ** max(0, self._version - record.version))
            if not math.isfinite(weight) or weight <= 0.0:
                continue
            items.append((weight, int(index)))
        items.sort(key=lambda pair: -pair[0])
        return [
            {
                "id": "r{0}".format(index),
                "score": weight,
                "data": {OBSERVATIONS_KEY: self._records[index].observations},
            }
            for weight, index in items
        ]

    # -- StateProvider: write side -------------------------------------------

    def report(self, feedback):
        # type: (Any) -> None
        """Call feedback updates the view; the ``x-router.bandit`` extension closes a record."""
        now = self._clock()
        with self._lock:
            call = field(feedback, "call")
            if call is not None:
                self._absorb_call_locked(feedback, call, now)
            observations = self._extension_observations(feedback)
            if observations is None:
                return
            route_id = field(feedback, "route_id")
            pending = self._pending.pop(route_id, None) if route_id else None
            if pending is None:
                self._unknown += 1
                return
            # Stamp the version the decision was routed under, not the one at
            # scoring time: a publish() between the two must age this record.
            vector, _, version = pending
            self._append_locked(vector, _Record(observations, version, now))

    def _absorb_call_locked(self, feedback, call, now):
        # type: (Any, Any, float) -> None
        slot = _slot(field(feedback, "key"))
        entry = self._keys.get(slot)
        if entry is None or now - entry.touched_at > self.params.exclusion_ttl_secs:
            entry = _KeyEntry(exclusions=[])
            self._keys[slot] = entry
        entry.touched_at = now
        entry.sample_count += 1
        outcome = str(field(call, "outcome", "") or "").lower()
        model = str(field(feedback, "selected_model_id", "") or "")
        if outcome == "ok":
            entry.affinity = model
        elif outcome in ("overflow", "unavailable"):
            if model and model not in entry.exclusions:
                entry.exclusions.append(model)
        # rejected: counted, nothing else. Same as MemoryState.

    def _extension_observations(self, feedback):
        # type: (Any) -> Optional[Dict[str, Dict[str, Optional[float]]]]
        for extension in field(feedback, "extensions", None) or ():
            if field(extension, "schema") != EXTENSION_SCHEMA:
                continue
            parsed = parse_observations(field(extension, "data"))
            if not parsed:
                return None
            return {
                tier.name: {"quality": quality, "cost_usd": cost}
                for tier, (quality, cost) in parsed.items()
            }
        return None

    def _append_locked(self, vector, record):
        # type: (Any, _Record) -> None
        import numpy as np

        if self._matrix is None:
            self._matrix = np.zeros((64, self._dim), dtype=np.float32)
        if self._count >= self._matrix.shape[0]:
            self._matrix = np.vstack([self._matrix, np.zeros_like(self._matrix)])
        self._matrix[self._count] = vector
        self._records.append(record)
        self._count += 1
        # FIFO at capacity. Rare, so the O(n) shift is acceptable.
        while self._count > self.params.max_entries:
            self._records.pop(0)
            self._matrix[: self._count - 1] = self._matrix[1 : self._count]
            self._count -= 1

    def _sweep_locked(self, now):
        # type: (float) -> None
        if now - self._last_sweep < self._sweep_every and len(self._pending) <= self.params.max_entries:
            return
        self._last_sweep = now
        ttl = self.params.pending_ttl_secs
        stale = [did for did, (_, opened, _) in self._pending.items() if now - opened > ttl]
        for did in stale:
            del self._pending[did]
        self._dropped += len(stale)

    def expire_pending(self):
        # type: () -> int
        """Drop pending records past the TTL now, bypassing the sweep throttle."""
        with self._lock:
            before = self._dropped
            self._last_sweep = 0.0
            self._sweep_locked(self._clock())
            return self._dropped - before

    # -- StateProvider: version ----------------------------------------------

    def publish(self, slot, artifact=b"", ver=0):
        # type: (str, Any, int) -> None
        """Advance the policy version. Records are never cleared; only their weight ages."""
        if slot != POLICY_SLOT:
            return
        with self._lock:
            self._version = int(ver)

    # -- helpers ---------------------------------------------------------------

    def _view_locked(self, key):
        # type: (Any) -> Dict[str, Any]
        slot = _slot(key)
        entry = self._keys.get(slot)
        if entry is None:
            return _empty_view()
        if self._clock() - entry.touched_at > self.params.exclusion_ttl_secs:
            del self._keys[slot]
            return _empty_view()
        return {
            "affinity": entry.affinity,
            "exclusions": list(entry.exclusions or []),
            "stats": {"sample_count": entry.sample_count},
        }
