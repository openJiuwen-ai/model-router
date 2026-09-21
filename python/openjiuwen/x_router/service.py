"""The bandit loop, per request: hint on the way in, scored outcome on the way back.

Three levels, for three kinds of host.

The helpers — :func:`build_hint`, :func:`build_bandit_feedback`,
:func:`parse_reasoning` — are the pieces. They fix the two formats the
algorithm and the store must agree on with the host: the text that is
retrieved against (the classifier's own preview, capped at the kernel's byte
limit) and the extension that carries a scored outcome. A host with its own
quality signal uses these alone.

:class:`Scorer` is the synchronous settle step — judge the served turn and file
the second report — as a single call. A host that has its own task queue keeps
scheduling in its hands and only drops the boilerplate.

:class:`XRouterService` is the whole loop. The host calls ``route`` and
``report``, passing along what it already has (the messages, the response,
the cost); hints, judging, the quality report, the store and the override are
internal. Settling runs on a small pool of worker threads after ``report``
returns, so a slow judge never sits on the request path.

What this does not change: the host still supplies the response and the cost,
because nothing else has them; and the kernel is untouched — this module lives
where x-router already absorbs kernel gaps (see facade.py), and shrinks to
a shell if the runtime ever grows a settle stage of its own.

Because settling is asynchronous, its failures cannot reach the ``report``
caller. They are counted in :attr:`XRouterService.stats`, logged, and handed to
``on_settle_error`` if the host gave one. A host that wants exceptions uses
:meth:`XRouterService.settle` or :class:`Scorer` directly.

Privacy: the transcript and the response go to the judge. With
``[x-router.judge_model] kind = "api"`` that means they leave the machine.
"""

from __future__ import annotations

import logging
import re
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .bandit import OBSERVATIONS_KEY
from .bandit_store import EXTENSION_SCHEMA, EXTENSION_VERSION
from .complexity import conversation_preview
from .facade import (
    SECTION,
    build_judge,
    build_params,
    build_request,
    build_router,
    build_store,
    build_store_params,
    load_profile,
)
from .judge import JudgeError, LocalJudgeBackend, build_judge_request, parse_judge_score
from .types import ComplexityLevel, XRouterParams, field, finite

__all__ = [
    "QUERY_MAX_TEXT_BYTES",
    "SERVED",
    "Scorer",
    "XRouterService",
    "build_bandit_feedback",
    "build_hint",
    "build_service",
    "parse_reasoning",
    "retrieval_text",
]

logger = logging.getLogger(__name__)

# The kernel's limit on StateQuery.text (crates/protocol/src/state_query.rs).
# Over it, the runtime silently falls back to snapshot() and the bandit stays
# cold — so the hint builder must stay under it rather than trust the caller.
QUERY_MAX_TEXT_BYTES = 16_384

# In build_bandit_feedback's observations, the key that means "the tier this
# decision actually served", resolved from the selection's reasoning line.
SERVED = "served"

_REASONING_PAIR = re.compile(r"(\w+)=(\S+)")

# Settles waiting or running before report() starts dropping them. Losing a
# score is harmless — the store is a hint — while an unbounded queue behind a
# stalled judge is not.
DEFAULT_QUEUE_SIZE = 256


# --------------------------------------------------------------------------
# The pieces: hint in, scored outcome back
# --------------------------------------------------------------------------


def retrieval_text(messages, max_chars):
    # type: (Sequence[Any], int) -> str
    """The text the store indexes: the classifier's own preview, capped in bytes.

    Same view as the classifier sees (see conversation_preview), then cut to
    the kernel's byte limit on a character boundary.
    """
    text = conversation_preview(messages, max_chars)
    encoded = text.encode("utf-8")
    if len(encoded) <= QUERY_MAX_TEXT_BYTES:
        return text
    return encoded[:QUERY_MAX_TEXT_BYTES].decode("utf-8", errors="ignore")


def build_hint(messages, params, top_k=None):
    # type: (Sequence[Any], XRouterParams, Optional[int]) -> Optional[Dict[str, Any]]
    """A ``RouteHint`` (dict form) asking the store for this conversation's neighbours.

    Pass it as the second argument of ``route_sync``. Returns ``None`` when the
    conversation yields no text, since an empty query would only be degraded by
    the runtime anyway. ``top_k`` defaults to the store's own setting.
    """
    text = retrieval_text(messages, params.classifier_preview_chars)
    if not text.strip():
        return None
    query = {"text": text}  # type: Dict[str, Any]
    if top_k is not None:
        query["top_k"] = int(top_k)
    return {"state_query": query}


def parse_reasoning(reasoning):
    # type: (str) -> Dict[str, str]
    """The ``key=value`` pairs of an x-router reasoning line, as a dict."""
    if not isinstance(reasoning, str) or not reasoning.startswith("x-router:"):
        raise ValueError("not an x-router reasoning line: {0!r}".format(reasoning))
    return dict(_REASONING_PAIR.findall(reasoning))


def _observation(value):
    # type: (Any) -> Tuple[float, Optional[float]]
    if isinstance(value, Mapping):
        quality, cost = value.get("quality"), value.get("cost_usd")
    elif isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError("an observation tuple is (quality, cost_usd)")
        quality, cost = value
    else:
        quality, cost = value, None
    number = finite(quality)
    if number is None:
        raise ValueError("quality must be a finite number, got {0!r}".format(quality))
    if not -1.0 <= number <= 1.0:
        raise ValueError("quality must be within [-1, 1], got {0!r}".format(quality))
    if cost is not None:
        cost_number = finite(cost)
        if cost_number is None or cost_number < 0:
            raise ValueError("cost_usd must be a non-negative finite number or None, got {0!r}".format(cost))
        cost = cost_number
    return number, cost


def build_bandit_feedback(selection, observations, session_id="", agent_id=""):
    # type: (Any, Mapping[str, Any], str, str) -> Dict[str, Any]
    """The second report: what the served turn was worth.

    ``observations`` maps a tier name — or :data:`SERVED`, meaning the tier this
    selection routed to — to ``(quality, cost_usd)``, a ``{"quality", "cost_usd"}``
    mapping, or a bare quality. Quality is a signed score in ``[-1, 1]``;
    ``cost_usd`` is what the call cost, ``0.0`` for a free tier (omit it only
    when genuinely unknown: the bandit drops tiers with unknown cost while cost
    carries weight). Several tiers at once is how shadow scoring is reported.

    The result is a feedback dict for ``report_sync`` with ``call = None`` — a
    delayed evaluation in the kernel's terms — carrying the ``x-router.bandit``
    extension. The store matches it to the query by ``route_id``, so pass the
    selection ``route_sync`` returned, not a copy without one.
    """
    selected = field(selection, "selected_model_id")
    route_id = field(selection, "route_id")
    if not route_id:
        raise ValueError("selection has no route_id; the store cannot match it to a query")
    if not observations:
        raise ValueError("observations must name at least one tier")

    served = None  # type: Optional[str]
    data = {}  # type: Dict[str, Dict[str, Optional[float]]]
    for name, value in observations.items():
        if name == SERVED:
            if served is None:
                served = parse_reasoning(field(selection, "reasoning")).get("tier")
                if not served:
                    raise ValueError("selection reasoning names no tier; cannot resolve 'served'")
            tier = ComplexityLevel.parse(served)
        else:
            tier = ComplexityLevel.parse(name)
        if tier.name in data:
            raise ValueError("tier {0} observed twice".format(tier.name))
        quality, cost = _observation(value)
        data[tier.name] = {"quality": quality, "cost_usd": cost}

    return {
        "session_id": session_id,
        "agent_id": agent_id,
        "selected_model_id": selected,
        "route_id": route_id,
        "call": None,
        "extensions": [
            {"schema": EXTENSION_SCHEMA, "version": EXTENSION_VERSION, "data": {OBSERVATIONS_KEY: data}}
        ],
    }


# --------------------------------------------------------------------------
# The loop: synchronous, then on worker threads
# --------------------------------------------------------------------------


class Scorer(object):
    """Judge one served turn and file the bandit's second report. Synchronous.

    Raises what the judge raises (``JudgeError``, or ``ValueError`` from the
    parser), so the caller decides what a failed score means.
    """

    def __init__(self, router, judge):
        # type: (Any, Any) -> None
        if not hasattr(judge, "score"):
            raise TypeError("judge must implement score(request) -> str")
        self.router = router
        self.judge = judge

    def settle(
        self,
        selection,
        messages,
        response_text,
        tool_calls=(),
        tools=(),
        cost_usd=None,
        session_id="",
        agent_id="",
    ):
        # type: (Any, Sequence[Any], Optional[str], Sequence[Any], Sequence[Any], Optional[float], str, str) -> float
        """Score the turn, report it, return the quality.

        ``cost_usd`` should be ``0.0`` for a free tier; ``None`` means unknown,
        and the bandit ignores tiers with unknown cost while cost carries weight.
        """
        request = build_judge_request(messages, response_text, tool_calls, tools)
        quality = parse_judge_score(self.judge.score(request))
        feedback = build_bandit_feedback(
            selection, {SERVED: (quality, cost_usd)}, session_id=session_id, agent_id=agent_id
        )
        self.router.report_sync(feedback)
        return quality


class XRouterService(object):
    """Route and report; the bandit loop closes inside.

    Build one with :func:`build_service`, or assemble it from parts. ``judge``
    may be ``None``, in which case ``report`` files call feedback only and the
    service is simply a tidier front for x-router.
    """

    def __init__(
        self,
        router,
        params,
        judge=None,
        store=None,
        workers=None,
        queue_size=DEFAULT_QUEUE_SIZE,
        on_settle_error=None,
        executor=None,
    ):
        # type: (Any, XRouterParams, Any, Any, Optional[int], int, Optional[Callable[[BaseException, Any], None]], Any) -> None
        if not isinstance(params, XRouterParams):
            raise TypeError("params must be an XRouterParams")
        self.router = router
        self.params = params
        self.store = store
        self.judge = judge
        self._scorer = Scorer(router, judge) if judge is not None else None
        # Retrieval only pays when something answers it: our own store, or a
        # bandit that expects one behind whatever state the host injected.
        self._retrieve = store is not None or params.bandit is not None
        self._on_error = on_settle_error
        self._queue_size = int(queue_size)
        if workers is None:
            # A local judge shares the accelerator with the classifier; one
            # worker keeps scoring from crowding the request path.
            workers = 1 if isinstance(judge, LocalJudgeBackend) else 4
        self._executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=int(workers), thread_name_prefix="x-router-settle"
        )
        self._lock = threading.Lock()
        self._inflight = 0
        self._counts = {"submitted": 0, "settled": 0, "judge_failed": 0, "dropped_full": 0}
        self._closed = False

    # -- the two calls -------------------------------------------------------

    def route(self, messages, session_id="", agent_id="", exclusions=()):
        # type: (Sequence[Any], str, str, Sequence[str]) -> Any
        """Route a conversation. Builds the request and, when useful, the retrieval hint."""
        request = build_request(messages, session_id=session_id, agent_id=agent_id, exclusions=exclusions)
        hint = build_hint(messages, self.params) if self._retrieve else None
        return self.router.route_sync(request, hint)

    def report(
        self,
        selection,
        outcome="ok",
        latency_ms=None,
        cache_valid=None,
        messages=None,
        response_text=None,
        tool_calls=(),
        tools=(),
        cost_usd=None,
        session_id="",
        agent_id="",
    ):
        # type: (Any, str, Optional[int], Optional[bool], Optional[Sequence[Any]], Optional[str], Sequence[Any], Sequence[Any], Optional[float], str, str) -> bool
        """Report a finished call.

        Files the call feedback now. When a judge is configured, the call
        succeeded, and ``messages`` and ``response_text`` were given, also
        schedules the turn for scoring. Returns whether scoring was scheduled.
        """
        call = {"outcome": outcome}  # type: Dict[str, Any]
        if latency_ms is not None:
            call["latency_ms"] = int(latency_ms)
        if cache_valid is not None:
            call["cache_valid"] = bool(cache_valid)
        self.router.report_sync(
            {
                "session_id": session_id,
                "agent_id": agent_id,
                "selected_model_id": field(selection, "selected_model_id"),
                "route_id": field(selection, "route_id"),
                "call": call,
            }
        )
        if self._scorer is None or outcome != "ok" or messages is None or response_text is None:
            return False
        return self._submit(
            selection, list(messages), response_text, tuple(tool_calls or ()), tuple(tools or ()),
            cost_usd, session_id, agent_id,
        )

    def settle(self, selection, messages, response_text, **kwargs):
        # type: (Any, Sequence[Any], Optional[str], Any) -> float
        """Score and report synchronously, raising on failure. See :class:`Scorer`."""
        if self._scorer is None:
            raise RuntimeError("no judge configured; nothing can score this turn")
        return self._scorer.settle(selection, messages, response_text, **kwargs)

    # -- background settling -------------------------------------------------

    def _submit(self, *args):
        # type: (Any) -> bool
        with self._lock:
            if self._closed or self._inflight >= self._queue_size:
                self._counts["dropped_full"] += 1
                if self._counts["dropped_full"] in (1, 10, 100, 1000):
                    logger.warning(
                        "x-router settle queue full (%d in flight); dropped %d score(s) so far",
                        self._inflight, self._counts["dropped_full"],
                    )
                return False
            self._inflight += 1
            self._counts["submitted"] += 1
        try:
            self._executor.submit(self._run_settle, *args)
        except RuntimeError:  # executor shut down under us
            with self._lock:
                self._inflight -= 1
                self._counts["dropped_full"] += 1
            return False
        return True

    def _run_settle(self, selection, messages, response_text, tool_calls, tools, cost_usd, session_id, agent_id):
        # type: (Any, Any, Any, Any, Any, Any, Any, Any) -> None
        try:
            self._scorer.settle(  # type: ignore[union-attr]
                selection, messages, response_text, tool_calls=tool_calls, tools=tools,
                cost_usd=cost_usd, session_id=session_id, agent_id=agent_id,
            )
            with self._lock:
                self._counts["settled"] += 1
        except Exception as exc:  # noqa: BLE001 — a worker must never die silently
            with self._lock:
                self._counts["judge_failed"] += 1
                failures = self._counts["judge_failed"]
            if failures in (1, 10, 100, 1000) or not isinstance(exc, (JudgeError, ValueError)):
                logger.warning("x-router settle failed (%d so far): %s", failures, exc)
            if self._on_error is not None:
                try:
                    self._on_error(exc, selection)
                except Exception:  # noqa: BLE001
                    logger.exception("x-router on_settle_error raised")
        finally:
            with self._lock:
                self._inflight -= 1

    # -- introspection and lifecycle ----------------------------------------

    @property
    def stats(self):
        # type: () -> Dict[str, Any]
        """Settle funnel plus the store's. ``judge_failed`` rising is the signal ``report`` cannot give."""
        with self._lock:
            counts = dict(self._counts)
            counts["pending"] = self._inflight
        counts["store"] = getattr(self.store, "stats", None) if self.store is not None else None
        return counts

    def close(self, timeout=None):
        # type: (Optional[float]) -> None
        """Stop accepting scores and wait for the ones in flight."""
        with self._lock:
            self._closed = True
        try:
            self._executor.shutdown(wait=timeout is None or timeout > 0)
        except TypeError:  # an injected executor without the parameter
            self._executor.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def build_service(config, backend=None, judge=None, state=None, **service_kwargs):
    # type: (Any, Any, Any, Any, Any) -> XRouterService
    """Assemble the whole loop from one profile.

    Reads ``[x-router]`` for the router, ``[x-router.bandit.store]`` for the
    store and ``[x-router.judge_model]`` for the judge; any of the three may be
    injected instead. A local judge with ``share_classifier = true`` reuses the
    classifier this call built. Extra keyword arguments go to
    :class:`XRouterService` (``workers``, ``queue_size``, ``on_settle_error``).
    """
    profile = load_profile(config)
    params = build_params(profile, backend=backend)

    store = None
    if state is None and build_store_params(profile) is not None:
        store = build_store(profile)
        state = store

    router = build_router(profile, backend=params.backend, state=state)

    section = profile.get(SECTION) or {}
    if judge is None and section.get("judge_model") is not None:
        judge = build_judge(profile, classifier=params.backend)
    if judge is None and params.bandit is not None:
        warnings.warn(
            "[{0}.bandit] is enabled but no judge is configured; the service will file call "
            "feedback only and the store will never warm up".format(SECTION),
            stacklevel=2,
        )

    return XRouterService(router, params, judge=judge, store=store, **service_kwargs)
