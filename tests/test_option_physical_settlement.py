"""验证物理交割只在显式声明的日线回测中生效，否则期权到期默认现金结算。"""

import pandas as pd
import pytest

import config
from backtest.backtester import Backtester
from common.options.risk import OptionRiskLeg
from strategies.base_strategy import BaseStrategy


class HoldPut(BaseStrategy):
    option_settlement = "physical"

    def init(self):
        pass

    def next(self):
        stock, option = self.broker.datas
        if len(stock) == 1:
            leg = OptionRiskLeg(option._name, stock._name, "PUT", -1, 100, 2,
                                float(stock.close[0]), 100, expiry="2026-09-04")
            self.broker.submit_option_order(option, 1, "SELL_TO_OPEN", price=2,
                                             risk_leg=leg, allow_sell_to_open=True)


def run_expiry(monkeypatch, spot=90, strategy=HoldPut, end="20260908", commission=0, missing_expiry=False,
               option_symbol="US.SPY260904P00100000"):
    monkeypatch.setattr(config, "PRINT_PLAN", False)
    monkeypatch.setattr(config, "LOG", False)
    dates = pd.bdate_range("2026-09-01", "2026-09-08")
    prices = [102, 102, 102, spot, spot + 10, spot + 10]
    stock = pd.DataFrame({key: prices for key in ("open", "high", "low", "close")}, index=dates)
    stock["volume"] = 1000
    if missing_expiry:
        stock = stock.drop(pd.Timestamp("2026-09-04"))
    option = pd.DataFrame({key: [2.0] * 4 for key in ("open", "high", "low", "close")}, index=dates[:4])
    option["volume"], option["contract_multiplier"] = 100, 100
    engine = Backtester({"US.SPY": stock, option_symbol: option}, strategy,
                        start_date="20260901", end_date=end, cash=100000,
                        commission=commission, slippage=0, enable_plot=False, verbose=False)
    engine.run()
    return engine


def test_itm_put_expiry_transfers_exact_cash_and_stock_once(monkeypatch):
    engine = run_expiry(monkeypatch)
    broker = engine.cerebro.broker
    events = broker.option_settlement_events
    assert len(events) == 1
    assert events[0]["event"] == "ASSIGNED_PUT"
    assert events[0]["cash_delta"] == -10000
    assert broker.getcash() == pytest.approx(90200)
    assert broker.getvalue() == pytest.approx(100200)
    # 指派亏损归属期权交易：权利金 200 - 内在价值 10*100；正股按结算价 90 入账。
    option_trade = engine.get_closed_trades()[0]
    assert option_trade["pnl"] == pytest.approx(-800)
    assert option_trade["settlement_event"] == "ASSIGNED_PUT"
    stock = next(data for data in engine.cerebro.datas if data._name == "US.SPY")
    assert broker.getposition(stock).size == 100
    assert broker.getposition(stock).price == pytest.approx(90)


def test_otm_put_expiry_releases_cash_without_stock(monkeypatch):
    engine = run_expiry(monkeypatch, spot=110)
    broker = engine.cerebro.broker
    assert broker.option_settlement_events[0]["event"] == "EXPIRED_OTM"
    assert broker.getcash() == pytest.approx(100200)
    assert all(p.size == 0 for p in broker.positions.values())


def test_expiry_on_final_bar_still_settles_and_values_stock(monkeypatch):
    engine = run_expiry(monkeypatch, end="20260904")
    assert engine.cerebro.broker.getcash() == pytest.approx(90200)
    assert engine.cerebro.broker.getvalue() == pytest.approx(99200)


def test_expiry_transfer_is_not_a_second_commission_charged_stock_trade(monkeypatch):
    engine = run_expiry(monkeypatch, commission=0.001)
    assert engine.cerebro.broker.getcash() == pytest.approx(90199.8)


def test_missing_expiry_price_fails_instead_of_using_next_days_price(monkeypatch):
    with pytest.raises(ValueError, match="expiry-day underlying close"):
        run_expiry(monkeypatch, missing_expiry=True)


class CashPut(HoldPut):
    option_settlement = None


def test_default_cash_settles_itm_short_put_into_closed_trade(monkeypatch):
    engine = run_expiry(monkeypatch, strategy=CashPut)
    broker = engine.cerebro.broker
    assert broker.option_expiry_settlement == "cash"
    event = broker.option_settlement_events[0]
    assert (event["event"], event["settlement"], event["underlying_delta"]) == ("ASSIGNED_CASH", "cash", 0)
    # 现金结算：权利金 200 - 内在价值 (100-90)*100，不交割正股。
    assert broker.getcash() == pytest.approx(99200)
    assert broker.getvalue() == pytest.approx(99200)
    assert all(p.size == 0 for p in broker.positions.values())
    trade = engine.get_closed_trades()[0]
    assert trade["pnl"] == pytest.approx(-800)
    assert trade["settlement_event"] == "ASSIGNED_CASH"


def test_default_cash_settles_otm_short_put_as_full_premium_win(monkeypatch):
    engine = run_expiry(monkeypatch, spot=110, strategy=CashPut)
    broker = engine.cerebro.broker
    assert broker.getcash() == pytest.approx(100200)
    assert all(p.size == 0 for p in broker.positions.values())
    trade = engine.get_closed_trades()[0]
    assert trade["pnl"] == pytest.approx(200)
    assert trade["settlement_event"] == "EXPIRED_OTM"


class CashLongCall(BaseStrategy):
    def init(self):
        pass

    def next(self):
        stock, option = self.broker.datas
        if len(stock) == 1:
            self.broker.submit_option_order(option, 1, "BUY_TO_OPEN", price=2)


def test_default_cash_settles_itm_long_call_gain(monkeypatch):
    engine = run_expiry(monkeypatch, spot=110, strategy=CashLongCall, option_symbol="US.SPY260904C00100000")
    broker = engine.cerebro.broker
    assert broker.getcash() == pytest.approx(100800)
    assert all(p.size == 0 for p in broker.positions.values())
    trade = engine.get_closed_trades()[0]
    assert trade["pnl"] == pytest.approx(800)
    assert trade["settlement_event"] == "EXERCISED_CASH"


def test_cash_settlement_without_underlying_feed_keeps_marked_position(monkeypatch):
    class OptionOnly(BaseStrategy):
        def init(self):
            pass

        def next(self):
            option = self.broker.datas[0]
            if len(option) == 1:
                self.broker.submit_option_order(option, 1, "BUY_TO_OPEN", price=2)

    monkeypatch.setattr(config, "PRINT_PLAN", False)
    monkeypatch.setattr(config, "LOG", False)
    dates = pd.bdate_range("2026-09-01", "2026-09-08")
    option = pd.DataFrame({key: [2.0] * len(dates) for key in ("open", "high", "low", "close")}, index=dates)
    option["volume"], option["contract_multiplier"] = 100, 100
    engine = Backtester({"US.SPY260904C00100000": option}, OptionOnly, start_date="20260901",
                        end_date="20260908", cash=100000, commission=0, slippage=0,
                        enable_plot=False, verbose=False)
    engine.run()
    # 缺标的价格时不猜结算价、不中断回测，保留原估值路径。
    assert engine.cerebro.broker.option_settlement_events == []
    assert any(p.size == 1 for p in engine.cerebro.broker.positions.values())


def test_stock_only_backtest_does_not_enable_expiry_settlement(monkeypatch):
    monkeypatch.setattr(config, "PRINT_PLAN", False)
    monkeypatch.setattr(config, "LOG", False)

    class Idle(BaseStrategy):
        def init(self):
            pass

        def next(self):
            pass

    dates = pd.bdate_range("2026-09-01", "2026-09-04")
    stock = pd.DataFrame({key: [100.0] * len(dates) for key in ("open", "high", "low", "close")}, index=dates)
    stock["volume"] = 1000
    engine = Backtester({"US.SPY": stock}, Idle, start_date="20260901", end_date="20260904",
                        cash=1000, enable_plot=False, verbose=False)
    engine.run()
    assert getattr(engine.cerebro.broker, "option_expiry_settlement", None) is None


class PutSpread(BaseStrategy):
    option_settlement = "physical"

    def init(self):
        self.uncommitted = []

    def next(self):
        stock = self.broker.datas[0]
        short = next(d for d in self.broker.datas if d._name.endswith("P00100000"))
        long = next(d for d in self.broker.datas if d._name.endswith("P00095000"))
        if len(stock) == 1:
            spot = float(stock.close[0])
            self.broker.submit_option_spread([
                {"data": short, "volume": 1, "effect": "SELL_TO_OPEN", "price": 2.0,
                 "risk_leg": OptionRiskLeg(short._name, stock._name, "PUT", -1, 100.0, 2.0, spot, 100.0,
                                           expiry="2026-09-04", historical_volatility=0.2)},
                {"data": long, "volume": 1, "effect": "BUY_TO_OPEN", "price": 1.0,
                 "risk_leg": OptionRiskLeg(long._name, stock._name, "PUT", 1, 95.0, 1.0, spot, 100.0,
                                           expiry="2026-09-04", historical_volatility=0.2)},
            ], volume=1)
        elif len(stock) == 2:
            self.uncommitted.append(self.broker.get_option_uncommitted_cash())


def run_spread(monkeypatch, cash=100000):
    monkeypatch.setattr(config, "PRINT_PLAN", False)
    monkeypatch.setattr(config, "LOG", False)
    dates = pd.bdate_range("2026-09-01", "2026-09-08")
    prices = [102, 102, 102, 90, 100, 100]
    stock = pd.DataFrame({key: prices for key in ("open", "high", "low", "close")}, index=dates)
    stock["volume"] = 1000

    def option(price):
        frame = pd.DataFrame({key: [price] * 4 for key in ("open", "high", "low", "close")}, index=dates[:4])
        frame["volume"], frame["contract_multiplier"] = 100, 100
        return frame

    engine = Backtester({"US.SPY": stock, "US.SPY260904P00100000": option(2.0),
                         "US.SPY260904P00095000": option(1.0)}, PutSpread,
                        start_date="20260901", end_date="20260908", cash=cash,
                        commission=0, slippage=0, enable_plot=False, verbose=False)
    engine.run()
    return engine


def test_itm_put_spread_settles_netted_without_collateral_error(monkeypatch):
    # 现金不足以全额接 100 股，但价差净交割只需支付执行价宽度。
    engine = run_spread(monkeypatch, cash=5000)
    broker = engine.cerebro.broker
    assert sorted(event["event"] for event in broker.option_settlement_events) == [
        "ASSIGNED_PUT", "EXERCISED_PUT",
    ]
    assert all(p.size == 0 for p in broker.positions.values())
    # 5000 + 权利金 200 - 保护腿 100 - 执行价宽度 500。
    assert broker.getcash() == pytest.approx(4600)
    spread = engine.get_closed_trades()[0]
    assert spread["pnl"] == pytest.approx(-400)


def test_filled_spread_reserves_width_not_full_csp_cash(monkeypatch):
    engine = run_spread(monkeypatch)
    strategy = engine.results[0].strategy
    # 成交后现金 100100，只扣定义风险 (100-95)*100，而非整笔 10000 指派现金。
    assert strategy.uncommitted == [pytest.approx(99600)]


def test_physical_settlement_rejects_intraday_approximation():
    engine = Backtester({}, HoldPut, timeframe="Minutes", verbose=False, enable_plot=False)
    with pytest.raises(ValueError, match="daily bars"):
        engine._init_broker()


def test_physical_settlement_rejects_partial_daily_cutoff():
    engine = Backtester({}, HoldPut, timeframe="Day:14:45:00", verbose=False, enable_plot=False)
    with pytest.raises(ValueError, match="Day cutoff bars are not official closing prices"):
        engine._init_broker()


def test_next_open_rejects_option_settlement_path(monkeypatch):
    with pytest.raises(ValueError, match="unsupported for option strategies"):
        Backtester(
            {"US.SPY": pd.DataFrame(), "US.SPY260904P00100000": pd.DataFrame()},
            HoldPut,
            timeframe="Days",
            compression=1,
            execution_price="next_open",
            verbose=False,
            enable_plot=False,
        )


def _short_stock_with_long_call(settlement, short_shares):
    class ShortStockProtectiveCall(BaseStrategy):
        option_settlement = settlement

        def init(self):
            pass

        def next(self):
            stock, option = self.broker.datas
            if len(stock) == 1:
                self.broker.sell(data=stock, size=short_shares)
                self.broker.submit_option_order(option, 1, "BUY_TO_OPEN", price=2)

    return ShortStockProtectiveCall


@pytest.mark.parametrize("spot", [110, 90])  # 价内 / 价外
def test_cash_settlement_keeps_pre_existing_short_stock(monkeypatch, spot):
    """现金结算不动正股，空股 + 保护性 Call 到期不得被当成担保不足而中断回测。"""
    engine = run_expiry(monkeypatch, spot=spot, strategy=_short_stock_with_long_call(None, 100),
                        option_symbol="US.SPY260904C00100000")
    broker = engine.cerebro.broker
    assert broker.option_expiry_settlement == "cash"
    stock = engine.cerebro.datas[0]
    assert broker.getposition(stock).size == -100
    event = broker.option_settlement_events[0]
    assert event["settlement"] == "cash"
    assert event["event"] == ("EXERCISED_CASH" if spot > 100 else "EXPIRED_OTM")


def test_physical_call_partially_covering_pre_existing_short_is_allowed(monkeypatch):
    """物理交割只约束交割新增的空头：空 200 股 + 1 张价内 Call 交割后剩 -100 股属于原有空头。"""
    engine = run_expiry(monkeypatch, spot=110, strategy=_short_stock_with_long_call("physical", 200),
                        option_symbol="US.SPY260904C00100000")
    stock = engine.cerebro.datas[0]
    assert engine.cerebro.broker.getposition(stock).size == -100


def test_physical_assignment_creating_new_short_still_fails(monkeypatch):
    """物理交割仍拒绝交割本身造成的新空头（无正股的空头 Call 被指派）。"""

    class NakedShortCall(BaseStrategy):
        option_settlement = "physical"

        def init(self):
            pass

        def next(self):
            stock, option = self.broker.datas
            if len(stock) == 1:
                leg = OptionRiskLeg(option._name, stock._name, "CALL", -1, 100, 2,
                                    float(stock.close[0]), 100, expiry="2026-09-04")
                self.broker.submit_option_order(option, 1, "SELL_TO_OPEN", price=2, risk_leg=leg,
                                                allow_sell_to_open=True, underlying_positions={stock._name: 100})

    with pytest.raises(ValueError, match="not fully collateralized"):
        run_expiry(monkeypatch, spot=110, strategy=NakedShortCall, option_symbol="US.SPY260904C00100000")
