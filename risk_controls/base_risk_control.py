from abc import ABC, abstractmethod
from types import SimpleNamespace


class BaseRiskControl(ABC):
    """
    风控模块的抽象基类
    风控模块在策略的 'next' 逻辑之前被调用。
    """

    params = {}

    def __init__(self, broker, params=None):
        """
        初始化风控模块
        :param broker: 策略执行器 (BacktraderStrategyWrapper 实例)
        :param params: 从命令行传入的参数字典
        """
        self.broker = broker
        # 1. 合并类级别定义的默认参数和实例化时传入的参数
        final_params = self.params.copy()
        if params:
            final_params.update(params)

        # 2. 使用辅助类将最终的参数字典转换为一个对象
        self.params = SimpleNamespace(**final_params)

        # 3. 创建 'p' 作为 'params' 的快捷方式
        self.p = self.params

    @abstractmethod
    def check(self, data) -> str:
        """
        【核心】对指定的数据执行风控检查。
        如果策略持有该 'data' 的仓位，此方法将在每个 'next' bar 被调用。

        :param data: 当前K线的数据 feed (e.g., self.datas[0])
        :return: 'SELL' 表示把该 feed 的已确认仓位打回零。多头卖平，空头买平。
                 None 或其他值表示不动作。股票百分比样例不衡量期权权利金和乘数。
        """
        pass

    def notify_order(self, order):
        """
        订单状态通知。
        风控模块可以在此跟踪订单状态。
        """
        pass

    def notify_trade(self, trade):
        """
        交易成交通知。
        风控模块可以在此跟踪持仓成本。
        """
        pass