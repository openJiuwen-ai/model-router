"""Complexity classification: prompt, preview, strict parsing, heuristic fallback.

The prompt template, the preview rules and the heuristic thresholds are frozen.
The classifier is a stock small model, so these are not a contract with a
checkpoint — they are what determines its behaviour, and changing any of them
makes benchmark runs incomparable. See README.md, "Classifier" and "Tests".
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Sequence, Tuple

from .types import ClassifierRequest, ComplexityLevel, XRouterParams

__all__ = [
    "SOURCE_LLM",
    "SOURCE_HEURISTIC",
    "SOURCE_HEURISTIC_FALLBACK",
    "SOURCE_PARSE_FAILED",
    "SOURCE_UNAVAILABLE",
    "build_classifier_request",
    "content_text",
    "classify",
    "classify_heuristic",
    "conversation_preview",
    "parse_complexity",
    "PROMPT_TEMPLATE",
]

# Where a tier came from. Recorded in Decision.reasoning so classifier health is
# observable from routing telemetry alone.
SOURCE_LLM = "llm"                                # classifier answered and parsed
SOURCE_HEURISTIC = "heuristic"                    # no backend configured
SOURCE_HEURISTIC_FALLBACK = "heuristic_fallback"  # backend unreachable or raised
SOURCE_PARSE_FAILED = "parse_failed"              # backend answered, output rejected
SOURCE_UNAVAILABLE = "unavailable"                # everything failed; MEDIUM default

# Conversation window handed to the classifier.
WINDOW = 6

PROMPT_TEMPLATE = """Classify the work needed in the next assistant turn as SIMPLE, MEDIUM, COMPLEX, RESEARCH, or REASONING.
Use the overall user goal and recent assistant/tool progress to identify what remains. Ignore system prompts, tool definitions, and completed work.

Levels:
SIMPLE: A direct, single-step response using supplied context, with no substantive transformation or execution.
MEDIUM: Bounded drafting, editing, summarization, calculation, coding, data transformation, or routine tool/file work with clear steps.
COMPLEX: Substantial planning, implementation, debugging, design, or analysis requiring broad context or multiple interdependent steps or artifacts.
RESEARCH: Investigative work requiring gathering, evaluation, comparison, and synthesis across multiple sources.
REASONING: A hard problem where rigorous multi-hop inference, formal proof, derivation, or verification is the central work.

Locality: SIMPLE and MEDIUM are local-only; COMPLEX, RESEARCH, and REASONING are cloud tiers. If the next step requires internet access, external sources, or a remote API, choose among the three cloud tiers by the definitions above; external access alone does not distinguish among them. Looking up current or live data, or using a remote service through a tool or CLI, requires a cloud tier. Writing code or instructions that may use an API later does not itself require cloud access.
Classify the remaining step, not the entire original task. The mere availability of tools must not affect the level.

Conversation and recent progress:
{content}

Reply with ONLY the tier name."""

# Strict: the whole output must be one label. Prose or several labels is drift,
# not a parse problem to paper over.
_LEVEL_RE = re.compile(r"\s*(SIMPLE|MEDIUM|COMPLEX|RESEARCH|REASONING)\s*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")

_REASONING_KEYWORDS = frozenset({
    "prove", "proof", "theorem", "lemma", "corollary", "axiom",
    "formal verification", "formal proof", "mathematical proof",
    "by induction", "induction hypothesis", "qed",
    "contrapositive", "bijection", "isomorphism",
    "differential equation", "eigenvalue", "tensor calculus",
    "fourier transform", "laplace transform",
    "np-complete", "np-hard", "sat solver", "model checking",
})
_RESEARCH_KEYWORDS = frozenset({
    "quantum entanglement", "quantum cryptography", "quantum computing",
    "bell's inequality", "epr paradox", "wave function collapse",
    "qubit", "decoherence",
    "genomics", "proteomics", "pharmacokinetics", "pharmacodynamics",
    "pathophysiology", "epidemiology", "clinical trial", "meta-analysis",
    "neurological", "etiology", "comorbidity",
    "jurisprudence", "constitutional law", "case precedent",
    "litigation", "habeas corpus", "estoppel",
    "derivative pricing", "black-scholes", "monte carlo simulation",
    "stochastic process", "martingale",
    "transformer architecture", "attention mechanism", "rlhf",
    "systematic review", "literature review", "survey paper",
})
_COMPLEX_KEYWORDS = frozenset({
    "analyze all", "analyze multiple", "across all files",
    "end-to-end", "architecture design", "root cause analysis",
    "multi-step plan", "step by step plan", "step by step", "step-by-step",
    "compare and contrast", "trade-off analysis",
    "long document", "entire codebase",
})
_MEDIUM_KEYWORDS = frozenset({
    "write a script", "write a function", "generate code", "implement",
    "refactor", "optimize", "rewrite", "debug", "fix this bug",
    "how does", "how do i",
    "summarize", "translate", "convert", "parse", "extract",
})


# --------------------------------------------------------------------------
# One text view, shared by the preview and the heuristic
# --------------------------------------------------------------------------


def _field(message, name):
    # type: (Any, str) -> Any
    """Read a field from a protocol Message object or a plain dict."""
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def content_text(content):
    # type: (Any) -> str
    """Flatten message content to text.

    Content may be a string or a list of content parts. The kernel's protocol
    carries only strings, so requests are flattened before they reach it — but
    this also runs on messages handed straight to the classifier, where the list
    shape that OpenAI-compatible clients emit still arrives intact.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []  # type: List[str]
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return " ".join(parts)
    return ""


def _is_scaffolding(message):
    # type: (Any) -> bool
    """Runtime scaffolding the classifier must not see.

    System prompts, and reminder blocks injected into user turns by agent
    harnesses. Both describe the harness, not the work being asked for.
    """
    role = str(_field(message, "role") or "").lower()
    if role == "system":
        return True
    text = content_text(_field(message, "content")).lstrip()
    return text.startswith("<system-reminder>") and "<prompt-attachment" in text


def _tool_call_names(tool_calls):
    # type: (Any) -> Tuple[str, ...]
    names = []  # type: List[str]
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            name = function.get("name") if isinstance(function, dict) else tool_call.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return tuple(names)


def conversation_messages(messages):
    # type: (Sequence[Any]) -> List[Tuple[str, str, Tuple[str, ...]]]
    """Filtered conversation view: (role, text, tool_call_names) per message.

    The preview and the heuristic both start here, so the two classification
    paths never disagree about which text they are looking at.
    """
    view = []  # type: List[Tuple[str, str, Tuple[str, ...]]]
    for message in messages or ():
        if _is_scaffolding(message):
            continue
        role = str(_field(message, "role") or "user").lower()
        text = content_text(_field(message, "content")).strip()
        names = _tool_call_names(_field(message, "tool_calls"))
        if text or names:
            view.append((role, text, names))
    return view


def _bounded_text(text, max_chars):
    # type: (str, int) -> str
    """Truncate the middle, keeping the opening and the most recent text."""
    if len(text) <= max_chars:
        return text
    marker = "\n...[truncated]...\n"
    available = max_chars - len(marker)
    head = available // 2
    return "{0}{1}{2}".format(text[:head], marker, text[-(available - head):])


def conversation_preview(messages, max_chars):
    # type: (Sequence[Any], int) -> str
    """Bounded conversation snippet for the classifier prompt."""
    view = conversation_messages(messages)
    recent = view[-WINDOW:]
    # An agent loop's tail is often all assistant/tool traffic; without this the
    # task itself falls out of the window and the classifier sees no request.
    latest_user = None
    for entry in reversed(view):
        if entry[0] == "user":
            latest_user = entry
            break
    if latest_user is not None and latest_user not in recent:
        recent = [latest_user] + recent[-(WINDOW - 1):]

    parts = []  # type: List[str]
    for role, text, names in recent:
        marker = " <tool calls: {0}>".format(", ".join(names)) if names else ""
        parts.append("[{0}]: {1}{2}".format(role, text, marker))
    return _bounded_text("\n".join(parts), max_chars)


def build_classifier_request(messages, max_chars):
    # type: (Sequence[Any], int) -> ClassifierRequest
    """Build the bounded prompt. Raises when there is nothing to classify."""
    content = conversation_preview(messages, max_chars)
    if not content:
        raise ValueError("classifier requires a non-empty conversation")
    return ClassifierRequest(prompt=PROMPT_TEMPLATE.format(content=content))


def parse_complexity(value):
    # type: (Any) -> ComplexityLevel
    """Parse exactly one label. Prose or multiple labels is a rejection."""
    match = _LEVEL_RE.fullmatch(str(value))
    if not match:
        raise ValueError("classifier output must be exactly one complexity label")
    return ComplexityLevel[match.group(1).upper()]


def classify_heuristic(messages):
    # type: (Sequence[Any]) -> ComplexityLevel
    """Model-free classifier. Thresholds are part of the frozen contract."""
    view = conversation_messages(messages)
    text = " ".join(entry[1] for entry in view).lower()
    stripped = text.strip()
    token_count = len(_WHITESPACE_RE.split(stripped)) if stripped else 0
    user_turns = sum(1 for entry in view if entry[0] == "user")
    tool_call_depth = sum(1 for entry in view if entry[2])
    question_count = text.count("?") + text.count("？")

    if any(keyword in text for keyword in _REASONING_KEYWORDS):
        return ComplexityLevel.REASONING
    if any(keyword in text for keyword in _RESEARCH_KEYWORDS) or token_count > 1200:
        return ComplexityLevel.RESEARCH
    if (
        any(keyword in text for keyword in _COMPLEX_KEYWORDS)
        or token_count > 400
        or user_turns > 6
        or tool_call_depth > 2
        or question_count > 3
    ):
        return ComplexityLevel.COMPLEX
    if (
        any(keyword in text for keyword in _MEDIUM_KEYWORDS)
        or token_count > 80
        or user_turns > 2
    ):
        return ComplexityLevel.MEDIUM
    return ComplexityLevel.SIMPLE


def classify(messages, params):
    # type: (Sequence[Any], XRouterParams) -> Tuple[ComplexityLevel, str]
    """Classify a conversation. Never raises; degrades instead.

    Returns the tier and where it came from. No failure path may produce a
    higher tier than the classifier would have: a broken classifier must not
    cause an escalation.
    """
    backend = params.backend
    if backend is None:
        return _safe_heuristic(messages, SOURCE_HEURISTIC)

    try:
        request = build_classifier_request(messages, params.classifier_preview_chars)
    except Exception:
        return _safe_heuristic(messages, SOURCE_HEURISTIC_FALLBACK)

    try:
        raw = backend.classify(request)
    except Exception:
        return _safe_heuristic(messages, SOURCE_HEURISTIC_FALLBACK)

    try:
        return parse_complexity(raw), SOURCE_LLM
    except Exception:
        return _safe_heuristic(messages, SOURCE_PARSE_FAILED)


def _safe_heuristic(messages, source):
    # type: (Sequence[Any], str) -> Tuple[ComplexityLevel, str]
    try:
        return classify_heuristic(messages), source
    except Exception:
        return ComplexityLevel.MEDIUM, SOURCE_UNAVAILABLE
