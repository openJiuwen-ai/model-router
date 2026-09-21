"""安装 jiuwen-model-router wheel 之后，宿主侧自己写的算法示例。

不在 ``openjiuwen`` 包内，``discover`` 不会扫描到这里。导入本包会定义
``PreferFirstAlgorithm``，带稳定 ``name`` 的子类在定义时自动写入槽位。
"""

from .prefer_first import PreferFirstAlgorithm

__all__ = ["PreferFirstAlgorithm"]
