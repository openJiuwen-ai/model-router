# Python 目录

Python 发行包名为 `jiuwen-model-router`，导入包名仍为 `openjiuwen`；构建与安装方式见[根 README](../README.md)。当前导入路径与 openJiuwen Core 重叠，两者请使用独立环境。

`python/` 明确分为两部分：

```text
python/
├── openjiuwen/                 # 正式包根：项目面向 Python 的公开接口
│   ├── __init__.py            # Router / ModelSelection / 注册入口
│   ├── algorithm_provider.py  # Python 算法契约（AlgorithmProvider）
│   ├── state_provider.py      # Python 状态契约（StateProvider）
│   ├── discover.py            # 扫描子包并默认安装，不引用具体算法名
│   ├── test_algo/             # 算法团队 demo（CostAwareAlgorithm）
│   ├── test_algo2/            # 算法团队 demo（LastAvailableAlgorithm）
│   ├── _openjiuwen.pyi        # PyO3 扩展类型桩
│   └── py.typed               # PEP 561 typed 包标记
└── custom_test_algo/           # wheel 消费者示例：导入子类即登记
    ├── prefer_first.py
    └── run_custom_algorithm.py
```

`openjiuwen` 是 maturin 混合工程的 Python 包根。PyO3 生成的
`openjiuwen._openjiuwen` 负责进入 Rust runtime，也负责把已注册的
Python 算法回调为 Rust `AlgorithmProvider` trait。随包实现放在 `openjiuwen`
的并列子包（`test_algo`、`test_algo2` …）；[`discover.py`](openjiuwen/discover.py)
只扫这些子包。`custom_test_algo/` 在包外，导入子类后按 `name` 自动写入槽位。

随包 demo 见 [`openjiuwen/test_algo/README.md`](openjiuwen/test_algo/README.md)；
自定义注册见 [`custom_test_algo/README.md`](custom_test_algo/README.md)。
