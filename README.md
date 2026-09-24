# openjiuwen-router

[中文](README.zh.md)

## Overview

`openjiuwen-router` is the model-routing kernel for openJiuwen: it picks one model from the request and a state snapshot. The core is Rust, split across a Cargo workspace into protocol, state, algorithms, and runtime, with a Python facade (PyO3 / maturin) so an edge crate and a cloud wheel share the same decision logic.

The host (agent, gateway, or edge app) calls `Router::route`, invokes the selected model itself, then `report`s the outcome. Algorithms are pure functions: the same `(request, ctx)` must yield the same decision. Anything remembered across requests lives in state.

Core capabilities:

- `Router` facade: `from_config` / `route` / `report`. The northbound contract `RouterProvider` lives in runtime.
- One pluggable algorithm slot (`AlgorithmProvider`) and one state slot (`StateProvider`), each active at runtime.
- Protocol types: `RouteRequest`, `Decision`, `ModelSelection`, `Feedback`, `StateView`.
- Two TOML profiles for edge and cloud (in-process state / remote state client).
- A PyO3 extension and bundled Python algorithm packages (skeleton).

Architecture and plugin guide: [`docs/en/architecture.md`](docs/en/architecture.md) ([中文](docs/zh/architecture.md)). This repository is the workspace skeleton from the blueprint: layout, contracts, assembly, and one runnable ReAct path are in place. Weighted algorithms, remote state gRPC, and the full PyO3 binding are still stubs.

## Why this kernel

- **Decision and execution stay apart.** An algorithm returns only `selected_model_id` and `reasoning`. The host performs the model call, so the router is not on the traffic path.
- **Pure functions travel.** Algorithms do not call the selected target and do not hold mutable state. Edge, cloud, Rust, and Python hosts share one contract.
- **One slot, swapped at assembly.** A router instance runs one algorithm and one state implementation. Candidates come from the registry; the profile picks them.
- **State is a hint.** Losing it only degrades to a cold route. A remote implementation that hits its deadline returns an empty view instead of failing the request.
- **One kernel, two shapes.** Edge/cloud differences stay in the TOML profile, not in forked business code.
- **Embeddable.** A Rust host links `openjiuwen-runtime` (`Router` / `RouterProvider`). The cloud side can re-export that through PyO3.

## Repository layout

```text
model-router/
├── README.md                       # English
├── README.zh.md                    # Chinese
├── Cargo.toml                      # workspace
├── pyproject.toml                  # maturin: cloud-side Python wheel
├── crates/
│   ├── protocol/                   # L1 protocol (zero deps: request / decision / feedback / error)
│   ├── state/                      # L2 state: StateProvider trait + memory / remote
│   ├── algorithms/                 # L3 algorithms: AlgorithmProvider trait + built-ins (feature-gated)
│   ├── runtime/                    # L4 assembly and runtime: Router facade
│   └── py/                         # L5 PyO3 binding (cdylib `_openjiuwen`)
├── python/
│   ├── openjiuwen/                 # cloud Python facade, algorithm contract, bundled algorithms
│   │   ├── x_router/               # route by request complexity
│   │   ├── test_algo/              # bundled algorithm sample (discover registers it)
│   │   └── test_algo2/             # another bundled sample
│   └── custom_test_algo/           # out-of-package algorithm sample (registers by name on import)
├── config/
│   ├── edge.toml                   # edge: in-process memory
│   ├── cloud.toml                  # cloud: remote state
│   └── x-router-example.toml       # x-router: tier map + classifier
├── examples/
│   ├── python_integration.py       # Python host sample (run after maturin develop)
│   ├── x_router_cli.py             # x-router CLI (see how a profile routes)
│   └── rust_integration/           # Rust host sample (standalone mini crate, cargo run)
├── docs/
│   ├── zh/architecture.md          # architecture and plugin guide (Chinese)
│   └── en/architecture.md          # architecture and plugin guide (English)
└── tests/
    ├── react_agent.rs              # minimal ReAct host, checks the routing path
    ├── react_agent.py              # the same script as a Python host
    └── test_package.py             # Python package layout smoke test
```

## Quick start

### Requirements

- Rust `stable` (verified on `x86_64-pc-windows-gnu`).
- Python 3.8 or newer, when building the Python extension.
- `maturin >= 1.7`, when building the Python extension.
- Windows, Linux, or macOS. If the Windows GNU toolchain on `PATH` is LLVM-MinGW (no `libgcc`), `.cargo/config.toml` already points at the rustup linker:

```toml
[target.x86_64-pc-windows-gnu]
rustflags = ["-C", "link-self-contained=yes"]
```

Toolchain directories and rust-analyzer environment variables match the local `rust_demo_mod_04` setup (`RUSTUP_HOME` / `CARGO_HOME` in `.vscode/settings.json`).

### Build the Rust core

```bash
git clone <repository-url>
cd model-router
cargo check
cargo build
```

`crates/py` is not in the workspace `default-members`. Day-to-day `cargo build` / `cargo test` compile protocol, state, algorithms, and runtime only, and do not require PyO3.

Build just the runtime (its dependencies come along):

```bash
cargo build -p openjiuwen-runtime
```

### Build the Python extension

Install `maturin` in an active virtualenv, then:

```bash
maturin develop
```

That installs the `openjiuwen` package. `Router.from_config` takes a path or a dict. On the Python side `route` / `report` are async; the kernel underneath is still synchronous Rust. Cross-boundary types are `RouteRequest`, `ModelSelection` (alias `Decision`), and `Feedback`. Remote state uses `state.backend = "remote"` in the profile. A custom state uses a Python `StateProvider` (`state=` / `register_state`). `import openjiuwen` scans sibling packages for bundled Python algorithms and installs them into the Rust slot. An out-of-package `AlgorithmProvider` subclass registers by `name` on import (it must implement `decide` and be constructible with no arguments). `AlgorithmProvider` can still be imported when the extension is not built.

```python
import openjiuwen
from openjiuwen import Feedback, Outcome, Router

router = Router.from_config("config/cloud.toml")
# or Router.from_config({"algorithm": "passthrough", "state": {"backend": "memory"}, "targets": {"models": ["a"]}})

decision = await router.route({
    "messages": [{"role": "user", "content": "hi"}],
    "session_id": "s1",
    "agent_id": "host",
})
# the host invokes decision.selected_model_id itself
await router.report(Feedback.ok(decision, latency_ms=12, session_id="s1", agent_id="host"))
```

Python tests (`tests/test_package.py` does not need the extension; `tests/test_native_router.py` needs `_openjiuwen` installed):

```bash
pytest tests/test_package.py tests/test_native_router.py
```

## Example 1: native Rust host

The host links `openjiuwen-runtime`, calls `route` in-process, invokes the model itself, then `report`s. This is blueprint figure 2 (a direct crate dependency, no PyO3).

`Feedback` is a concrete struct. `Feedback::ok` builds a successful result; copy `route_id` from the `Decision` so the report joins that route. `Overflow` / `Unavailable` live on `call.outcome` and drive the exclusion hint. `call = None` means the outcome is not known yet. Field notes: [`crates/protocol/README.md`](crates/protocol/README.md).

```rust
use openjiuwen_runtime::{
    Feedback, RequestMetadata, RouteHint, RouteRequest, Router,
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
        "selected {} ({})",
        decision.selected_model_id, decision.reasoning
    );

    // The host calls the backend for decision.selected_model_id.
    // Model traffic does not pass through the router.

    let mut feedback = Feedback::ok(req.routing_key(), &decision.selected_model_id, 40);
    feedback.route_id = decision.route_id.clone();
    router.report(feedback);

    Ok(())
}
```

`Router::from_toml` is the other assembly entry (tests, or a config service that ships the text). `from_profile`:

1. Looks up one `Box<dyn AlgorithmProvider>` by the `algorithm` name (single slot).
2. Selects `MemoryState` or `RemoteState` from `state.backend` (single slot).
3. Collects `targets.models` into the catalog that `decide` later filters with exclusions.

A bad name, or an `algo-*` feature that was not compiled in, returns `RouterError::Config` at startup rather than on the first `route`.

Edge profile (`config/edge.toml`):

```toml
algorithm = "passthrough"

[state]
backend = "memory"
ttl_secs = 300
max_entries = 1024

[targets]
models = ["local-default"]
```

The cloud profile (`config/cloud.toml`) only switches `state.backend` to `remote`. The algorithm implementation stays the same Rust code.

## Example 2: minimal ReAct host

`route` before every model call, `report` after. The model is a mock; there is no network. The loop is Thought → Action → Observation → Final Answer.

- Rust host: [`tests/react_agent.rs`](tests/react_agent.rs)
- Python host (same Rust `Router`, through `python/openjiuwen`): [`tests/react_agent.py`](tests/react_agent.py)

```bash
cargo test -p openjiuwen-runtime --test react_agent -- --nocapture
python tests/react_agent.py
pytest tests/test_react_agent.py
```

Script:

1. Passthrough prefers `fast-local` → the mock is unavailable → `report(Unavailable)`.
2. State records that model in the exclusion hint → the next `route` selects `strong-cloud`.
3. The mock emits `Action: calc[21*2]`; the host computes 42 locally.
4. The second turn still uses `strong-cloud` and gets `Final Answer: 42`.

Expected output:

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

That path covers steps ①–⑨ of blueprint figure 2. The ReAct loop belongs to the host, not to `Router`.

## Example 3: minimal host integration (`examples/`)

[`examples/`](examples/) drops the ReAct loop and keeps only the routing closed loop. Use it as the starting point for your own host:

- Python: [`examples/python_integration.py`](examples/python_integration.py). After `maturin develop`, run `python examples/python_integration.py`.
- Rust: [`examples/rust_integration/`](examples/rust_integration/), a standalone mini crate. `cd examples/rust_integration && cargo run`.

Both show the same loop: `route` picks a model → the host calls it (mock) → `report` → a failure excludes that model and the next route switches.

## Modules

### Protocol (`openjiuwen-protocol`)

Zero-dependency base. Every cross-crate type lives here: `RouteRequest`, `Decision`, `ModelSelection`, `Feedback`, `StateView`, `RouterError`. Other crates talk only through this layer. Types and samples: [`crates/protocol/README.md`](crates/protocol/README.md).

### State (`openjiuwen-state`)

`StateProvider` is the only contract: `snapshot(key) -> StateView` and `report(feedback)`. The trait is in `state_provider.rs`; test and sample implementations are in `test_state/`. Edge `MemoryState` (TTL plus a capacity cap). Cloud `RemoteState` client (skeleton: a timeout degrades to an empty view).

### Algorithms (`openjiuwen-algorithms`)

`AlgorithmProvider::decide(request, ctx) -> Decision` is the only entry point for algorithm authors (`algorithm_provider.rs`). `EvolvingProvider::fit` is the pure-compute contract for online evolution (`evolving_provider.rs`). Samples live in `test_algo/` and are feature-gated. Choosing the Python build turns the matching feature off so the same algorithm is not shipped twice.

### Runtime (`openjiuwen-runtime`)

Hosts only see `Router`, which implements `RouterProvider`. `from_config` fills the two plugin slots. `route` runs snapshot → decide. `report` forwards to state. `RouterProvider` sits in this layer next to `Router`. `Trigger` / `TrainingJob` types exist but are not wired into assembly yet.

### Python facade (`crates/py` + `python/openjiuwen`)

The PyO3 extension `_openjiuwen` and the user package `openjiuwen`. Forward binding: `from_config(path|dict)`, `route`, `report`, and the protocol types. Reverse binding: an `AlgorithmProvider` subclass enters the slot when the class is defined; `register_state` wraps a Python `StateProvider` as the Rust trait. `discover` scans sibling packages and installs them on `import openjiuwen` (the demo is `test_algo/`).

## Tests and checks

```bash
cargo fmt --all -- --check
cargo check
cargo test
cargo test -p openjiuwen-runtime --test react_agent -- --nocapture
```

Python:

```bash
pytest tests/test_package.py tests/test_native_router.py
```

## Status

Working today:

- Five crate layers and the public contracts (`RouterProvider` / `AlgorithmProvider` / `StateProvider` / `Router`).
- `from_config` assembles the algorithm slot and the state slot.
- Passthrough decisions, memory exclusion hints, and the ReAct integration test.
- Python facade: `RouteRequest` / `ModelSelection` / `Feedback` bindings, plus reverse wrapping for `AlgorithmProvider` subclasses and `register_state`.

Still a skeleton, or not wired:

- `weighted` / `signal` / `ensemble` / `rule_cascade` currently degrade to "pick the first".
- `RemoteState` does not send gRPC yet (a timeout degrades to an empty view).
- `[[evolving]]` parses, but is not attached to `Trigger` / `TrainingJob`.
- `report` writes synchronously on the `memory` backend; the async side path from the blueprint is not built.

## Contributing

- Open issues and feature requests.
- Improve docs and examples.
- Send fixes and tests.

Before submitting, run at least `cargo fmt --all -- --check`, `cargo check`, and the tests for the modules you touched.

## License

Apache-2.0, matching the workspace `Cargo.toml`.

This project decides which model to call. It does not ship a model and it does not proxy model traffic. When you wire the router into a product, you are responsible for data security, content safety, licensing, and any other compliance duty that applies.
