# 示例

宿主集成 openjiuwen-router 的最小可运行示例。两个示例演示同一条闭环：
`route` 选模型 → 宿主自己调模型 → `report` 回报 → 失败排除后自动换模。
模型调用均为 mock，不发真实网络。

| 示例 | 内容 | 运行方式 |
|---|---|---|
| [`python_integration.py`](python_integration.py) | Python 宿主：dict 配置装配、`route_sync` / `report_sync`、失败重试与排除 | 先 `maturin develop`，再 `python examples/python_integration.py` |
| [`rust_integration/`](rust_integration/) | Rust 宿主：独立 mini crate，path 依赖 `openjiuwen-runtime`，内联 TOML 装配 | `cd examples/rust_integration && cargo run` |
| [`x_router_cli.py`](x_router_cli.py) | x-router：从命令行路由请求，看 profile 下每条请求落到哪一档 | `python examples/x_router_cli.py "..."`（需要分类器权重，见 `config/x-router-example.toml`） |
| [`x_router_bandit.py`](x_router_bandit.py) | x-router bandit：同一类请求连跑十几轮，看 store 变热后 `bandit=override` 出现；分类器、模型、judge 全部 mock，秒级跑完 | `pip install numpy && python examples/x_router_bandit.py [turns]` |

预期输出（两个示例一致）：第一次 `route` 选中 `fast-local`，mock 故障后回报
`Unavailable`，state 写入排除 hint，第二次 `route` 自动切到 `strong-cloud` 并返回结果。

更完整的 ReAct 宿主示例见 `tests/react_agent.rs` 与 `tests/react_agent.py`；
架构与插件契约见 `docs/zh/architecture.md`（中文）和 `docs/en/architecture.md`（English）。
