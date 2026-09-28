"""The classifier deployed with x_router.

Both backends run without a server. Heavy inference packages are imported only
when the selected backend loads its model.
"""

from __future__ import annotations

from .backend import LocalBackend, VllmBackend, backend_from_config
from .engine import ClassifierEngine, EngineError

__all__ = ["ClassifierEngine", "EngineError", "LocalBackend", "VllmBackend", "backend_from_config"]
