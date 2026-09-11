"""Python 状态契约，对应 Rust `StateProvider`。

子类实现 `snapshot(key) -> dict | StateView` 与 `report(feedback)`，经 PyO3
反向包装成 Rust trait 对象。状态是 hint：可持有跨请求记忆，超时应返回空视图。

可选扩展：`query(key, query) -> dict | StateSnapshot`。它与 `snapshot` **平级**，
两者互不影响：

- 只实现 `snapshot`：不改任何代码即可继续工作；宿主若携带检索入参，Router 会
  自动降级为 `snapshot`（`retrieved` 为空）。
- 实现 `query`：可基于 `query.text` / `query.vector` / `query.top_k` 做 KNN 等
  更精细的检索，返回 `StateSnapshot(view=..., retrieved=[RetrievedItem, ...])`。
  检索失败或返回越界时同样降级为 `snapshot`，不阻塞路由。
"""

from __future__ import annotations


class StateProvider:
    """状态槽插件契约。与算法侧 `AlgorithmProvider` 对位。"""

    name = "unnamed"

    def snapshot(self, key):
        raise NotImplementedError("{0} must implement snapshot()".format(type(self).__name__))

    def report(self, feedback):
        raise NotImplementedError("{0} must implement report()".format(type(self).__name__))

    # 可选：默认不提供。未覆写时 Router 走 snapshot 降级路径。
    # def query(self, key, query):
    #     raise NotImplementedError
