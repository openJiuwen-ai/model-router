"""The classifier deployed with x_router.

It runs in the router's own process: ``LocalBackend`` calls ``ClassifierEngine``
directly, with no server and no network hop. Importing this package does not
import torch — the engine loads weights on first use.
"""

from __future__ import annotations

from .backend import LocalBackend, backend_from_config
from .engine import ClassifierEngine, EngineError

__all__ = ["ClassifierEngine", "EngineError", "LocalBackend", "backend_from_config"]
