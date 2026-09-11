# openjiuwen（PyO3 / L5）

## 简介

`crates/py` 是 openjiuwen-router 的 **L5 宿主集成层**：把 L4 `runtime::Router` 编成 Python 扩展 `_openjiuwen`。本层 **不含路由逻辑**，只做类型转换与同步调用。云侧 async 门面、算法 / 状态 SDK 在 `python/openjiuwen/`。

Python 宿主只看北向门面：`from_config` 装配，`route` 取 `ModelSelection`，自己调模型，再 `report`。决策与执行仍然分离；流量不经过本层。

```bash
maturin develop
```

## 北向功能接口

`import openjiuwen` 之后，宿主只用这些名字。对应内部是 `runtime::Router` 与 [`openjiuwen-protocol`](../protocol/README.md)。

### Router

与 Rust [`RouterProvider`](../runtime/README.md) 对齐：`route` / `report` / `algorithm_name`。装配不在该 trait 上，Python 仍走 `Router` 类方法。

| Python | 说明 |
|--------|------|
| `Router.from_config(path \| dict, *, state=None)` | 装配。dict 便于配置中心注入；`state=` 覆盖 profile，传入 Python `StateProvider` |
| `Router.from_toml(text)` | 测试或下发文本 |
| `await router.route(request, hint=None)` | 云侧 async 门面；内核同步。返回 `ModelSelection` |
| `router.route_sync(request, hint=None)` | 不要 event loop 时用 |
| `await router.report(feedback)` | fire-and-forget；Python 侧 async 立刻返回 |
| `router.report_sync(feedback)` | 同步转发 state |
| `router.algorithm_name()` | 当前算法槽稳定名 |
| `register_state(obj)` | 按 `obj.name` 写入进程内注册表；`state.backend` 命中后优先于 `memory` / `remote` |

`request` 可以是 `RouteRequest` 或 dict（`messages` / `metadata` 或顶层 `session_id`+`agent_id` / `exclusions`）。`hint` 可以是 `RouteHint`、`str`（当作 `cache_affinity`）、dict 或 `None`；dict 支持 `state_query` 键，可用 `StateQuery` 实例，也可直接给 `{"text": ..., "vector": [...], "top_k": ...}`。

`route` 返回 **`ModelSelection`**（`Decision` 是同一类型的别名）。字段：`selected_model_id` / `reasoning` / `is_answer_call`；`target` 是 `selected_model_id` 的别名。

远程状态走 profile `state.backend = "remote"` + `endpoint`；自定义状态走 `StateProvider`。

### 跨边界载荷

蓝图约定：PyO3 边界上编解码的是这三类。其余结构仅在反向绑定回调 Python 插件时再投影出来。

| Python 类型 | 北向用途 |
|-------------|----------|
| `RouteRequest` | 入参：`messages` + `metadata` + `exclusions` |
| `Message` | `role` / `content`；本层不解释角色语义 |
| `RequestMetadata` | `session_id` / `agent_id` → `routing_key()` |
| `RoutingKey` | 与 `Feedback.key` 必须同一把，排除才对得上 |
| `RouteHint` | 每请求 hint：`cache_affinity` + `state_query`（可选检索入参） |
| `ModelSelection`（`Decision`） | `route` 出参；宿主拿 `selected_model_id` 去调模型，并保留 `decision_id` 以便 `report` 关联 |
| `Feedback` | 回报：`key` + `selected_model_id` + `call`（`outcome` / `latency_ms?` / `cache_valid?`）+ `extensions` |
| `CallFeedback` | 单次调用结果；`call=None` 表示延迟反馈，state 不更新 |
| `Extension` | `schema` / `version` / `data`；宿主私有信号走这里，未知 schema 只做结构校验 |
| `StateQuery` | 检索入参：`text?` / `vector?` / `top_k?` / `extensions`；`StateQuery.text_query(text, top_k?)` 是文本便捷构造 |
| `RetrievedItem` | 单条命中：`id` / `score` / `data?`；也可用 dict 传入 |
| `Outcome` | `OK` / `OVERFLOW` / `UNAVAILABLE` / `REJECTED` |

`Feedback.ok(decision, latency_ms, *, key=..., session_id=..., agent_id=..., outcome=...)` 从决策上取模型名，并自动带上其 `decision_id`。`OVERFLOW` / `UNAVAILABLE` 写入 state 排除表；`REJECTED` 不排除；`OK` 更新亲和。

`report_sync(feedback)` 接受 `Feedback` 实例或 dict，两条路径语义一致：`call` 与旧式顶层字段（`outcome` / `latency_ms` / `cache_valid`）互斥，同时传会报错。非法反馈（版本不符、schema 为空、超出嵌套 / 字节 / 元素 / 条数预算、非有限数、整数越界、重复键）抛 `ValueError`，且**不写入 state**；校验统一在 Rust 侧 `Router::try_report` 完成。

额外异常映射：

| Python 异常 | 来源 |
|-------------|------|
| `ValueError`（`config: ...`） | `RouterError::Config`：未知算法、未知 backend、缺 `endpoint`、TOML 失败 |
| `ValueError`（校验信息） | `FeedbackError`：反馈未通过协议校验 |
| `RuntimeError`（`algorithm:` / `state:`） | `RouterError::Algorithm` / `State` |
| `LookupError` | `RouterError::NoTarget`：排除后目录为空 |

没有单独的 `openjiuwen.RouterError` 类型。

### 反向占用槽位

算法与状态都可以用 Python 实现，经 PyO3 包装成 Rust trait，与内置实现同一决策循环。

| Python | 契约 |
|--------|------|
| `AlgorithmProvider` | `name` + `decide(request, ctx) -> dict \| ModelSelection`。纯函数：不 I/O、不调模型，随机性只用 `ctx.seed` |
| `StateProvider` | `name` + `snapshot(key) -> dict \| StateView`、`report(feedback)`。状态是 hint；超时应返回空视图 |
| `StateProvider.query(key, query)` | **可选**。实现后可做文本 / 向量 KNN 等精细检索，返回 `dict`（`view` + `retrieved`）或 `StateSnapshot`。未实现时 Router 自动降级为 `snapshot` |
| `RouteContext` | 传给 `decide`：`targets` / `view` / `retrieved` / `seed` |
| `StateView` | `affinity` / `exclusions` / `stats`；可为空，算法必须能降级 |

`import openjiuwen` 会扫描并列子包并把随包算法写入槽位。包外只要 `import` 你的 `AlgorithmProvider` 子类就会登记。子类必须带稳定 `name`、实现 `decide`、能无参构造；配置写在类属性上。自定义状态仍走 `state=` / `register_state`。

状态检索是**可选能力**：老插件只写 `snapshot` 就能继续工作；需要精细检索时额外实现 `query`。宿主用 `RouteHint(state_query=StateQuery(...))` 传入检索意图，命中结果由算法从 `ctx.retrieved` 读取。检索失败、返回越界或 `query` 抛异常时，runtime 一律降级为 `snapshot`，`ctx.retrieved` 为空列表，路由不中断。

### 调用链

```text
Python 宿主
    Router.from_config(path | dict, *, state=?)
    await router.route(RouteRequest | dict, hint?)
        → _openjiuwen（类型转换）
        → runtime snapshot(RoutingKey) → decide
            或 query(RoutingKey, StateQuery) → StateSnapshot（携带检索意图时）
        → ModelSelection
    宿主自己调用 selected_model_id
    await router.report(Feedback)
        → state 写回；下一轮 snapshot 才看见排除 / 亲和
```

## 样例 1：装配、路由、回报

`session_id` / `agent_id` 构成 `RoutingKey`。宿主调完模型后再 `report`。

```python
from openjiuwen import Feedback, Outcome, Router

router = Router.from_config("config/cloud.toml")
# 或从配置中心：
# router = Router.from_config({
#     "algorithm": "passthrough",
#     "state": {"backend": "memory"},
#     "targets": {"models": ["fast-local", "strong-cloud"]},
# })

decision = await router.route({
    "messages": [{"role": "user", "content": "What is 21 * 2?"}],
    "session_id": "sess-1",
    "agent_id": "host-app",
})
# 宿主自己调用 decision.selected_model_id（或 decision.target）

await router.report(Feedback.ok(
    decision,
    latency_ms=40,
    session_id="sess-1",
    agent_id="host-app",
    outcome=Outcome.OK,
))
```

同步路径把 `await` 换成 `route_sync` / `report_sync`。

## 样例 2：失败排除

同一把 `RoutingKey` 上报 `UNAVAILABLE` 后，下一轮不再选该模型。

```python
from openjiuwen import Feedback, Outcome, RequestMetadata, RouteRequest, Router

router = Router.from_config({
    "algorithm": "passthrough",
    "state": {"backend": "memory"},
    "targets": {"models": ["fast-local", "strong-cloud"]},
})

req = RouteRequest(
    messages=[],
    metadata=RequestMetadata(session_id="s1", agent_id="a1"),
)
first = router.route_sync(req)
router.report_sync(Feedback.ok(
    first, latency_ms=1, key=req.routing_key(), outcome=Outcome.UNAVAILABLE,
))
second = router.route_sync(req)  # 不再选 first.selected_model_id
```

远程状态不经 Python 对象注入，只写 profile：

```python
router = Router.from_config({
    "algorithm": "passthrough",
    "state": {"backend": "remote", "endpoint": "http://127.0.0.1:9", "timeout_ms": 5},
    "targets": {"models": ["only"]},
})
```

## 样例 3：Python 算法与 Python 状态

`decide` 只看已过滤的 `ctx.targets`，返回 dict 即可。`snapshot` / `report` 按 `RoutingKey` 记住排除。

```python
from openjiuwen import AlgorithmProvider, Feedback, Outcome, Router, StateProvider

class PreferFirst(AlgorithmProvider):
    name = "custom_prefer_first"

    def decide(self, request, ctx):
        del request
        selected = ctx.targets[0]
        return {
            "selected_model_id": selected,
            "reasoning": "custom_prefer_first: first remaining target",
            "is_answer_call": True,
        }

class ExclusionStore(StateProvider):
    name = "python_exclusion_store"

    def __init__(self):
        self._exclusions = {}

    def snapshot(self, key):
        slot = (key.session_id, key.agent_id)
        return {"exclusions": list(self._exclusions.get(slot, [])), "affinity": None}

    def report(self, feedback):
        if feedback.outcome != Outcome.UNAVAILABLE:
            return
        slot = (feedback.key.session_id, feedback.key.agent_id)
        seen = self._exclusions.setdefault(slot, [])
        if feedback.selected_model_id not in seen:
            seen.append(feedback.selected_model_id)

store = ExclusionStore()

router = Router.from_config(
    {
        "algorithm": "custom_prefer_first",
        "state": {"backend": "memory"},
        "targets": {"models": ["fast-local", "strong-cloud"]},
    },
    state=store,
)
# 或 register_state(store) 后令 state.backend = "python_exclusion_store"

req = {"session_id": "s1", "agent_id": "host"}
first = router.route_sync(req)
router.report_sync(Feedback.ok(
    first, latency_ms=1, session_id="s1", agent_id="host",
    outcome=Outcome.UNAVAILABLE,
))
second = router.route_sync(req)
```

随包 demo（`openjiuwen.test_algo`）在 `import openjiuwen` 时已写入槽位，可直接 `algorithm = "python_cost_aware"`。端到端 ReAct 宿主：`python tests/react_agent.py`。

## 样例 4：可选的状态检索

只实现 `snapshot` 的插件不受影响；需要 KNN 时额外加 `query`，宿主通过 `RouteHint.state_query` 传入检索意图。

```python
from openjiuwen import RouteHint, Router, StateProvider, StateQuery


class VectorStore(StateProvider):
    name = "my_vector_store"

    def snapshot(self, key):
        return {}  # 无检索意图时的降级路径

    def query(self, key, query):
        hits = self._index.search(query.vector or self._embed(query.text),
                                  k=query.top_k or 8)
        return {
            "view": {"exclusions": [], "affinity": None},
            "retrieved": [{"id": h.id, "score": h.score, "data": h.payload} for h in hits],
        }

    def report(self, feedback):
        pass


router = Router.from_config(
    {
        "algorithm": "python_cost_aware",
        "state": {"backend": "memory"},
        "targets": {"models": ["fast-expensive", "slow-cheap"]},
    },
    state=VectorStore(),
)

hint = RouteHint(state_query=StateQuery(text="缓存策略", top_k=4))
# 或 StateQuery(vector=embed(text), top_k=4)，或直接给 dict：
# hint = {"state_query": {"text": "缓存策略", "top_k": 4}}
decision = router.route_sync({"session_id": "s1", "agent_id": "host"}, hint)
```

算法侧从 `ctx.retrieved` 读取命中（每项是 `RetrievedItem`，含 `id` / `score` / `data`）。检索入参有硬上限（文本 16 KiB、向量 4 096 维、`top_k` ≤ 256），超限会在构造 `StateQuery` 时抛 `ValueError`。
