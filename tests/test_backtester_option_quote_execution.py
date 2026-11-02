import pandas as pd

from backtest.backtester import Backtester
from common.options.risk import OptionRiskLeg
from strategies.base_strategy import BaseStrategy


def _zero_ohlc_option():
    dates = pd.bdate_range("2026-09-14", periods=3)
    frame = pd.DataFrame(
        {
            "open": [0.0, 0.0, 0.0],
            "high": [0.0, 0.0, 0.0],
            "low": [0.0, 0.0, 0.0],
            "close": [0.0, 0.0, 0.0],
            "volume": [0, 0, 0],
            "bid": [2.0, 1.5, 1.0],
            "ask": [2.2, 1.7, 1.2],
            "option_type": ["PUT"] * 3,
            "strike": [100.0] * 3,
            "expiry": ["2026-10-16"] * 3,
            "contract_multiplier": [100.0] * 3,
        },
        index=dates,
    )
    return frame


class _QuotedOptionRoundTrip(BaseStrategy):
    def init(self):
        self.phase = 0
        self.open_order = None
        self.close_order = None

    def next(self):
        data = self.broker.datas[0]
        if self.phase == 0:
            leg = OptionRiskLeg(
                data._name,
                "US.AAPL",
                "PUT",
                -1.0,
                100.0,
                2.0,
                105.0,
                100.0,
                expiry="2026-10-16",
            )
            self.open_order = self.broker.submit_option_order(
                data,
                1,
                "SELL_TO_OPEN",
                price=2.0,
                risk_leg=leg,
            )
            self.phase = 1
        elif self.phase == 1:
            self.close_order = self.broker.submit_option_order(
                data,
                1,
                "BUY_TO_CLOSE",
                price=1.0,
            )
            self.phase = 2


def test_option_market_orders_fill_at_explicit_nbbo_when_eod_ohlc_is_zero():
    engine = Backtester(
        {"US.AAPL261016P00100000": _zero_ohlc_option()},
        _QuotedOptionRoundTrip,
        cash=20_000,
        commission=0,
        slippage=0,
        enable_plot=False,
        verbose=False,
    )

    result = engine.run()[0]

    assert result.strategy.open_order.executed.price == 2.0
    assert result.strategy.close_order.executed.price == 1.0
    assert result.getposition(result.datas[0]).size == 0
    assert engine.cerebro.broker.getvalue() == 20_100.0
    assert engine.get_closed_trades()[0]["pnl"] == 100.0


class _RejectUnpricedLongPut(BaseStrategy):
    def init(self):
        self.order = None

    def next(self):
        data = self.broker.datas[0]
        if self.order is not None:
            return
        self.order = self.broker.submit_option_order(
            data,
            1,
            "BUY_TO_OPEN",
        )


class _RejectUnpricedShortPut(BaseStrategy):
    def init(self):
        self.order = None

    def next(self):
        data = self.broker.datas[0]
        if self.order is not None:
            return
        self.order = self.broker.submit_option_order(
            data,
            1,
            "SELL_TO_OPEN",
            risk_leg=OptionRiskLeg(
                data._name,
                "US.AAPL",
                "PUT",
                -1.0,
                100.0,
                0.0,
                105.0,
                100.0,
                expiry="2026-10-16",
            ),
        )


def test_backtest_long_option_without_an_execution_quote_fails_closed():
    engine = Backtester(
        {"US.AAPL261016P00100000": _zero_ohlc_option()},
        _RejectUnpricedLongPut,
        cash=20_000,
        commission=0,
        slippage=0,
        enable_plot=False,
        verbose=False,
    )

    result = engine.run()[0]

    assert result.strategy.order is None
    assert result._last_order_target_skip_reason == "option_quote_unavailable"
    assert result.getposition(result.datas[0]).size == 0


def test_backtest_short_option_without_an_execution_quote_fails_closed():
    engine = Backtester(
        {"US.AAPL261016P00100000": _zero_ohlc_option()},
        _RejectUnpricedShortPut,
        cash=20_000,
        commission=0,
        slippage=0,
        enable_plot=False,
        verbose=False,
    )

    result = engine.run()[0]

    assert result.strategy.order is None
    assert result._last_order_target_skip_reason == "option_quote_unavailable"
    assert result.getposition(result.datas[0]).size == 0
