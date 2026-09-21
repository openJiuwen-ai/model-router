# openjiuwen-runtime

## 简介

`openjiuwen-runtime` 是 openjiuwen-router 的 **L4 运行层**：把协议、状态、算法装配成一个可调用的 `Router`。宿主只看这一个门面——`from_config` 装配，`route` 取决策，`report` 交反馈。北向契约是 [`RouterProvider`](src/router_provider.rs)：`Router` 实现它，`route` 投影为 `ModelSelection`。**本 crate 不调用模型**；选中谁之后由宿主自己去调后端。

运行期两个插件槽各生效一个：算法槽（`Box<dyn AlgorithmProvider>`）和 state 槽（`Arc<dyn StateProvider>`）。端云差异收敛在 TOML profile，不在 `route` 里分叉。

依赖 `openjiuwen-protocol`、`openjiuwen-state`、`openjiuwen-algorithms`，以及 `toml` / `serde`（只用于解析 profile）。协议层常用类型在 `lib.rs` 再导出，Rust 宿主可以只依赖本 crate。

## 为什么要单独一层 runtime

- **宿主接口收敛**：算法作者看 `AlgorithmProvider`，状态实现者看 `StateProvider`，宿主看 `Router` / `RouterProvider`。
- **决策与执行分离**：`route` 返回 `Decision` 即结束；流量不经过本层。
- **状态经入参注入**：`decide_loop` 先 `snapshot`，再把 `StateView` 塞进 `RouteContext`，算法从不直接访问 state。
- **装配错误提前暴露**：未知算法名、未知 backend、TOML 读失败在 `from_config` 就返回 `RouterError::Config`。
- **一套内核两种形态**：`memory` / `remote` 只在 `from_profile` 选一次实现。

Rust 原生宿主主路径：

```text
from_config → Router
宿主  route(RouteRequest, RouteHint)
        ├─ snapshot(RoutingKey) → StateView
        ├─ 合并 exclusions，装配 RouteContext
        └─ decide → Decision
宿主  自己调用 selected_model_id
宿主  report(Feedback) → state 写回
```

## 仓库结构

```text
crates/runtime/
├── Cargo.toml
├── README.md
└── src/
    ├── lib.rs            # 模块入口；重导出 Router、RouterProvider 与协议类型
    ├── router.rs         # Router 门面：from_config / route / report
    ├── router_provider.rs # 北向契约 RouterProvider
    ├── config.rs         # TOML profile 解析（RouterProfile）
    ├── registry.rs       # 算法池：按名取出一个 AlgorithmProvider
    ├── decide_loop.rs    # snapshot → RouteContext → decide
    ├── trigger.rs        # Trigger / TriggerSpec（骨架，未挂装配）
    └── training.rs       # TrainingJob / DataSelector / PublishPlan（骨架）
```

集成测试在仓库 `tests/react_agent.rs`，由本 crate 的 `[[test]]` 指向，不放在 `src/` 内。

## 快速开始

### 环境要求

与仓库根目录相同：Rust `stable`。Windows GNU 链接见根目录 `.cargo/config.toml`。

### 编译

在仓库根目录：

```bash
cargo build -p openjiuwen-runtime
cargo test -p openjiuwen-runtime
```

本 crate 的 `algo-*` feature 转发到 `openjiuwen-algorithms`（默认全开）。只要 passthrough：

```bash
cargo build -p openjiuwen-runtime --no-default-features --features algo-passthrough
```

profile 里的 `algorithm = "..."` 必须与编进产物的 feature 一致，否则 `registry` 返回 `RouterError::Config`。

## 样例 1：宿主调用

```rust
use openjiuwen_runtime::{
    Feedback, Outcome, RequestMetadata, RouteHint, RouteRequest, Router,
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let router = Router::from_config("config/edge.toml")?;

    let req = RouteRequest {
        metadata: RequestMetadata {
            session_id: Some("s1".into()),
            agent_id: Some("host-app".into()),
        },
        ..Default::default()
    };

    // route 返回前会填充 route_id（runtime 生成，算法不参与）。
    let decision = router.route(&req, &RouteHint::default())?;
    // 宿主自己调用 decision.selected_model_id 对应的模型

    // Feedback::ok 构造成功反馈；随后补齐关联字段。
    let mut feedback = Feedback::ok(req.routing_key(), &decision.selected_model_id, 40);
    feedback.route_id = decision.route_id.clone();
    feedback.observed_at_ms = Some(1_700_000_000_000);
    // Overflow / Unavailable 写入排除 hint；Rejected 不驱动排除。
    feedback.call.as_mut().unwrap().outcome = Outcome::Ok;

    // try_report 会校验并返回 FeedbackError；report 是静默丢弃非法值的兼容入口。
    router.try_report(feedback)?;
    Ok(())
}
```

`Feedback::delayed(key, model)` 用于没有单次调用结果的场景（`call = None`，MemoryState 不做更新；自定义 state 仍可消费扩展）。要挂载应用私有信号，用 `openjiuwen_protocol::{Extension, Value}` 填 `feedback.extensions`；该路径需直接依赖 protocol crate，`openjiuwen_runtime` 只重导出 `Feedback` / `Outcome` 等必需类型。

测试或配置中心下发文本时用 `Router::from_toml`。`Feedback.key` 必须与 `route` 时的 `RoutingKey` 相同，否则排除/亲和对不上。

## 样例 2：装配与决策循环

`from_profile` 做三件事：按名取算法、按 `state.backend` 选 `MemoryState` 或 `RemoteState`、收下 `targets.models` 作为目录。

`route` 每次把 `seed` 加一后交给 `decide_loop::run`：

1. 生成 `route_id`；无检索意图时调用 `state.snapshot`，否则把 id 交给 `state.query`
2. 合并 `req.exclusions` 与 `view.exclusions`，从目录里 `without`
3. 组装 `RouteContext { targets, view, retrieved, seed }`
4. `algorithm.decide(req, &ctx)` → `Decision`，随后由 runtime 填入 `route_id`

端到端（首选失败 → `report(Unavailable)` → 下次 snapshot 换模）见：

```bash
cargo test -p openjiuwen-runtime --test react_agent -- --nocapture
```

## 主要模块

### `Router`（`router.rs`）

| 方法 | 作用 |
|------|------|
| `from_config` / `from_toml` / `from_profile` | 启动期装配，失败为 `RouterError::Config` |
| `from_parts` / `state_from_profile` | 用已构造的算法与 state 装配；供 PyO3 注入 |
| `route` | 决策循环；返回 `Decision`（Rust 原生路径）。`RouterProvider::route` 再投影为 `ModelSelection` |
| `report` | 转发 `StateProvider::report`；骨架为同步写入 |
| `with_kv_coordinator` / `set_kv_coordinator` | 注册切换回调；骨架只保存，`route` 尚未触发 `on_switch` |
| `algorithm_name` | 当前算法槽的稳定名 |

`RouteHint` 已传入 `decide_loop`，骨架里尚未读 `cache_affinity`。

### `RouterProvider`（`router_provider.rs`）

应用把本项目当 plugin 嵌入时看这个 trait：`route` → `ModelSelection`，`report`，`algorithm_name`。装配不在 trait 上。

```rust
use openjiuwen_runtime::{RouteHint, RouteRequest, Router, RouterProvider};

let router = Router::from_config("config/edge.toml")?;
let req = RouteRequest::default();
let plugin: &dyn RouterProvider = &router;
let selection = plugin.route(&req, &RouteHint::default())?;
// 应用自己调用 selection.selected_model_id
```

### 配置（`config.rs`）

`RouterProfile` 对应 TOML：`algorithm`、`[state]`、`[targets]`、`[[evolving]]`。`evolving` 能解析，**尚未**挂到 `TriggerRegistry` / `TrainingJob`。

| `state.backend` | 实现 | 典型 profile |
|------------------|------|----------------|
| `memory` | `MemoryState`（TTL 默认 300s，容量默认 1024） | `config/edge.toml` |
| `remote` | `RemoteState`（必须配置 `endpoint`；超时默认 5ms） | `config/cloud.toml` |

### 注册表（`registry.rs`）

按字符串从算法池取出一个 `Box<dyn AlgorithmProvider>`。单槽：候选可以有很多，一个 `Router` 只持有一个实例。名字写错或 feature 未编译 → `unknown or disabled algorithm: {name}`。

### 触发与训练（`trigger.rs` / `training.rs`）

`Trigger` / `TriggerSpec`、`TrainingJob` / `DataSelector` / `PublishPlan` 类型已在，判定和后台调度还没有接到 `from_profile` 或 `route`。自演进计算在 `openjiuwen-algorithms::EvolvingProvider`，本层只负责何时跑、怎么写回。

`TrainingBatch` 有两条通道，来源不同：

- `feedbacks` 来自 `StateProvider`：state 是 hint 层，**不保留原始请求与决策**，只有聚合反馈。
- `prompts`（`TrainingPrompt` 三元组：请求 / 决策 / 反馈）来自**宿主 journal**，经 `DataSelector::with_prompts` 传入。runtime 在 `route` 时不落盘决策与请求，因此这是唯一能拿到原始 prompt 文本的通道。

`DataSelector::select` 当前只复制宿主提供的 `prompts`，不拉取 feedback，也不执行批量校验；宿主需显式校验批次后再训练。

### 与其它 crate 的关系

```text
宿主 → dyn RouterProvider（runtime 北向契约）
         ↑ runtime::Router 实现
         ├─ algorithms::AlgorithmProvider（decide）
         └─ openjiuwen_state::StateProvider（snapshot / report）
二者之间没有直接调用；耦合点是 RouteContext.view。
```

## 测试与检查

```bash
cargo fmt -p openjiuwen-runtime -- --check
cargo test -p openjiuwen-runtime
cargo test -p openjiuwen-runtime --test react_agent -- --nocapture
```

`passthrough_picks_first_target` 在 `router.rs` 内联测试里：装配 passthrough 后选目录第一个目标。
