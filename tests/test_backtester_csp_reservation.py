import pandas as pd

from backtest.backtester import Backtester
from common.options.risk import OptionRiskLeg
from strategies.base_strategy import BaseStrategy


def _option_frame(strike):
    index = pd.date_range("2026-01-02", periods=2, freq="D")
    return pd.DataFrame(
        {
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [100, 100],
            "option_type": ["PUT", "PUT"],
            "strike": [strike, strike],
            "expiry": ["2026-02-20", "2026-02-20"],
            "contract_multiplier": [100.0, 100.0],
        },
        index=index,
    )


class _TwoCspSameBar(BaseStrategy):
    params = {"submitted": False}

    def init(self):
        self.results = []

    def next(self):
        if self.p.submitted:
            return
        for data in self.broker.datas:
            leg = OptionRiskLeg(
                data._name,
                data._name.split("261")[0],
                "PUT",
                -1,
                float(data.p.dataname["strike"].iloc[0]),
                1.0,
                100.0,
                100.0,
            )
            self.results.append(
                self.broker.submit_option_order(
                    data,
                    1,
                    "SELL_TO_OPEN",
                    price=1.0,
                    risk_leg=leg,
                )
            )
        self.p.submitted = True


class _PositiveRiskLegProbe(BaseStrategy):
    def init(self):
        self.done = False
        self.result = None

    def next(self):
        if self.done:
            return
        data = self.broker.datas[0]
        leg = OptionRiskLeg(
            data._name,
            "AAPL",
            "PUT",
            1,
            90.0,
            1.0,
            100.0,
            100.0,
        )
        self.result = self.broker.submit_option_order(
            data, 1, "SELL_TO_OPEN", price=1.0, risk_leg=leg
        )
        self.done = True


def test_same_bar_csp_orders_reserve_full_assignment_cash():
    backtester = Backtester(
        datas={
            "US.AAPL260220P00090000": _option_frame(90.0),
            "US.AAPL260220P00100000": _option_frame(100.0),
        },
        strategy_class=_TwoCspSameBar,
        cash=10_000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    result = backtester.run()[0]

    assert sum(item is not None for item in result.strategy.results) == 1
    assert result._last_order_target_skip_reason == "csp_assignment_cash_insufficient"
    short_sizes = [result.getposition(data).size for data in result.datas]
    assert short_sizes.count(-1) == 1


def test_csp_uncommitted_cash_subtracts_short_position_obligation():
    class Probe(BaseStrategy):
        def init(self):
            self.cash_after = None

        def next(self):
            if self.cash_after is not None:
                return
            data = self.broker.datas[0]
            self.broker.submit_option_order(
                data,
                1,
                "SELL_TO_OPEN",
                price=1.0,
                risk_leg=OptionRiskLeg(
                    data._name,
                    "AAPL",
                    "PUT",
                    -1,
                    90.0,
                    1.0,
                    100.0,
                    100.0,
                ),
            )
            self.cash_after = self.broker.get_csp_uncommitted_cash()

    backtester = Backtester(
        datas={"US.AAPL260220P00090000": _option_frame(90.0)},
        strategy_class=Probe,
        cash=10_000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    result = backtester.run()[0]
    # 10000 - 9000 reservation; premium is not collateral.
    assert result.strategy.cash_after == 1000.0


def test_short_option_risk_leg_must_be_negative():
    backtester = Backtester(
        datas={"US.AAPL260220P00090000": _option_frame(90.0)},
        strategy_class=_PositiveRiskLegProbe,
        cash=10_000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    result = backtester.run()[0]
    assert result.strategy.result is None
    assert result.getposition(result.datas[0]).size == 0
