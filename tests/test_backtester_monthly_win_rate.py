import pandas as pd

import config
from backtest.backtester import Backtester
from strategies.base_strategy import BaseStrategy


class _BuyAndHoldStrategy(BaseStrategy):
    def init(self):
        self.submitted = False

    def next(self):
        if self.submitted:
            return
        self.broker.order_target_percent(data=self.broker.datas[0], target=1.0)
        self.submitted = True


def test_get_performance_metrics_includes_monthly_win_rate(monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 1)

    idx = pd.to_datetime(
        [
            "2024-01-31",
            "2024-02-29",
            "2024-03-31",
            "2024-04-30",
        ]
    )
    df = pd.DataFrame(
        {
            "open": [100.0, 110.0, 105.0, 120.0],
            "high": [100.0, 110.0, 105.0, 120.0],
            "low": [100.0, 110.0, 105.0, 120.0],
            "close": [100.0, 110.0, 105.0, 120.0],
            "volume": [1.0, 1.0, 1.0, 1.0],
        },
        index=idx,
    )

    bt = Backtester(
        datas={"AAA": df},
        strategy_class=_BuyAndHoldStrategy,
        cash=1000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    metrics = bt.get_performance_metrics()

    assert "monthly_win_rate" in metrics
    assert metrics["monthly_win_rate"] == 2.0 / 3.0


class _WinThenHoldStrategy(BaseStrategy):
    def init(self):
        self.bar = 0

    def next(self):
        target = {0: 1.0, 1: 0.0, 2: 1.0}.get(self.bar)
        if target is not None:
            self.broker.order_target_percent(data=self.broker.datas[0], target=target)
        self.bar += 1


def test_win_rate_counts_closed_trades_only(monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 1)
    idx = pd.bdate_range("2024-01-02", periods=5)
    prices = [100.0, 110.0, 110.0, 120.0, 130.0]
    df = pd.DataFrame({key: prices for key in ("open", "high", "low", "close")}, index=idx)
    df["volume"] = 1.0
    bt = Backtester(datas={"AAA": df}, strategy_class=_WinThenHoldStrategy, cash=1000.0,
                    commission=0.0, slippage=0.0, enable_plot=False, verbose=False)
    bt.run()
    metrics = bt.get_performance_metrics()
    # 一笔已平仓盈利 + 一笔未平仓：胜率为 100%，未平仓交易不进入分母。
    assert metrics["total_trades"] == 2
    assert metrics["win_rate"] == 100.0
