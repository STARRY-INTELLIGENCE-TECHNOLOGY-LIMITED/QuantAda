"""夏普年化因子必须与收益采样周期一致。"""

import backtrader as bt
import pytest

import config
from backtest.backtester import Backtester
from strategies.base_strategy import BaseStrategy


class _Noop(BaseStrategy):
    def init(self):
        pass

    def next(self):
        pass


def _sharpe_kwargs(timeframe, compression=1):
    engine = Backtester({}, _Noop, timeframe=timeframe, compression=compression,
                        enable_plot=False, verbose=False)
    for analyzer, _args, kwargs in engine.cerebro.analyzers:
        if analyzer is bt.analyzers.SharpeRatio:
            return kwargs
    raise AssertionError("SharpeRatio analyzer missing")


@pytest.mark.parametrize(
    "timeframe, compression, expected_timeframe, expected_factor",
    [
        ("Days", 1, bt.TimeFrame.Days, float(config.ANNUAL_FACTOR)),
        ("Weeks", 1, bt.TimeFrame.Weeks, 52.0),
        ("Months", 1, bt.TimeFrame.Months, 12.0),
        ("Weeks", 2, bt.TimeFrame.Weeks, 26.0),
        # 日内 K 线先聚合成日收益，再按日年化。
        ("Minutes", 15, bt.TimeFrame.Days, float(config.ANNUAL_FACTOR)),
    ],
)
def test_sharpe_factor_matches_bar_timeframe(timeframe, compression, expected_timeframe, expected_factor):
    kwargs = _sharpe_kwargs(timeframe, compression)
    assert kwargs["timeframe"] == expected_timeframe
    assert kwargs["compression"] == 1
    assert kwargs["factor"] == pytest.approx(expected_factor)
