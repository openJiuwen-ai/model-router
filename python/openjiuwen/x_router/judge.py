"""The bandit's ruler: a rubric for scoring one assistant turn, and its parser.

The bandit averages ``quality`` across turns, hosts and time, so every host has
to measure with the same instrument or the numbers — and ``margin`` — mean
nothing. This module is that instrument. It follows the classifier's pattern:
the package fixes the question and how the answer is read; the host supplies
the model that answers (:class:`JudgeBackend`) and runs it, because scoring
needs the response, the tool calls and the cost, none of which the router ever
sees.

    request = build_judge_request(messages, response_text, tool_calls)
    quality = parse_judge_score(my_judge.score(request))       # in [-1, 1]
    router.report_sync(build_bandit_feedback(selection, {SERVED: (quality, cost_usd)}, ...))

A host with ground truth (a grader, a test suite) can skip the judge and
report its own quality; only the value range is a contract.

Ported from agent-xrouter's ``evolution/judge.py``; the rubric and weights are
unchanged so scores stay comparable with that lineage.

Two backends ship below — weights in this process, or an OpenAI-compatible
API — selected by ``[x-router.judge_model] kind``. Either way the host still
decides when to score and calls ``score()`` itself; what the backends settle is
only *which model* answers and who holds it. Bringing your own is one method:
``score(request) -> str``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - 3.7 and below are out of support
    Protocol = object  # type: ignore[assignment, misc]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls

from .complexity import content_text
from .types import ParamsError, field

__all__ = [
    "JUDGE_SYSTEM_PROMPT",
    "RUBRIC_WEIGHTS",
    "ApiJudgeBackend",
    "JudgeBackend",
    "JudgeError",
    "JudgeRequest",
    "LocalJudgeBackend",
    "build_judge_request",
    "judge_from_config",
    "parse_judge_score",
]

# Three signed criteria; the weights are the rubric's, not tunables. Changing
# them makes every stored quality incomparable with every new one.
RUBRIC_WEIGHTS = {"task_progress": 0.45, "correctness": 0.35, "grounding": 0.20}

JUDGE_SYSTEM_PROMPT = """You are a strict evaluator of a single assistant turn in an agent conversation.
Score only the assistant turn from -1.0 to 1.0 for each criterion. Positive is good, zero is borderline, and negative is bad.
- task_progress: +1 makes decisive concrete progress; 0 neither advances nor hurts; -1 stalls, is off-task, or moves backward.
- correctness: +1 is correct and appropriate; 0 is partly correct or questionable; -1 is wrong or uses an invalid tool/arguments.
- grounding: +1 is fully consistent with the conversation; 0 has minor unsupported details; -1 invents facts, files, or tools.
Reply with ONLY a JSON object with task_progress, correctness, and grounding numeric fields."""

# Budgets for the prompt; middle-truncated so both the task and the latest
# exchange survive.
TRANSCRIPT_CHARS = 3000
RESPONSE_CHARS = 2000
TOOL_CALLS_CHARS = 1200
TOOL_NAMES_CHARS = 400

_FLOAT = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
_JSON_OBJECT = re.compile(r"\{[^{}]*\}")
_TRUNCATION_MARK = "\n...[truncated]...\n"


@dataclass(frozen=True)
class JudgeRequest:
    """The prompt handed to a judge backend: a system rubric and one turn to score."""

    system_prompt: str
    user_prompt: str


@runtime_checkable
class JudgeBackend(Protocol):
    """Scorer transport, supplied by the host — the judge's ``ComplexityBackend``.

    Synchronous in signature for symmetry; a host that awaits its model can
    simply pass what it awaited to :func:`parse_judge_score`.
    """

    def score(self, request):
        # type: (JudgeRequest) -> str
        """Return the judge model's raw reply."""


def _truncate_middle(text, limit):
    # type: (str, int) -> str
    if len(text) <= limit:
        return text
    half = max(0, (limit - len(_TRUNCATION_MARK)) // 2)
    return text[:half] + _TRUNCATION_MARK + text[-half:]


def _tool_names(tools):
    # type: (Sequence[Any]) -> List[str]
    names = []  # type: List[str]
    for tool in tools or ():
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        spec = function if isinstance(function, Mapping) else tool
        name = spec.get("name")
        if name:
            names.append(str(name))
    return names


def build_judge_request(messages, response_text, tool_calls=(), tools=()):
    # type: (Sequence[Any], Optional[str], Sequence[Any], Sequence[Any]) -> JudgeRequest
    """Frame one assistant turn for the judge.

    ``messages`` is the conversation the turn answered, in the same shape
    ``build_request`` accepts. ``response_text`` and ``tool_calls`` are what the
    model produced; ``tools`` (OpenAI tool schemas) tells the judge what it was
    allowed to call. Everything is bounded so the request fits a small judge.
    """
    lines = []  # type: List[str]
    for message in messages or ():
        role = str(field(message, "role") or "user")
        text = content_text(field(message, "content"))
        lines.append("[{0}]: {1}".format(role, text))
    transcript = _truncate_middle("\n".join(lines), TRANSCRIPT_CHARS)

    turn = []  # type: List[str]
    text = (response_text or "").strip()
    if text:
        turn.append(_truncate_middle(text, RESPONSE_CHARS))
    if tool_calls:
        rendered = json.dumps(list(tool_calls), ensure_ascii=False, default=str, separators=(",", ":"))
        turn.append("TOOL CALLS: " + rendered[:TOOL_CALLS_CHARS])

    names = ", ".join(_tool_names(tools))[:TOOL_NAMES_CHARS]
    tools_line = "AVAILABLE TOOLS: {0}\n\n".format(names) if names else ""

    user_prompt = (
        "<transcript>\n{0}\n</transcript>\n\n"
        "{1}<assistant_turn>\n{2}\n</assistant_turn>\n\n"
        "Score the assistant turn. Do not continue the conversation. Output only the JSON object."
    ).format(transcript, tools_line, "\n".join(turn))
    return JudgeRequest(system_prompt=JUDGE_SYSTEM_PROMPT, user_prompt=user_prompt)


def parse_judge_score(raw):
    # type: (Any) -> float
    """Combine the judge's reply into one signed quality in ``[-1, 1]``.

    Prefers the first JSON object that carries at least one rubric field, with
    each field weighted by :data:`RUBRIC_WEIGHTS` and the weights renormalised
    over the fields present; a field outside ``[-1, 1]`` is ignored rather than
    clamped. Falls back to the last bare number in the text. Raises
    ``ValueError`` when neither yields a score, so a judge that stopped
    answering the question is noticed rather than averaged in as zero.
    """
    text = str(raw).strip()
    for match in _JSON_OBJECT.finditer(text):
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        values = {}  # type: Dict[str, float]
        for key in RUBRIC_WEIGHTS:
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if -1.0 <= float(value) <= 1.0:
                values[key] = float(value)
        if values:
            total = sum(RUBRIC_WEIGHTS[key] for key in values)
            return sum(RUBRIC_WEIGHTS[key] * values[key] for key in values) / total
    numbers = _FLOAT.findall(text)
    if not numbers:
        raise ValueError("judge output must contain rubric JSON or a signed score")
    score = float(numbers[-1])
    if not -1.0 <= score <= 1.0:
        raise ValueError("judge score must be within [-1, 1], got {0}".format(score))
    return score


# --------------------------------------------------------------------------
# Backends: the model behind JudgeBackend, local or over an API
# --------------------------------------------------------------------------
#
# Failures raise JudgeError rather than degrade: a judge that cannot answer must
# not become a stream of zeros in the store. The host catches it and skips the
# report. Heavy imports (torch, transformers) stay inside the local backend's
# engine; the API backend uses only the standard library.

# A rubric answer is a small JSON object; 64 tokens covers it with room for a
# model that insists on a code fence. The classifier's 16 would truncate it.
DEFAULT_MAX_TOKENS = 64
DEFAULT_TIMEOUT_SECS = 30.0
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


class JudgeError(RuntimeError):
    """The judge could not produce a reply. Raised, never turned into a score."""


def _messages(request):
    # type: (JudgeRequest) -> list
    return [
        {"role": "system", "content": request.system_prompt},
        {"role": "user", "content": request.user_prompt},
    ]


class LocalJudgeBackend(object):
    """Runs the rubric on weights held in this process.

    Wraps the same engine the classifier uses (``ClassifierEngine`` is a plain
    chat-template-and-generate loop). Pass the classifier's engine to share one
    set of loaded weights between the two — cheap for a demo, though a model
    small enough to classify well is usually too small to judge well.
    """

    def __init__(self, model_path=None, engine=None, max_tokens=DEFAULT_MAX_TOKENS, **engine_kwargs):
        # type: (Any, Any, int, Any) -> None
        if engine is None:
            from .classifier.engine import ClassifierEngine

            engine = ClassifierEngine(model_path, **engine_kwargs)
        self.engine = engine
        self.max_tokens = int(max_tokens)

    def score(self, request):
        # type: (JudgeRequest) -> str
        try:
            prompt = self.engine.build_prompt(_messages(request))
            return self.engine.generate(prompt, max_new_tokens=self.max_tokens)
        except Exception as exc:
            raise JudgeError("local judge failed: {0}".format(exc)) from exc

    def warmup(self):
        # type: () -> None
        """Load weights now, so a broken deployment fails at assembly."""
        self.engine.load()


def _urllib_transport(url, headers, body, timeout):
    # type: (str, Dict[str, str], bytes, float) -> bytes
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise JudgeError("judge API returned HTTP {0}: {1}".format(exc.code, detail)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise JudgeError("judge API unreachable: {0}".format(exc)) from exc


class ApiJudgeBackend(object):
    """Runs the rubric through an OpenAI-compatible ``/chat/completions`` endpoint.

    Standard library only; ``transport`` is injectable for tests and for hosts
    that want their own HTTP client, retries or tracing. The API key is read
    from the environment at construction, so a missing key fails at assembly
    rather than on the first score.
    """

    def __init__(
        self,
        base_url,
        model,
        api_key=None,
        api_key_env=DEFAULT_API_KEY_ENV,
        timeout_secs=DEFAULT_TIMEOUT_SECS,
        max_tokens=DEFAULT_MAX_TOKENS,
        headers=None,
        transport=None,
    ):
        # type: (str, str, Optional[str], str, float, int, Optional[Mapping[str, str]], Optional[Callable[..., bytes]]) -> None
        if not isinstance(base_url, str) or not base_url.strip():
            raise ParamsError("judge_model.base_url must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise ParamsError("judge_model.model must be a non-empty string")
        if api_key is None:
            api_key = os.environ.get(api_key_env or "", "")
            if not api_key:
                raise ParamsError(
                    "judge_model.api_key_env names {0!r}, which is not set in the environment".format(api_key_env)
                )
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout_secs = float(timeout_secs)
        self.max_tokens = int(max_tokens)
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer {0}".format(api_key),
        }
        self._headers.update(dict(headers or {}))
        self._transport = transport or _urllib_transport

    def score(self, request):
        # type: (JudgeRequest) -> str
        payload = {
            "model": self.model,
            "messages": _messages(request),
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw = self._transport(self.url, dict(self._headers), body, self.timeout_secs)
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise JudgeError("judge API reply is not a chat completion: {0!r}".format(raw)[:600]) from exc
        if isinstance(content, list):  # content-parts form
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, Mapping)
            )
        if not isinstance(content, str):
            raise JudgeError("judge API reply has no text content")
        return content


def judge_from_config(config, classifier=None):
    # type: (Any, Any) -> Any
    """Build a judge backend from a ``[x-router.judge_model]`` table.

    ``kind`` selects the backend. For ``local``, ``share_classifier = true``
    reuses the engine behind ``classifier`` (the router's ``LocalBackend``)
    instead of loading a second model.
    """
    if not isinstance(config, Mapping) or not config:
        raise ParamsError("x-router needs a [judge_model] table to build a judge")
    kind = config.get("kind")
    if kind == "local":
        max_tokens = config.get("max_tokens", DEFAULT_MAX_TOKENS)
        if config.get("share_classifier", False):
            engine = getattr(classifier, "engine", None)
            if engine is None:
                raise ParamsError(
                    "judge_model.share_classifier needs the router's local classifier; "
                    "pass it as `classifier`, or set share_classifier = false and a model_path"
                )
            return LocalJudgeBackend(engine=engine, max_tokens=max_tokens)
        model_path = config.get("model_path")
        if not model_path:
            raise ParamsError("judge_model.kind = 'local' needs a model_path")
        return LocalJudgeBackend(
            model_path=model_path,
            max_tokens=max_tokens,
            device=config.get("device", "auto"),
            dtype=config.get("dtype", "auto"),
            max_input_tokens=config.get("max_input_tokens", 4096),
        )
    if kind == "api":
        return ApiJudgeBackend(
            base_url=config.get("base_url"),
            model=config.get("model"),
            api_key_env=config.get("api_key_env", DEFAULT_API_KEY_ENV),
            timeout_secs=config.get("timeout_secs", DEFAULT_TIMEOUT_SECS),
            max_tokens=config.get("max_tokens", DEFAULT_MAX_TOKENS),
            headers=config.get("headers"),
        )
    raise ParamsError("judge_model.kind must be 'local' or 'api', got {0!r}".format(kind))
