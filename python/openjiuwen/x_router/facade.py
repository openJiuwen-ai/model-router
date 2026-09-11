"""Configuration, assembly, and request shaping.

Two kernel limitations are absorbed here, which is why this module exists at
all. Both are described in DESIGN.md section 5.

The algorithm slot cannot receive parameters from the profile, so
:func:`build_router` generates a configured subclass instead of handing
parameters to a constructor. When the kernel grows an ``[algorithm.params]``
table, this collapses into ``Router.from_config``.

The protocol layer accepts message content only as a string, while the
OpenAI-compatible shape allows a list of content parts — the form used for
multimodal messages and emitted by several client SDKs even for plain text.
Passing one through unflattened raises out of ``route()``, and a host that
catches routing errors and falls back to the local model, as it should, then
routes everything locally while looking healthy. :func:`normalize_messages`
flattens content to text before the request is built, which is lossless here
because x-router reads only text.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .classifier import backend_from_config
from .complexity import content_text
from .types import ComplexityBackend, ParamsError, XRouterParams

__all__ = [
    "SECTION",
    "build_request",
    "build_router",
    "load_profile",
    "normalize_messages",
]

# Top-level table this package reads. The Rust profile parser ignores unknown
# tables, so it can live in the same file as the kernel's own configuration.
SECTION = "x-router"


def _load_toml(path):
    # type: (str) -> Dict[str, Any]
    try:
        import tomllib  # type: ignore[import-not-found]  # 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise ParamsError(
                "reading a TOML profile on Python < 3.11 requires `tomli`; "
                "install it, or pass configuration as a dict instead"
            )
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def load_profile(config):
    # type: (Any) -> Dict[str, Any]
    """Return the parsed profile for either a TOML path or a mapping."""
    if isinstance(config, str):
        return _load_toml(config)
    if isinstance(config, Mapping):
        return dict(config)
    raise ParamsError("config must be a path to a TOML profile or a mapping")


def normalize_messages(messages):
    # type: (Sequence[Any]) -> List[Dict[str, Any]]
    """Flatten message content to text so the protocol layer accepts it.

    Non-text parts are dropped. See the module docstring for why this is not
    optional.
    """
    normalized = []  # type: List[Dict[str, Any]]
    for message in messages or ():
        if isinstance(message, Mapping):
            role = message.get("role")
            content = message.get("content")
        else:
            role = getattr(message, "role", None)
            content = getattr(message, "content", None)
        normalized.append(
            {
                "role": str(role or "user"),
                "content": content if isinstance(content, str) else content_text(content),
            }
        )
    return normalized


def build_request(messages, session_id="", agent_id="", exclusions=()):
    # type: (Sequence[Any], str, str, Sequence[str]) -> Dict[str, Any]
    """Build a route request dict with content already normalized."""
    return {
        "messages": normalize_messages(messages),
        "session_id": session_id,
        "agent_id": agent_id,
        "exclusions": list(exclusions),
    }


def build_params(config, backend=None):
    # type: (Any, Optional[ComplexityBackend]) -> XRouterParams
    """Build validated parameters from a profile's ``[x-router]`` section.

    The classifier is required unless the profile opts out explicitly. An
    injected backend replaces the configured one entirely and is not checked
    against the profile.
    """
    profile = load_profile(config)
    section = profile.get(SECTION)
    if section is None:
        raise ParamsError("profile has no [{0}] section".format(SECTION))
    if backend is None:
        backend = backend_from_config(section.get("classifier_model"))
    params = XRouterParams.from_mapping(section, backend=backend)
    params.validate_against((profile.get("targets") or {}).get("models") or [])
    return params


def build_router(config, backend=None, state=None):
    # type: (Any, Optional[ComplexityBackend], Any) -> Any
    """Assemble a Router with x-router configured from ``config``.

    ``config`` is a TOML path or a profile mapping. The algorithm is registered
    under the profile's own ``algorithm`` name, so the profile stays the single
    source of truth.
    """
    from .. import Router  # deferred: the package imports this subpackage
    from .algorithm import specialize

    profile = load_profile(config)
    name = profile.get("algorithm")
    if not isinstance(name, str) or not name.strip():
        raise ParamsError("profile must declare a non-empty `algorithm`")

    params = build_params(profile, backend=backend)

    # Load the classifier now. At request time a failure degrades to the
    # heuristic on purpose, so a model that never loads would otherwise show up
    # as a router that quietly stops escalating — healthy-looking and wrong.
    warmup = getattr(params.backend, "warmup", None)
    if callable(warmup):
        warmup()

    specialize(params, name=name)

    if state is None:
        return Router.from_config(config)
    return Router.from_config(config, state=state)
