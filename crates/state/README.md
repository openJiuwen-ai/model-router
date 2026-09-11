# openjiuwen-state

## 简介

`openjiuwen-state` 是 openjiuwen-router 的 **L2 状态层**：跨请求记忆的插件契约与内置实现。runtime 只面向 `StateProvider`（`snapshot` / `query` / `report` / `publish`）；算法从不直接访问本 crate。

状态是 hint：有界、可丢失。丢失只降质为冷路由。远程实现超时必须返回空 `StateView`，而不是让请求失败。

读入口有两个，**平级且互不影响**：`snapshot` 是基础读，`query` 是可选扩展。未覆写 `query` 时由 trait 默认实现降级为 `snapshot`，因此旧实现不改一行代码即可继续工作；需要文本 / 向量 KNN 等精细检索时才覆写 `query`。

本 crate 只依赖 `openjiuwen-protocol`。状态团队的唯一接入点是 `StateProvider` trait，与算法侧 `AlgorithmProvider` 对位：运行期单槽选一。

## 为什么状态必须外置

- **算法保持纯函数**：跨请求信息经 `snapshot` 变成 `StateView`，由 runtime 塞进 `RouteContext.view`。
- **端云同一契约**：端侧进程内 `memory`、云侧 `remote` 客户端，宿主只看 `StateProvider`。
- **失败可降级**：远程超时 → 空视图，请求继续；排除 / 亲和对不上只是冷启动。
- **写回与决策分离**：`report` 异步、尽力而为；下一轮 `snapshot` 才看得见。

## 仓库结构

```text
crates/state/
├── Cargo.toml
├── README.md
└── src/
    ├── lib.rs                        # 统一公开导出
    ├── state_provider.rs             # StateProvider trait + CasConflict
    ├── test_state/                   # 测试/示意实现
    │   ├── mod.rs
    │   ├── memory.rs                 # 端侧：TTL + 容量上界
    │   └── remote.rs                 # 云侧客户端：超时降级（骨架）
    └── service/
        ├── mod.rs                    # 独立状态服务占位（feature = service）
        └── main.rs                   # openjiuwen-state-service 入口
```

公共契约位于 crate 根目录；`test_state/` 只存放测试或示意实现，与算法层的 `test_algo/` 分工一致。

## 快速开始

### 环境要求

与仓库根目录相同：Rust `stable`。本 crate 无额外系统依赖。

### 编译

在仓库根目录：

```bash
cargo build -p openjiuwen-state
cargo test -p openjiuwen-state
```

独立状态服务（骨架，尚未挂 gRPC）：

```bash
cargo run -p openjiuwen-state --features service --bin openjiuwen-state-service
```

`openjiuwen-runtime` 按 profile `state.backend` 装配；`memory` / `remote` 必须与编进产物的实现一致。

## 样例 1：实现一个 StateProvider

`snapshot` 必须能立刻返回（远程超时给空视图）。`report` 不要回压 `route`。

```rust
use openjiuwen_state::{CasConflict, StateProvider};
use openjiuwen_protocol::{Feedback, RoutingKey, StateView};

pub struct NullState;

impl StateProvider for NullState {
    fn snapshot(&self, _key: &RoutingKey) -> StateView {
        StateView::empty()
    }

    fn report(&self, _feedback: Feedback) {}

    fn publish(&self, _slot: &str, _artifact: &[u8], _ver: u64) -> Result<(), CasConflict> {
        Ok(())
    }
}
```

`RoutingKey` 是 `{session_id, agent_id}`。`report` 与下一次 `snapshot` 必须用同一把键，排除 / 亲和对得上。

### 可选：覆写 `query` 做精细检索

`snapshot` 只能给固定的视图字段；需要按查询文本 / 向量检索时，覆写 `query`：

```rust
use openjiuwen_protocol::{
    Feedback, RetrievedItem, RoutingKey, StateQuery, StateQueryError, StateSnapshot, StateView,
};
use openjiuwen_state::{CasConflict, StateProvider};

pub struct KnnState {
    index: MyVectorIndex,
}

impl StateProvider for KnnState {
    fn snapshot(&self, _key: &RoutingKey) -> StateView {
        StateView::empty()
    }

    fn query(&self, _key: &RoutingKey, query: &StateQuery) -> Result<StateSnapshot, StateQueryError> {
        // embedding 属于 state 的 I/O；这里只示意检索结果的形状。
        let hits = self.index.search(query.text.as_deref(), query.vector.as_deref(), query.top_k);
        Ok(StateSnapshot {
            view: StateView::empty(),
            retrieved: hits
                .into_iter()
                .map(|h| RetrievedItem::new(h.id, h.score))
                .collect(),
        })
    }

    fn report(&self, _feedback: Feedback) {}

    fn publish(&self, _slot: &str, _artifact: &[u8], _ver: u64) -> Result<(), CasConflict> {
        Ok(())
    }
}
```

入参有硬上限，超限在 runtime 侧就被拒并降级：文本 16 KiB、向量 4 096 维、`top_k` ≤ 256、命中条数 ≤ 256。`query` 返回的 `StateSnapshot` 会在交给算法前再做一次校验（条数 / 分数有限 / 载荷预算）；不合规或 `Err` 时 runtime 自动退回 `snapshot`，请求照常完成。

宿主通过 `RouteHint.state_query` 触发检索；未携带检索意图时不会调用 `query`。

## 样例 2：端侧内存实现

```rust
use std::time::Duration;
use openjiuwen_state::MemoryState;

let state = MemoryState::new(Duration::from_secs(300), 1024);
```

`Unavailable` / `Overflow` 写入 `view.exclusions`；`Ok` 更新 `affinity`；`Rejected` 只记统计。TTL 过期后该键视为空。容量满时丢最旧条目（骨架，不是完整 LRU）。

## 主要模块

### `StateProvider`（跨请求记忆）

运行期单槽：一个 `Router` 只跑一个实现。候选由 profile `state.backend` 选定。示意实现在 `test_state`：

| 实现 | `backend` | 现状 |
|------|------------|------|
| `MemoryState` | `memory` | TTL + 容量上界；排除 / 亲和写入进程内 HashMap |
| `RemoteState` | `remote` | 必须配置 `endpoint`；骨架不发网络，snapshot 返回空视图 |

### `service`（独立进程）

云侧状态服务入口。`feature = "service"` 才编 `openjiuwen-state-service`。骨架只打印占位，gRPC `snapshot` / `report` / `publish` 尚未接线。

### 与 algorithm / runtime 的关系

state **从不调用算法**。链路是：

```text
runtime.snapshot(key) → StateView            # 基础路径
runtime.query(key, StateQuery) → StateSnapshot  # 可选：携带检索意图时
        ↓ 塞进 RouteContext.view / .retrieved
algorithm.decide(req, ctx) → Decision
        ↓ 宿主调模型
runtime.report(feedback) → state 写回（下一轮 snapshot 才能看见）
```

在接口层画一条 state↔algorithm 的调用边，会破坏「纯函数 + 状态外置」。

## 测试与检查

```bash
cargo fmt -p openjiuwen-state -- --check
cargo test -p openjiuwen-state
```

端到端排除 hint（`report(Unavailable)` → 下次 snapshot 换模）在仓库根目录：

```bash
cargo test -p openjiuwen-runtime --test react_agent -- --nocapture
```
