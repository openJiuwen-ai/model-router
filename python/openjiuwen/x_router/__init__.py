"""x-router — complexity-based model routing.

Importing this package registers the ``x-router`` algorithm. ``discover.py``
imports it automatically as part of ``import openjiuwen``, so hosts normally do
not import it directly; they call :func:`build_router` to attach configuration.

Design and rationale: see README.md in this directory.
"""

from __future__ import annotations

from .classifier import LocalBackend, backend_from_config
from .algorithm import (
    RULE_ESCALATE_CLOUD,
    RULE_TARGET_EXCLUDED_DEGRADE,
    RULE_WITHIN_LOCAL_CAPABILITY,
    XRouter,
    decide_by_tier,
    specialize,
)
from .complexity import (
    PROMPT_TEMPLATE,
    SOURCE_HEURISTIC,
    SOURCE_HEURISTIC_FALLBACK,
    SOURCE_LLM,
    SOURCE_PARSE_FAILED,
    SOURCE_UNAVAILABLE,
    build_classifier_request,
    classify,
    classify_heuristic,
    content_text,
    conversation_preview,
    parse_complexity,
)
from .facade import (
    build_params,
    build_request,
    build_router,
    normalize_messages,
)
from .types import (
    ClassifierRequest,
    ComplexityBackend,
    ComplexityLevel,
    ParamsError,
    XRouterParams,
)

__all__ = [
    "ClassifierRequest",
    "LocalBackend",
    "ComplexityBackend",
    "ComplexityLevel",
    "ParamsError",
    "PROMPT_TEMPLATE",
    "RULE_ESCALATE_CLOUD",
    "RULE_TARGET_EXCLUDED_DEGRADE",
    "RULE_WITHIN_LOCAL_CAPABILITY",
    "SOURCE_HEURISTIC",
    "SOURCE_HEURISTIC_FALLBACK",
    "SOURCE_LLM",
    "SOURCE_PARSE_FAILED",
    "SOURCE_UNAVAILABLE",
    "XRouter",
    "XRouterParams",
    "backend_from_config",
    "build_classifier_request",
    "build_params",
    "build_request",
    "build_router",
    "classify",
    "classify_heuristic",
    "content_text",
    "conversation_preview",
    "decide_by_tier",
    "normalize_messages",
    "parse_complexity",
    "specialize",
]
