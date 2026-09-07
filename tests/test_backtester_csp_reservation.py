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


def test_cash_secured_allocation_rejects_insufficient_capital():
    class Probe(BaseStrategy):
        def init(self):
            self.result = "unset"

        def next(self):
            if self.result != "unset":
                return
            data = self.broker.datas[0]
            self.result = self.broker.submit_option_order(
                data,
                1,
                "SELL_TO_OPEN",
                price=1.0,
                risk_leg=OptionRiskLeg(
                    data._name,
                    "SPY",
                    "PUT",
                    -1,
                    101.0,
                    1.0,
                    500.0,
                    100.0,
                ),
            )

    backtester = Backtester(
        datas={"US.SPY260220P00101000": _option_frame(101.0)},
        strategy_class=Probe,
        cash=10_000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    result = backtester.run()[0]

    assert result.strategy.result is None
    assert result._last_order_target_skip_reason == "csp_assignment_cash_insufficient"
    assert result.getposition(result.datas[0]).size == 0


def test_cash_secured_reservation_blocks_other_equity_buys():
    class Probe(BaseStrategy):
        def init(self):
            self.done = False
            self.csp_order = None
            self.equity_order = None

        def next(self):
            if self.done:
                return
            option, equity = self.broker.datas
            self.csp_order = self.broker.submit_option_order(
                option,
                1,
                "SELL_TO_OPEN",
                price=1.0,
                risk_leg=OptionRiskLeg(
                    option._name, "SPY", "PUT", -1, 90.0, 1.0, 500.0, 100.0
                ),
            )
            self.equity_order = self.broker.order_target_value(equity, 2_000.0)
            self.done = True

    equity = _option_frame(1.0).drop(columns=["option_type", "strike", "expiry", "contract_multiplier"])
    backtester = Backtester(
        datas={
            "US.SPY260220P00090000": _option_frame(90.0),
            "US.SPY": equity,
        },
        strategy_class=Probe,
        cash=10_000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    result = backtester.run()[0]

    assert result.strategy.csp_order is not None
    assert result.strategy.equity_order is not None
    # 现金 10,000 中有 9,000 被 Short Put 指派义务隔离，股票最多只能
    # 使用卖权金后的约 1,100，而不能按 2,000 的目标市值全额买入。
    assert result.getposition(result.datas[1]).size <= 1_100


def test_backtest_supports_covered_call_sell_to_open():
    class Probe(BaseStrategy):
        def init(self):
            self.phase = 0
            self.covered_call_order = None

        def next(self):
            option, underlying = self.broker.datas
            if self.phase == 0:
                self.broker.buy(data=underlying, size=100)
                self.phase = 1
                return
            if self.phase == 1:
                position = self.broker.getposition(underlying)
                self.covered_call_order = self.broker.submit_option_order(
                    option,
                    1,
                    "SELL_TO_OPEN",
                    price=1.0,
                    risk_leg=OptionRiskLeg(
                        option._name,
                        underlying._name,
                        "CALL",
                        -1,
                        100.0,
                        1.0,
                        100.0,
                        100.0,
                    ),
                    underlying_positions={underlying._name: position.size},
                )
                self.phase = 2

    option = _option_frame(100.0).copy()
    option["option_type"] = "CALL"
    option["strike"] = 100.0
    option["close"] = 1.0
    option["contract_multiplier"] = 100.0
    underlying = option.drop(columns=["option_type", "strike", "expiry", "contract_multiplier"])
    underlying["close"] = 100.0

    backtester = Backtester(
        datas={
            "US.SPY260220C00100000": option,
            "US.SPY": underlying,
        },
        strategy_class=Probe,
        cash=20_000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    result = backtester.run()[0]

    assert result.strategy.covered_call_order is not None


def test_backtest_supports_long_option_buy_to_open():
    class Probe(BaseStrategy):
        def init(self):
            self.order = None

        def next(self):
            if self.order is None:
                self.order = self.broker.submit_option_order(
                    self.broker.datas[0], 1, "BUY_TO_OPEN", price=1.0
                )

    option = _option_frame(100.0).copy()
    option["option_type"] = "CALL"
    option["contract_multiplier"] = 100.0
    backtester = Backtester(
        datas={"US.SPY260220C00100000": option},
        strategy_class=Probe,
        cash=20_000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    result = backtester.run()[0]

    assert result.strategy.order is not None
    assert result.getposition(result.datas[0]).size > 0


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
