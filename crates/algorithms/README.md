# openjiuwen-algorithms

## 简介

`openjiuwen-algorithms` 是 openjiuwen-router 的 **L3 算法层**：纯函数集合，只读 `RouteRequest` 与 `RouteContext`，返回 `Decision`。本 crate **不持有可变状态**；跨请求信息由 runtime 从 state 快照后经 `ctx.view` 注入。

算法团队的唯一接入点是 `AlgorithmProvider` trait（`name` / `decide`），与 state 侧 `StateProvider` 对位。在线自演进是另一条契约 `EvolvingProvider`（`name` / `fit`），同样纯计算；拉数据、调度、CAS 写回由 runtime 的 `TrainingJob` 履行。

本 crate 只依赖 `openjiuwen-protocol`。示意实现按 `algo-*` / `evolving-mf` feature 条件编译，配置选用 Python 版时关闭对应 feature，磁盘源码保留、不入产物。

## 为什么算法必须是纯函数

- **端云零改动复用**：语言边界上只有值进、值出，Rust 原生、Python 算法、gRPC 边车共用同一契约。
- **可表驱动测试**：一条用例 = `(request, ctx) → Decision`，无需 mock 执行器或时钟。
- **决策与执行分离**：算法不能 `await` 模型；宿主拿到 `Decision` 后自己调后端。
- **状态外置**：`ctx.view` 可为空，算法必须能降级为冷路由；不知道 state 在内存还是远端。
- **可重放**：随机性只能用 `ctx.seed`，不能读系统时钟或全局 RNG。

### 边界：哪些模型调用是允许的

「算法不调模型」需要精确化，否则会误伤合理实现（例如自带复杂度分类器的算法）：

1. **不得调用被选中的目标模型。** 决策止于返回 `Decision.selected_model_id`；
   调用目标模型是宿主（runtime / host）的职责。这是「决策与执行分离」的核心，不可让步。
2. **算法可以自带决策辅助模型。** 例如用一个小分类器判断请求复杂度、据此选档。
   它服务的是决策本身：读自己的参数、产出决策信号，与第 1 条不冲突。
3. **辅助模型调用应尽量保持无状态纯调用。** 无论调用自带模型还是外部（含云端）
   辅助模型，都应避免在调用链中引入可变状态——如缓存、会话粘性、跨请求记忆。
   这类状态会削弱「同输入 → 同输出」与可重放性，存在破坏整体架构设计的风险。

第 3 条是**算法实现者自身承担的责任**，不是框架能强制约束的：框架无法阻止
开发者在自己的模块里保存状态，因此它是纪律要求，而非运行时校验。

## 仓库结构

```text
crates/algorithms/
├── Cargo.toml
├── README.md
└── src/
    ├── lib.rs                        # 统一公开导出
    ├── algorithm_provider.rs         # AlgorithmProvider trait + RouteContext
    ├── evolving_provider.rs          # EvolvingProvider trait + TrainingBatch / Artifact
    └── test_algo/                    # 仅存放测试/示意实现
        ├── mod.rs
        ├── routing/                  # 路由示意实现（按 algo-* feature 编译）
        │   ├── mod.rs
        │   ├── passthrough.rs
        │   ├── weighted.rs
        │   ├── rule_cascade.rs
        │   ├── signal.rs
        │   └── ensemble.rs
        └── evolving/                 # 自演进示意实现
            ├── mod.rs
            └── mf.rs                 # MfWeights：fit 纯重算（骨架）
```

Python 契约在 `python/openjiuwen/algorithm_provider.py`，发现逻辑在 `python/openjiuwen/discover.py`；外部团队实现放在并列子包（如 `test_algo/`）。

## 快速开始

### 环境要求

与仓库根目录相同：Rust `stable`。本 crate 无额外系统依赖。

### 编译

在仓库根目录：

```bash
cargo build -p openjiuwen-algorithms
cargo test -p openjiuwen-algorithms
```

默认开启全部内置算法 feature。只要 passthrough：

```bash
cargo build -p openjiuwen-algorithms --no-default-features --features algo-passthrough
```

`openjiuwen-runtime` 通过自身 `algo-*` feature 转发到本 crate；profile 里的 `algorithm = "passthrough"` 必须与编进产物的 feature 一致，否则装配期报 `unknown or disabled algorithm`。

## 样例 1：实现一个算法

`AlgorithmProvider` 只含两个方法。`ctx.view` 为空时仍须给出合法 `Decision`。

```rust
use openjiuwen_algorithms::{AlgorithmProvider, RouteContext};
use openjiuwen_protocol::{Decision, RouteRequest, RouterError};

pub struct FirstAvailable;

impl AlgorithmProvider for FirstAvailable {
    fn name(&self) -> &str {
        "first_available"
    }

    fn decide(
        &self,
        request: &RouteRequest,
        ctx: &RouteContext,
    ) -> Result<Decision, RouterError> {
        let available = ctx.targets.without(&request.exclusions);
        let model = available.first().ok_or(RouterError::NoTarget)?;
        Ok(Decision::answer(model, "first available target"))
    }
}
```

`RouteContext` 由 runtime 在 `route` 时组装，算法不要自己去 snapshot：

| 字段 | 含义 |
|------|------|
| `targets` | 本次可选目标（已剔除请求 exclusions 与 state 排除 hint） |
| `view` | 状态快照；可为空，必须能降级 |
| `retrieved` | 可选的状态检索命中；无检索或检索失败时可为空 |
| `seed` | 显式注入的随机种子，保证可重放 |

算法填写 `Decision` 的 `selected_model_id`（与 `TargetSet` 对齐的语义名）、`reasoning`（原因）和 `is_answer_call`（是否应答调用）；`Decision::answer` 将 `route_id` 置空，由 runtime 在返回宿主前填入。

## 样例 2：在线自演进（EvolvingProvider）

`EvolvingProvider::fit` 回答「给我一批历史样本，重算出一份新参数集」。不允许 I/O。

`TrainingBatch` 有两条并行通道，算法按需读取，空批次是合法输入：

| 字段 | 内容 | 来源 |
|------|------|------|
| `feedbacks` | `Vec<Feedback>`：只有「选了谁、结果如何」 | `StateProvider`（hint 层，不保留请求与决策） |
| `prompts` | `Vec<TrainingPrompt>`：`请求 → 决策 → 反馈` 三元组，可带宿主渲染好的 `text` | 宿主 journal（runtime 不落盘决策与请求） |

只靠反馈就能建模的算法继续读 `feedbacks` 即可；需要原始 prompt 做 embedding 或监督信号的读 `prompts`。

```rust
use std::sync::Arc;
use openjiuwen_algorithms::{Artifact, EvolvingProvider, TrainingBatch};

pub struct MfWeights;

impl EvolvingProvider for MfWeights {
    fn name(&self) -> &str {
        "mf-weights"
    }

    fn fit(&self, batch: &TrainingBatch) -> Arc<Artifact> {
        // 富样本优先；缺失时降级到纯反馈。
        let samples = if batch.prompts.is_empty() {
            batch.feedbacks.len()
        } else {
            batch.prompts.len()
        };
        let payload = format!("samples={samples}").into_bytes();
        Arc::new(Artifact {
            kind: "MfWeights".into(),
            payload,
        })
    }
}
```

触发时机、DataSelector 拉数、CAS 写回属于 runtime（`trigger.rs` / `training.rs`），不在本 crate。TOML 里的 `[[evolving]]` 目前可解析，但尚未挂到 Router 装配路径。

## 主要模块

### `AlgorithmProvider`（路由决策）

运行期单槽：一个 `Router` 只跑一个实现。候选来自注册表（`runtime::registry`），由 profile `algorithm = "..."` 选中。示意实现在 `test_algo::routing`：

| 实现 | feature | `name()` | 现状 |
|------|---------|----------|------|
| `Passthrough` | `algo-passthrough` | `passthrough` | 选第一个未被排除的目标 |
| `Weighted` | `algo-weighted` | `weighted` | 骨架：退化为直通 |
| `RuleCascade` | `algo-rule_cascade` | `rule_cascade` | 骨架：退化为直通 |
| `Signal` | `algo-signal` | `signal` | 骨架：退化为直通 |
| `Ensemble` | `algo-ensemble` | `ensemble` | 骨架：退化为直通 |

### `EvolvingProvider`（参数自优化）

与 `AlgorithmProvider` 同为算法团队交付面，但不占路由单槽。由触发机制驱动，可多 job 并存。示意实现在 `test_algo::evolving`：骨架提供 `MfWeights`（`evolving-mf`），`fit` 返回空 `Artifact`。

`TrainingBatch` 的 `prompts` 通道由宿主 journal 提供，不从 state 来——state 只有聚合反馈，runtime 也不落盘请求与决策。算法只消费组装好的批次，不自己拉数。

### 与 runtime / state 的关系

算法**从不直接访问 state**。链路是：

```text
runtime.snapshot(key) → StateView
        ↓ 塞进 RouteContext.view
algorithm.decide(req, ctx) → Decision
        ↓ 宿主调模型
runtime.report(feedback) → state 写回（下一轮 snapshot 才能看见）
```

在接口层画一条 state↔algorithm 的调用边，会破坏「纯函数 + 状态外置」。

## 测试与检查

```bash
cargo fmt -p openjiuwen-algorithms -- --check
cargo test -p openjiuwen-algorithms
```

端到端选模（passthrough + 排除 hint + ReAct 宿主）在仓库根目录：

```bash
cargo test -p openjiuwen-runtime --test react_agent -- --nocapture
```
