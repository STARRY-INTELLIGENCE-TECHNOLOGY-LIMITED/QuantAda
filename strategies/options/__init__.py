"""框架内置期权样例策略。

这些样例只覆盖当前 Broker 已实现的安全子集：
- 买开/卖平单腿 Put、Call
- 现金担保单腿短 Put
- 有正股覆盖的 Covered Call
- 原子 Put Credit Spread

裸卖、Call 价差、铁鹰等未实现组合会失败关闭，不要在样例里伪造。
运行时请使用全限定名，例如
``strategies.options.sample_put_credit_spread_strategy.SamplePutCreditSpreadStrategy``。
各样例文件顶部有可复制的 ``python run.py`` 命令。
"""

from .sample_cash_secured_put_strategy import SampleCashSecuredPutStrategy
from .sample_covered_call_strategy import SampleCoveredCallStrategy
from .sample_long_option_strategy import SampleLongCallStrategy, SampleLongPutStrategy
from .sample_put_credit_spread_strategy import SamplePutCreditSpreadStrategy

__all__ = [
    "SampleCashSecuredPutStrategy",
    "SampleCoveredCallStrategy",
    "SampleLongCallStrategy",
    "SampleLongPutStrategy",
    "SamplePutCreditSpreadStrategy",
]
