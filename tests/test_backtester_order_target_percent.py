import pandas as pd
import pytest

import config
from backtest.backtester import Backtester
from strategies.base_strategy import BaseStrategy


def _make_flat_df():
    idx = pd.to_datetime(['2024-01-01', '2024-01-02', '2024-01-03'])
    return pd.DataFrame(
        {
            'open': [100.0, 100.0, 100.0],
            'high': [100.0, 100.0, 100.0],
            'low': [100.0, 100.0, 100.0],
            'close': [100.0, 100.0, 100.0],
            'volume': [1.0, 1.0, 1.0],
        },
        index=idx,
    )


def _make_constant_price_df(price, bars=4):
    idx = pd.date_range('2024-01-01', periods=bars, freq='D')
    return pd.DataFrame(
        {
            'open': [float(price)] * bars,
            'high': [float(price)] * bars,
            'low': [float(price)] * bars,
            'close': [float(price)] * bars,
            'volume': [1.0] * bars,
        },
        index=idx,
    )


class _TwoSymbolPercentBuyStrategy(BaseStrategy):
    def init(self):
        self.submitted = False
        self.statuses = []

    def next(self):
        if self.submitted:
            return
        for d in self.broker.datas:
            self.broker.order_target_percent(data=d, target=0.6)
        self.submitted = True

    def notify_order(self, order):
        self.statuses.append(order.getstatusname())


class _WarmupProbeStrategy(BaseStrategy):
    seen_dates = []
    dataname_first = None

    def init(self):
        df = self.broker.datas[0].p.dataname
        self.__class__.dataname_first = df.index.min()

    def next(self):
        self.__class__.seen_dates.append(self.broker.datas[0].datetime.datetime(0))


class _BuyThenSellProbeStrategy(BaseStrategy):
    def init(self):
        self.phase = 0

    def next(self):
        data = self.broker.datas[0]
        if self.phase == 0:
            self.broker.order_target_value(data=data, target=100.0)
            self.phase = 1
            return

        if self.phase == 1 and self.broker.getposition(data).size > 0:
            self.broker.order_target_value(data=data, target=0.0)
            self.phase = 2


class _FractionalBuyThenSellProbeStrategy(BaseStrategy):
    completed_sizes = []

    def init(self):
        self.phase = 0

    def next(self):
        data = self.broker.datas[0]
        if self.phase == 0:
            order = self.broker.order_target_value(data=data, target=25.0)
            assert order is not None
            self.phase = 1
        elif self.phase == 1 and self.broker.getposition(data).size > 0:
            self.broker.order_target_value(data=data, target=0.0)
            self.phase = 2

    def notify_order(self, order):
        if order.is_completed():
            self.__class__.completed_sizes.append(abs(order.executed.size))


class _InsufficientCashBuyProbeStrategy(BaseStrategy):
    skip_reason = None

    def init(self):
        self.submitted = False

    def next(self):
        if self.submitted:
            return
        order = self.broker.order_target_value(data=self.broker.datas[0], target=200.0)
        assert order is None
        self.__class__.skip_reason = getattr(self.broker, "_last_order_target_skip_reason", None)
        self.submitted = True


class _ExactOneShareBuyProbeStrategy(BaseStrategy):
    statuses = []
    final_size = 0

    def init(self):
        self.submitted = False

    def next(self):
        if self.submitted:
            return
        order = self.broker.order_target_value(data=self.broker.datas[0], target=100.0)
        assert order is not None
        self.submitted = True

    def notify_order(self, order):
        self.statuses.append(order.getstatusname())
        if order.is_completed():
            self.__class__.final_size = order.executed.size


class _TinySellDoesNotFreeCashProbeStrategy(BaseStrategy):
    buy_order_submitted = None

    def init(self):
        self.step = 0

    def next(self):
        if self.step == 0:
            order = self.broker.order_target_value(data=self.broker.datas[0], target=100.0)
            assert order is not None
            self.step += 1
            return

        if self.step == 1:
            # 当前 1 股，目标 99.5 美元，只需卖 0.005 股；LOT_SIZE=1 时不应提交卖单，也不应释放现金。
            sell_order = self.broker.order_target_value(data=self.broker.datas[0], target=99.5)
            assert sell_order is None
            buy_order = self.broker.order_target_value(data=self.broker.datas[1], target=1.0)
            self.__class__.buy_order_submitted = buy_order is not None
            self.step += 1


class _SellThenTopKRebalanceProbeStrategy(BaseStrategy):
    params = {
        'initial_target': 0.0,
    }
    statuses = []

    def init(self):
        self.phase = 0

    def next(self):
        if self.phase == 0:
            order = self.broker.order_target_value(
                data=self.broker.datas[0],
                target=self.p.initial_target,
            )
            assert order is not None
            self.phase = 1
            return

        if self.phase == 1:
            if self.broker.getposition(self.broker.datas[0]).size <= 0:
                return
            self.execute_rebalance(
                target_symbols=[self.broker.datas[1], self.broker.datas[2]],
                top_k=2,
                rebalance_threshold=0.0,
                rebalance_when='next',
            )
            self.phase = 2

    def notify_order(self, order):
        self.statuses.append(order.getstatusname())


def test_order_target_percent_tracks_virtual_spent_cash_with_multi_symbol_buy(monkeypatch):
    """
    回归：同一 Bar 多标的连续 order_target_percent 买入时，必须扣减本轮已花费现金。
    否则第二笔订单会在回测柜台阶段触发 Margin。
    """
    monkeypatch.setattr(config, 'LOT_SIZE', 1)

    datas = {
        'AAA': _make_flat_df(),
        'BBB': _make_flat_df(),
    }

    bt = Backtester(
        datas=datas,
        strategy_class=_TwoSymbolPercentBuyStrategy,
        cash=1000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    wrapper = bt.results[0]
    statuses = wrapper.strategy.statuses
    assert 'Margin' not in statuses, "同 Bar 多标的买入应避免产生 Margin 拒单。"

    final_sizes = {d._name: wrapper.getposition(d).size for d in wrapper.datas}
    assert final_sizes == {'AAA': 6, 'BBB': 4}, "第二笔买单应按剩余现金自动缩量成交，而非被拒单。"


def test_backtester_supports_fractional_crypto_lot_size(monkeypatch):
    monkeypatch.setattr(config, 'LOT_SIZE', 0.0001)
    _FractionalBuyThenSellProbeStrategy.completed_sizes = []

    bt = Backtester(
        datas={'CRYPTO.BTC.USD': _make_constant_price_df(100.0)},
        strategy_class=_FractionalBuyThenSellProbeStrategy,
        cash=100.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    wrapper = bt.results[0]
    assert _FractionalBuyThenSellProbeStrategy.completed_sizes == pytest.approx([0.25, 0.25])
    assert wrapper.getposition(wrapper.datas[0]).size == pytest.approx(0.0)


def test_backtester_keeps_full_dataname_while_feed_starts_at_start_date():
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    df = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1.0,
        },
        index=idx,
    )
    _WarmupProbeStrategy.seen_dates = []
    _WarmupProbeStrategy.dataname_first = None

    bt = Backtester(
        datas={"AAA": df},
        strategy_class=_WarmupProbeStrategy,
        start_date="20240106",
        end_date="20240110",
        cash=1000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    assert _WarmupProbeStrategy.dataname_first == pd.Timestamp("2024-01-01")
    assert [d.strftime("%Y%m%d") for d in _WarmupProbeStrategy.seen_dates] == [
        "20240106",
        "20240107",
        "20240108",
        "20240109",
        "20240110",
    ]
    assert bt.get_performance_metrics()["start_date"] == pd.Timestamp("2024-01-06").to_pydatetime()


def test_backtester_prints_trade_attribution_before_performance_metrics(monkeypatch, capsys):
    monkeypatch.setattr(config, 'LOT_SIZE', 1)

    idx = pd.to_datetime(['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'])
    df = pd.DataFrame(
        {
            'open': [100.0, 100.0, 100.0, 100.0],
            'high': [100.0, 100.0, 100.0, 100.0],
            'low': [100.0, 90.0, 95.0, 100.0],
            'close': [100.0, 110.0, 120.0, 120.0],
            'volume': [1.0, 1.0, 1.0, 1.0],
        },
        index=idx,
    )

    bt = Backtester(
        datas={'AAA': df},
        strategy_class=_BuyThenSellProbeStrategy,
        cash=1000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    closed_trades = bt.get_closed_trades()
    assert len(closed_trades) == 1
    assert closed_trades[0]["symbol"] == "AAA"
    assert closed_trades[0]["lowest_price_during_trade"] == 90.0

    bt.display_results()
    out = capsys.readouterr().out
    assert "Trade Attribution by Symbol" in out
    assert "Winning Trade MAE Statistics" in out
    assert out.index("Trade Attribution by Symbol") < out.index("Backtest Performance Metrics")


def test_backtester_results_report_keeps_attribution_above_performance_metrics():
    from backtest.reporting import format_backtest_results_report

    metrics = {
        "start_date": pd.Timestamp("2024-01-01"),
        "end_date": pd.Timestamp("2024-01-31"),
        "initial_portfolio": 1000.0,
        "final_portfolio": 1100.0,
        "total_return": 0.1,
        "annual_return": 1.2,
        "sharpe_ratio": 2.0,
        "max_drawdown": -0.05,
        "calmar_ratio": 24.0,
        "total_trades": 3,
        "win_rate": 66.666,
        "profit_factor": 2.5,
        "pnl_ratio": 1.8,
    }

    report = format_backtest_results_report(metrics, attribution_report="Trade Attribution by Symbol")

    assert "Trade Attribution by Symbol" in report
    assert "Backtest Performance Metrics" in report
    assert report.index("Trade Attribution by Symbol") < report.index("Backtest Performance Metrics")
    assert " Total Return:         10.00%" in report


def test_order_target_value_marks_insufficient_cash_for_min_lot(monkeypatch):
    monkeypatch.setattr(config, 'LOT_SIZE', 1)
    _InsufficientCashBuyProbeStrategy.skip_reason = None

    idx = pd.to_datetime(['2024-01-01', '2024-01-02'])
    df = pd.DataFrame(
        {
            'open': [100.0, 100.0],
            'high': [100.0, 100.0],
            'low': [100.0, 100.0],
            'close': [100.0, 100.0],
            'volume': [1.0, 1.0],
        },
        index=idx,
    )

    bt = Backtester(
        datas={'AAA': df},
        strategy_class=_InsufficientCashBuyProbeStrategy,
        cash=10.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    assert _InsufficientCashBuyProbeStrategy.skip_reason == 'insufficient_cash_for_min_lot'


def test_order_target_value_submits_exact_one_share_when_cash_allows(monkeypatch):
    monkeypatch.setattr(config, 'LOT_SIZE', 1)
    _ExactOneShareBuyProbeStrategy.statuses = []
    _ExactOneShareBuyProbeStrategy.final_size = 0

    idx = pd.to_datetime(['2024-01-01', '2024-01-02'])
    df = pd.DataFrame(
        {
            'open': [100.0, 100.0],
            'high': [100.0, 100.0],
            'low': [100.0, 100.0],
            'close': [100.0, 100.0],
            'volume': [1.0, 1.0],
        },
        index=idx,
    )

    bt = Backtester(
        datas={'AAA': df},
        strategy_class=_ExactOneShareBuyProbeStrategy,
        cash=100.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    assert 'Margin' not in _ExactOneShareBuyProbeStrategy.statuses
    assert _ExactOneShareBuyProbeStrategy.final_size == 1


def test_tiny_sell_skip_does_not_free_cash_for_later_buy(monkeypatch):
    monkeypatch.setattr(config, 'LOT_SIZE', 1)
    _TinySellDoesNotFreeCashProbeStrategy.buy_order_submitted = None

    idx = pd.to_datetime(['2024-01-01', '2024-01-02', '2024-01-03'])
    df = pd.DataFrame(
        {
            'open': [100.0, 100.0, 100.0],
            'high': [100.0, 100.0, 100.0],
            'low': [100.0, 100.0, 100.0],
            'close': [100.0, 100.0, 100.0],
            'volume': [1.0, 1.0, 1.0],
        },
        index=idx,
    )
    cash_like_df = pd.DataFrame(
        {
            'open': [1.0, 1.0, 1.0],
            'high': [1.0, 1.0, 1.0],
            'low': [1.0, 1.0, 1.0],
            'close': [1.0, 1.0, 1.0],
            'volume': [1.0, 1.0, 1.0],
        },
        index=idx,
    )

    bt = Backtester(
        datas={'AAA': df, 'BBB': cash_like_df},
        strategy_class=_TinySellDoesNotFreeCashProbeStrategy,
        cash=100.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    assert _TinySellDoesNotFreeCashProbeStrategy.buy_order_submitted is False


def test_a_share_sell_then_topk_rebalance_buys_rounded_lots_without_margin(monkeypatch):
    monkeypatch.setattr(config, 'LOT_SIZE', 100)
    _SellThenTopKRebalanceProbeStrategy.statuses = []

    bt = Backtester(
        datas={
            'AAA': _make_constant_price_df(10.0),
            'BBB': _make_constant_price_df(10.0),
            'CCC': _make_constant_price_df(10.0),
        },
        strategy_class=_SellThenTopKRebalanceProbeStrategy,
        params={'initial_target': 11000.0},
        cash=11000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    wrapper = bt.results[0]
    final_sizes = {data._name: wrapper.getposition(data).size for data in wrapper.datas}

    assert 'Margin' not in _SellThenTopKRebalanceProbeStrategy.statuses
    assert final_sizes == {'AAA': 0, 'BBB': 500, 'CCC': 500}


def test_us_sell_then_topk_rebalance_buys_integer_shares_without_margin(monkeypatch):
    monkeypatch.setattr(config, 'LOT_SIZE', 1)
    _SellThenTopKRebalanceProbeStrategy.statuses = []

    bt = Backtester(
        datas={
            'AAA': _make_constant_price_df(100.0),
            'BBB': _make_constant_price_df(100.0),
            'CCC': _make_constant_price_df(100.0),
        },
        strategy_class=_SellThenTopKRebalanceProbeStrategy,
        params={'initial_target': 1000.0},
        cash=1000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    bt.run()

    wrapper = bt.results[0]
    final_sizes = {data._name: wrapper.getposition(data).size for data in wrapper.datas}

    assert 'Margin' not in _SellThenTopKRebalanceProbeStrategy.statuses
    assert final_sizes == {'AAA': 0, 'BBB': 5, 'CCC': 5}
