"""PyO3 扩展 `_openjiuwen` 的类型桩。Pylance / Pyright 靠它跳转，运行时仍加载 `.pyd`。"""

from typing import Any, List, Optional, Union, Dict

class Message:
    role: str
    content: str
    def __init__(self, role: str, content: str) -> None: ...

class RequestMetadata:
    session_id: Optional[str]
    agent_id: Optional[str]
    def __init__(self, session_id: Optional[str] = ..., agent_id: Optional[str] = ...) -> None: ...
    def routing_key(self) -> RoutingKey: ...

class RoutingKey:
    session_id: str
    agent_id: str
    def __init__(self, session_id: Optional[str] = ..., agent_id: Optional[str] = ...) -> None: ...

class StateQuery:
    """状态检索入参，`snapshot` 的平级可选扩展。"""
    text: Optional[str]
    vector: Optional[List[float]]
    top_k: Optional[int]
    extensions: List[Extension]
    def __init__(
        self,
        text: Optional[str] = ...,
        vector: Optional[List[float]] = ...,
        top_k: Optional[int] = ...,
        extensions: Optional[List[Union[Extension, dict]]] = ...,
    ) -> None: ...
    @staticmethod
    def text_query(text: str, top_k: Optional[int] = ...) -> StateQuery: ...

class RetrievedItem:
    """单条状态检索结果。"""
    @property
    def id(self) -> str: ...
    @property
    def score(self) -> float: ...
    @property
    def data(self) -> JSONValue: ...
    def __init__(self, id: str, score: float, data: JSONValue = ...) -> None: ...

class RouteHint:
    cache_affinity: Optional[str]
    state_query: Optional[StateQuery]
    def __init__(
        self,
        cache_affinity: Optional[str] = ...,
        state_query: Optional[StateQuery] = ...,
    ) -> None: ...

class RouteRequest:
    messages: List[Message]
    metadata: RequestMetadata
    exclusions: List[str]
    def __init__(
        self,
        messages: Optional[List[Message]] = ...,
        metadata: Optional[RequestMetadata] = ...,
        exclusions: Optional[List[str]] = ...,
    ) -> None: ...
    def routing_key(self) -> RoutingKey: ...

class FeedbackStats:
    sample_count: int
    def __init__(self, sample_count: int = ...) -> None: ...

class StateView:
    affinity: Optional[str]
    exclusions: List[str]
    stats: FeedbackStats
    def __init__(
        self,
        affinity: Optional[str] = ...,
        exclusions: Optional[List[str]] = ...,
        stats: Optional[FeedbackStats] = ...,
    ) -> None: ...

class RouteContext:
    targets: List[str]
    view: StateView
    retrieved: List[RetrievedItem]
    seed: int

class ModelSelection:
    selected_model_id: str
    reasoning: str
    is_answer_call: bool
    decision_id: Optional[str]
    def __init__(
        self,
        selected_model_id: str,
        reasoning: str,
        is_answer_call: bool = ...,
    ) -> None: ...
    @property
    def target(self) -> str: ...

Decision = ModelSelection

class CallFeedback:
    @property
    def outcome(self) -> str: ...
    @property
    def latency_ms(self) -> Optional[int]: ...
    @property
    def cache_valid(self) -> Optional[bool]: ...
    def __init__(self, outcome: str, latency_ms: Optional[int] = ..., cache_valid: Optional[bool] = ...) -> None: ...

JSONValue = Union[None, bool, int, float, str, List["JSONValue"], Dict[str, "JSONValue"]]

class Extension:
    @property
    def schema(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def data(self) -> JSONValue: ...
    def __init__(self, schema: str, version: str, data: JSONValue) -> None: ...

class Feedback:
    @property
    def version(self) -> int: ...
    event_id: Optional[str]
    decision_id: Optional[str]
    key: RoutingKey
    selected_model_id: str
    @property
    def observed_at_ms(self) -> Optional[int]: ...
    @property
    def call(self) -> Optional[CallFeedback]: ...
    @property
    def extensions(self) -> List[Extension]: ...
    outcome: Optional[str]
    latency_ms: Optional[int]
    cache_valid: Optional[bool]
    def __init__(
        self,
        key: RoutingKey,
        selected_model_id: str,
        outcome: str = ...,
        latency_ms: int = ...,
        cache_valid: Optional[bool] = ...,
        *,
        version: int = ...,
        event_id: Optional[str] = ...,
        decision_id: Optional[str] = ...,
        observed_at_ms: Optional[int] = ...,
        call: Optional[Union[CallFeedback, dict]] = ...,
        extensions: List[Union[Extension, dict]] = ...,
    ) -> None: ...
    @staticmethod
    def from_dict(value: Dict[str, Any]) -> Feedback: ...
    def to_dict(self) -> Dict[str, Any]: ...
    @classmethod
    def ok(
        cls,
        decision: Any,
        latency_ms: int,
        *,
        key: Any = ...,
        session_id: Optional[str] = ...,
        agent_id: Optional[str] = ...,
        selected_model_id: Optional[str] = ...,
        cache_valid: Optional[bool] = ...,
        outcome: str = ...,
    ) -> Feedback: ...

class Router:
    @staticmethod
    def from_config(config: Union[str, dict], *, state: Any = ...) -> Router: ...
    @staticmethod
    def from_toml(text: str) -> Router: ...
    def route(self, request: Any, hint: Any = ...) -> ModelSelection: ...
    def report(self, feedback: Any) -> None: ...
    def algorithm_name(self) -> str: ...
    def with_kv_coordinator(self, cb: Any) -> None: ...

def _register_algorithm(obj: Any) -> str: ...
def register_state(obj: Any) -> str: ...

OK: str
OVERFLOW: str
UNAVAILABLE: str
REJECTED: str
