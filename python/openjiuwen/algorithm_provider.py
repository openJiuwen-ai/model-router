"""Python 算法契约，对应 Rust `AlgorithmProvider`。

子类必须：设置稳定 ``name``、实现 ``decide(request, ctx)``、能无参构造。
类定义时校验并写入槽位。配置放在类属性上，不要靠构造参数。

纯函数契约（三条边界，按重要性排序）：

1. **不得调用被选中的目标模型。** 决策止于返回 ``selected_model_id``；
   调用目标模型是宿主（runtime / host）的职责。这是「决策与执行分离」
   的核心，不可让步。
2. **算法可以自带决策辅助模型。** 例如用一个小分类器判断请求复杂度、
   据此选档。它服务的是决策本身，读自己的参数、产出决策信号，
   与第 1 条不冲突。
3. **辅助模型调用应尽量保持无状态纯调用。** 无论调用自带的模型还是
   外部（含云端）辅助模型，都尽量避免在调用链中引入可变状态——例如
   服务端 / 客户端缓存、会话粘性、跨请求记忆等。这类状态会削弱
   「同输入 → 同输出」与可重放性，存在破坏整体架构设计的风险。

第 3 条是**算法开发者自身承担的责任**，不是框架能强制约束的：框架无法
阻止开发者在自己的模块里保存状态。因此把它写成纪律要求，而非运行时校验。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Type


_register: Optional[Callable[[Any], str]] = None
_pending: List[Type["AlgorithmProvider"]] = []


def bind_register(register: Optional[Callable[[Any], str]]) -> None:
    """由包初始化注入内部登记函数；扩展未构建时为 None。"""
    global _register
    _register = register
    if register is None:
        return
    waiting = list(_pending)
    _pending.clear()
    for cls in waiting:
        _commit(cls)


def _validate(cls: Type["AlgorithmProvider"]) -> Any:
    if getattr(cls, "decide", None) is AlgorithmProvider.decide:
        raise TypeError("{0} must implement decide(request, ctx)".format(cls.__name__))
    name = getattr(cls, "name", "unnamed")
    if not isinstance(name, str) or not name or name == "unnamed":
        raise TypeError("{0} must set a non-empty class attribute name".format(cls.__name__))
    try:
        return cls()
    except TypeError as exc:
        raise TypeError(
            "{0} must be constructible with no arguments; put config on the class".format(
                cls.__name__
            )
        ) from exc


def _commit(cls: Type["AlgorithmProvider"]) -> None:
    instance = _validate(cls)
    if _register is None:
        if cls not in _pending:
            _pending.append(cls)
        return
    _register(instance)


class AlgorithmProvider:
    """算法槽插件契约。与 state 侧 `StateProvider` 对位。"""

    name = "unnamed"

    def __init_subclass__(cls, **kwargs):
        super(AlgorithmProvider, cls).__init_subclass__(**kwargs)
        _commit(cls)

    def decide(self, request, ctx):
        raise NotImplementedError("{0} must implement decide()".format(type(self).__name__))


# 旧名。新代码请用 AlgorithmProvider。
Algorithm = AlgorithmProvider


def check_purity(algo, request, ctx, rounds=2):
    """同输入双调用比对输出，辅助验收纯函数纪律。"""
    results = [algo.decide(request, ctx) for _ in range(rounds)]
    first = results[0]
    for other in results[1:]:
        if other != first:
            raise AssertionError("AlgorithmProvider {0} is not pure: {1} != {2}".format(
                getattr(algo, "name", type(algo).__name__), first, other
            ))
    return first
