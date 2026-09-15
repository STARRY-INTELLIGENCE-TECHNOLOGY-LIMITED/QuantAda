from backtrader.indicators import MACD, CrossOver

from .base_strategy import BaseStrategy


class SampleMacdCrossStrategy(BaseStrategy):
    """MACD 金叉死叉样本 SampleMacdCrossStrategy，仅用于本地回测和优化。

    它依赖 Backtrader 的指标工厂、`broker.dataclose` 和 `broker.buy()`。
    实盘 `BaseLiveBroker` 不提供这些接口，请不要用 `--connect` 启动本策略。
    需要同一套代码同时服务回测和实盘时，请使用 `sample_auto_rebalance_strategy`
    或 `order_target_percent` / `order_target_value`。

    复制执行。`--params` 与类默认参数一致。

    python run.py sample_macd_cross_strategy --symbols SHSE.600519 \\
      --params "{'exitbars': 5}"
    """
    params = {
        'exitbars': 5,
    }

    # !!!注意，初始化方法只会执行一次，如果将计算逻辑写到这里实盘会有不重新计算的风险，请抽象计算方法并放置于next中!!!
    def init(self):
        """
        初始化指标。
        注意这里我们通过 self.broker.indicators 来获取指标计算器。
        回测时，broker就是backtrader的strategy实例，它有indicators属性。
        实盘时，broker是我们的适配器，我们也需要为它实现indicators属性。
        """
        self.dataclose = self.broker.dataclose

        # 使用broker提供的指标工厂来创建指标
        macd_indicator = MACD()
        self.macd = macd_indicator.macd
        self.signal = macd_indicator.signal
        self.histo = self.macd - self.signal
        self.crossover = CrossOver(self.macd, self.signal)

        self.order = None

    def next(self):
        if self.order:
            return

        if not self.broker.position:
            if self.crossover[0] > 0:
                size_to_buy = 100  # 或根据资金动态计算
                self.log(f'BUY CREATE, Buying {size_to_buy} shares, Price: {self.dataclose[0]:.2f}')
                self.order = self.broker.buy(size=size_to_buy)
        else:
            if self.crossover[0] < 0:
                self.log(f'SELL SIGNAL: Closing position.')
                self.order = self.broker.order_target_percent(target=0.0)

    def notify_order(self, order):
        super().notify_order(order)
        if not order.is_pending():
            self.order = None
