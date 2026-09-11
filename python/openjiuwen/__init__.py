"""openjiuwen — 云侧 Python 门面。

Router / 协议类型由 PyO3 扩展 `_openjiuwen` 重导出；扩展未构建时包仍可导入，
`AlgorithmProvider` 与 `StateProvider` 可独立使用。`discover` 扫描并列子包中的随包 Python 算法，
`import openjiuwen` 时自动写入 Rust 槽。

`route` / `report` 在 Python 侧是 async（蓝图云侧门面）；同步内核仍在 Rust。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Union

from .algorithm_provider import Algorithm, AlgorithmProvider, bind_register, check_purity
from .state_provider import StateProvider

__all__ = [
    "AlgorithmProvider",
    "Algorithm",
    "check_purity",
    "StateProvider",
    "Router",
    "Decision",
    "ModelSelection",
    "RouteRequest",
    "RouteHint",
    "Message",
    "RequestMetadata",
    "RoutingKey",
    "Feedback",
    "CallFeedback",
    "Extension",
    "StateView",
    "StateQuery",
    "RetrievedItem",
    "RouteContext",
    "register_state",
    "Outcome",
]


class Outcome:
    """反馈结果。溢出与不可用会写入 state 排除表。"""

    OK = "ok"
    OVERFLOW = "overflow"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


if TYPE_CHECKING:
    from ._openjiuwen import (
        Feedback,
        CallFeedback,
        Extension,
        Message,
        ModelSelection,
        RequestMetadata,
        RouteContext,
        RouteHint,
        RouteRequest,
        RoutingKey,
        StateQuery,
        StateView,
        RetrievedItem,
        register_state,
        Router as NativeRouter,
    )

    _register_algorithm = None
else:
    try:
        from ._openjiuwen import (
            Feedback,
            CallFeedback,
            Extension,
            Message,
            ModelSelection,
            RequestMetadata,
            RouteContext,
            RouteHint,
            RouteRequest,
            RoutingKey,
            StateQuery,
            StateView,
            RetrievedItem,
            _register_algorithm,
            register_state,
            Router as NativeRouter,
        )
    except ImportError:  # pragma: no cover
        NativeRouter = None  # type: ignore[misc, assignment]
        Feedback = None  # type: ignore[misc, assignment]
        CallFeedback = None  # type: ignore[misc, assignment]
        Extension = None  # type: ignore[misc, assignment]
        Message = None  # type: ignore[misc, assignment]
        ModelSelection = None  # type: ignore[misc, assignment]
        RequestMetadata = None  # type: ignore[misc, assignment]
        RouteContext = None  # type: ignore[misc, assignment]
        RouteHint = None  # type: ignore[misc, assignment]
        RouteRequest = None  # type: ignore[misc, assignment]
        RoutingKey = None  # type: ignore[misc, assignment]
        StateQuery = None  # type: ignore[misc, assignment]
        StateView = None  # type: ignore[misc, assignment]
        RetrievedItem = None  # type: ignore[misc, assignment]
        _register_algorithm = None  # type: ignore[misc, assignment]
        register_state = None  # type: ignore[misc, assignment]


Decision = ModelSelection


def _require_native():
    """确保 native extension 已构建。"""
    if NativeRouter is None:
        raise ImportError(
            "native extension `_openjiuwen` is not built; run `maturin develop`"
        )
    return NativeRouter


class Router:
    """Python 门面：装配 / 同步内核调用 / async 包装。"""

    def __init__(self, native: Any) -> None:
        self._native = native

    @classmethod
    def from_config(cls, config: Union[str, dict], state: Any = None) -> Router:
        native_cls = _require_native()
        if state is None:
            return cls(native_cls.from_config(config))
        return cls(native_cls.from_config(config, state=state))

    @classmethod
    def from_toml(cls, text: str) -> Router:
        return cls(_require_native().from_toml(text))

    def route_sync(self, request: Any, hint: Any = None) -> Any:
        return self._native.route(request, hint)

    async def route(self, request: Any, hint: Any = None) -> Any:
        return self.route_sync(request, hint)

    def report_sync(self, feedback: Any) -> None:
        self._native.report(feedback)

    async def report(self, feedback: Any) -> None:
        self.report_sync(feedback)

    def algorithm_name(self) -> str:
        return self._native.algorithm_name()

    def with_kv_coordinator(self, cb: Any) -> Router:
        self._native.with_kv_coordinator(cb)
        return self


bind_register(_register_algorithm)
if _register_algorithm is not None:
    from .discover import install as _install_bundled_algorithms

    _install_bundled_algorithms()
