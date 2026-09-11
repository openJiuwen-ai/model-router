# openjiuwen-router

## 简介

`openjiuwen-router` 是 openJiuwen 的模型路由内核：根据请求与状态快照选出一个模型。项目以 Rust 为核心，通过 Cargo workspace 分层实现协议、状态、算法和运行时，并预留 Python（PyO3 / maturin）门面，方便端侧 crate 依赖与云侧 wheel 复用同一套决策逻辑。

宿主（agent / 网关 / 端侧应用）调用 `Router::route` 拿到 `Decision`，自己去调选中的模型，再把结果 `report` 回来。算法是纯函数——同样的 `(request, ctx)` 必须给出同样的决策；跨请求记忆全部外置到 state。

核心能力包括：

- `Router` 门面：`from_config` / `route` / `report`；北向契约 `RouterProvider` 在 runtime；
- 可插拔算法槽（`AlgorithmProvider`）与状态槽（`StateProvider`），运行期各生效一个；
- 协议层类型：`RouteRequest`、`Decision`、`ModelSelection`、`Feedback`、`StateView`；
- 端云两套 TOML profile（进程内 state / 远程 state 客户端）；
- 面向 Python 的 PyO3 扩展与内置 Python 算法包（骨架）。

架构与插件接入指南见 [`docs/zh/architecture.md`](docs/zh/architecture.md)（[English](docs/en/architecture.md)）。当前仓库是按蓝图搭起来的 workspace 骨架：目录、契约、装配和一条可跑的 ReAct 验证路径已经对齐；加权算法、远程 state gRPC、完整 PyO3 绑定仍是桩。

## 为什么选择这套内核

- **决策与执行分离**：算法只返回 `selected_model_id` 与 `reasoning`，模型调用由宿主履行，路由器不会成为流量瓶颈。
- **纯函数可复用**：算法不做 I/O、不持有可变状态，端云、Rust / Python 宿主共用同一契约。
- **单槽可插拔**：一个路由实例运行期只跑一个算法、一套 state；候选来自注册表，装配期选定。
- **状态是 hint**：丢失只降质为冷路由。远程实现硬超时返回空视图，而不是让请求失败。
- **一套内核、两种形态**：端云差异收敛在 TOML profile，不在业务代码里分叉。
- **可嵌入**：Rust 宿主静态链接 `openjiuwen-runtime`（`Router` / `RouterProvider`）。云侧可再经 PyO3 导出为 Python 扩展。

## 仓库结构

```text
model-router/
├── Cargo.toml                      # workspace
├── pyproject.toml                  # maturin：云侧 Python wheel
├── crates/
│   ├── protocol/                   # L1 协议层（零依赖：请求 / 决策 / 反馈 / 错误）
│   ├── state/                      # L2 状态：StateProvider trait + memory / remote
│   ├── algorithms/                 # L3 算法：AlgorithmProvider trait + 内置实现（feature 门控）
│   ├── runtime/                    # L4 装配与运行：Router 门面
│   └── py/                         # L5 PyO3 绑定（cdylib `_openjiuwen`）
├── python/
│   ├── openjiuwen/                 # 云侧 Python 门面、算法契约、随包算法
│   │   ├── x_router/                # 按请求复杂度分档选模型的算法
│   │   ├── test_algo/              # 随包算法示例（discover 自动登记）
│   │   └── test_algo2/             # 同上，另一个示例
│   └── custom_test_algo/           # 包外自定义算法示例（import 时按 name 登记）
├── config/
│   ├── edge.toml                   # 端侧：memory 进程内
│   ├── cloud.toml                  # 云侧：remote state
│   └── x-router-example.toml        # x-router：档位映射 + 分类器
├── examples/
│   ├── python_integration.py       # Python 宿主集成示例（maturin develop 后可运行）
│   ├── x_router_cli.py              # x-router 命令行驱动（看一份 profile 会怎么路由）
│   └── rust_integration/           # Rust 宿主集成示例（独立 mini crate，cargo run）
├── docs/
│   ├── zh/architecture.md          # 架构与插件接入指南（中文）
│   └── en/architecture.md          # Architecture and plugin guide (English)
└── tests/
    ├── react_agent.rs              # 最小 ReAct 宿主，验证路由主路径
    ├── react_agent.py              # 同一剧本的 Python 宿主版
    └── test_package.py             # Python 包布局冒烟
```

## 快速开始

### 环境要求

- Rust `stable`（本仓库在 `x86_64-pc-windows-gnu` 上验证过）；
- Python 3.8 或更高版本（构建 Python 扩展时使用）；
- `maturin >= 1.7`（构建 Python 扩展）；
- Windows / Linux / macOS。Windows GNU 工具链若 PATH 上是 LLVM-MinGW（缺 `libgcc`），仓库已在 `.cargo/config.toml` 指定 rustup 自带链接器：

```toml
[target.x86_64-pc-windows-gnu]
rustflags = ["-C", "link-self-contained=yes"]
```

工具链目录与 rust-analyzer 环境变量与本机 `rust_demo_mod_04` 对齐（`RUSTUP_HOME` / `CARGO_HOME` 见 `.vscode/settings.json`）。

### 编译 Rust 核心

```bash
git clone <repository-url>
cd model-router
cargo check
cargo build
```

`crates/py` 不在 workspace `default-members` 中，日常 `cargo build` / `cargo test` 只编 protocol、state、algorithms、runtime，不强制依赖 PyO3。

只构建运行时（会连带编进其依赖）：

```bash
cargo build -p openjiuwen-runtime
```

### 构建 Python 扩展

在已激活的 Python 虚拟环境中安装 `maturin`，然后执行：

```bash
maturin develop
```

安装后可以使用 `openjiuwen` 包。`Router.from_config` 接受路径或 dict；`route` / `report` 在 Python 侧是 async，同步内核仍在 Rust。跨边界类型是 `RouteRequest`、`ModelSelection`（别名 `Decision`）、`Feedback`。远程状态走 profile `state.backend = "remote"`；自定义状态用 Python `StateProvider`（`state=` / `register_state`）。`import openjiuwen` 会扫描并列子包中的随包 Python 算法并写入 Rust 槽；包外 `AlgorithmProvider` 子类在 import 时同样按 `name` 登记（必须实现 `decide`、能无参构造）。扩展未构建时，`AlgorithmProvider` 仍可单独导入。

```python
import openjiuwen
from openjiuwen import Feedback, Outcome, Router

router = Router.from_config("config/cloud.toml")
# 或 Router.from_config({"algorithm": "passthrough", "state": {"backend": "memory"}, "targets": {"models": ["a"]}})

decision = await router.route({
    "messages": [{"role": "user", "content": "hi"}],
    "session_id": "s1",
    "agent_id": "host",
})
# 宿主自己调用 decision.selected_model_id
await router.report(Feedback.ok(decision, latency_ms=12, session_id="s1", agent_id="host"))
```

Python 测试（`tests/test_package.py` 不需要扩展；`tests/test_native_router.py` 需要已安装的 `_openjiuwen`）：

```bash
pytest tests/test_package.py tests/test_native_router.py
```

## 样例 1：Rust 原生宿主

宿主静态链接 `openjiuwen-runtime`，进程内 `route` 取决策，自己调用模型后再 `report`。这是蓝图图 2 的形态（crate 直接依赖，无 PyO3）。

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

    let decision = router.route(&req, &RouteHint::default())?;
    println!(
        "选中模型 {} ({})",
        decision.selected_model_id, decision.reasoning
    );

    // 宿主自己调用 decision.selected_model_id 对应的模型后端
    // 路由器不经手流量

    router.report(Feedback {
        key: req.routing_key(),
        selected_model_id: decision.selected_model_id.clone(),
        outcome: Outcome::Ok, // Overflow / Unavailable 会写入排除 hint
        latency_ms: 40,
        cache_valid: None,
    });

    Ok(())
}
```

装配也可以用 `Router::from_toml`（测试或配置中心下发文本时）。`from_profile` 会：

1. 按 `algorithm` 名从注册表取出一个 `Box<dyn AlgorithmProvider>`（单槽）；
2. 按 `state.backend` 选择 `MemoryState` 或 `RemoteState`（单槽）；
3. 把 `targets.models` 收成目录，供后续 `decide` 剔除 exclusions。

名字写错或对应 `algo-*` feature 未编译，错误在启动时以 `RouterError::Config` 返回，不会拖到第一次 `route`。

端侧 profile 示例（`config/edge.toml`）：

```toml
algorithm = "passthrough"

[state]
backend = "memory"
ttl_secs = 300
max_entries = 1024

[targets]
models = ["local-default"]
```

云侧（`config/cloud.toml`）只把 `state.backend` 换成 `remote`，算法仍是同一份 Rust 实现。

## 样例 2：最小 ReAct 宿主验证路由

每一次模型调用前 `route`，调用后 `report`。模型由 mock 扮演，不发真实网络。循环是 Thought → Action → Observation → Final Answer。

- Rust 宿主：[`tests/react_agent.rs`](tests/react_agent.rs)
- Python 宿主（经 `python/openjiuwen` 调同一套 Rust `Router`）：[`tests/react_agent.py`](tests/react_agent.py)

```bash
cargo test -p openjiuwen-runtime --test react_agent -- --nocapture
python tests/react_agent.py
pytest tests/test_react_agent.py
```

剧本：

1. Passthrough 首选 `fast-local` → mock 返回不可用 → `report(Unavailable)`；
2. state 把该模型写入排除 hint → 再次 `route` 选中 `strong-cloud`；
3. mock 给出 `Action: calc[21*2]`，宿主本地算出 42；
4. 第二轮仍走 `strong-cloud`，得到 `Final Answer: 42`。

预期输出：

```text
ReAct: What is 21 * 2?
step 1
  route → fast-local (passthrough: first available target)
  fast-local failed (unavailable), report Unavailable
  route → strong-cloud (passthrough: first available target)
  thought: I should calculate.
  action: calc[21*2] → 42
step 2
  route → strong-cloud (passthrough: first available target)
  thought: I have the result.
  final: 42
test react_agent_routes_retries_and_answers ... ok
```

这条路径覆盖蓝图图 2 的 ①–⑨。ReAct 循环本身属于宿主，不属于 Router。

## 样例 3：最小宿主集成（examples/）

[`examples/`](examples/) 下是两个去掉 ReAct 循环、只保留路由闭环的最小示例，适合作为接入自己项目的起点：

- Python：[`examples/python_integration.py`](examples/python_integration.py)，`maturin develop` 后运行 `python examples/python_integration.py`；
- Rust：[`examples/rust_integration/`](examples/rust_integration/)，独立 mini crate，`cd examples/rust_integration && cargo run`。

两个示例演示同一条闭环：`route` 选模 → 宿主自己调模型（mock）→ `report` 回报 → 失败排除后自动换模。

## 主要模块

### 协议层（`openjiuwen-protocol`）

零依赖底座。全部跨模块类型都在这里：`RouteRequest`、`Decision`、`ModelSelection`、`Feedback`、`StateView`、`RouterError`。其他 crate 只经协议层对话。

### 状态层（`openjiuwen-state`）

`StateProvider` 是唯一契约：`snapshot(key) -> StateView`、`report(feedback)`。契约定义在 `state_provider.rs`，测试/示意实现在 `test_state/`。端侧 `MemoryState`（TTL + 容量上界）；云侧 `RemoteState` 客户端（骨架阶段超时降级为空视图）。

### 算法层（`openjiuwen-algorithms`）

`AlgorithmProvider::decide(request, ctx) -> Decision` 是算法团队的唯一接入点，定义在 `algorithm_provider.rs`；`EvolvingProvider::fit` 是在线自演进纯计算契约，定义在 `evolving_provider.rs`。测试/示意实现统一放在 `test_algo/`，并按 feature 门控；配置选 Python 版时关闭对应 feature，避免双份入产物。

### 运行层（`openjiuwen-runtime`）

宿主只看 `Router`（实现 `RouterProvider`）。`from_config` 装配两个插件槽；`route` 驱动 snapshot → decide；`report` 转发 state。`RouterProvider` 与 `Router` 同在本层。`Trigger` / `TrainingJob` 类型已占位，尚未挂到装配路径。

### Python 门面（`crates/py` + `python/openjiuwen`）

PyO3 扩展 `_openjiuwen` 与用户面包 `openjiuwen`。正向绑定：`from_config(path|dict)`、`route`、`report`、协议类型。反向绑定：`AlgorithmProvider` 子类定义时入槽，`register_state` 把 Python `StateProvider` 包装成 Rust trait。`discover` 扫描并列子包并在 `import openjiuwen` 时自动安装（demo 在 `test_algo/`）。

## 测试与检查

```bash
cargo fmt --all -- --check
cargo check
cargo test
cargo test -p openjiuwen-runtime --test react_agent -- --nocapture
```

Python 测试：

```bash
pytest tests/test_package.py tests/test_native_router.py
```

## 当前进度

已经能用：

- 五层 crate 目录与公开契约（`RouterProvider` / `AlgorithmProvider` / `StateProvider` / `Router`）；
- `from_config` 装配算法槽与 state 槽；
- passthrough 决策、memory 排除 hint、ReAct 集成测试；
- Python 门面：`RouteRequest` / `ModelSelection` / `Feedback` 绑定，以及 `AlgorithmProvider` 子类入槽 / `register_state` 反向包装。

仍是骨架 / 未接线：

- `weighted` / `signal` / `ensemble` / `rule_cascade` 目前退化为「选第一个」；
- `RemoteState` 尚未真正发 gRPC（超时降级为空视图）；
- `[[evolving]]` 能解析，但未挂到 `Trigger` / `TrainingJob`；
- `report` 在 `memory` 后端下是同步写入，蓝图中的异步旁路尚未做。

## 贡献

欢迎通过以下方式参与：

- 提交 Issue 和功能建议；
- 改进文档和示例；
- 提交修复和测试。

提交代码前，请至少运行 `cargo fmt --all -- --check`、`cargo check` 和受影响模块的测试。

## 许可证

本项目采用 Apache-2.0 发布（许可证声明与 workspace `Cargo.toml` 一致）。

本项目提供模型路由决策能力，不内置任何具体 AI 模型，也不转发模型请求流量。将路由接入具体业务场景时，使用者应自行承担数据安全、内容安全、许可及适用法律法规要求下的合规责任。
