"""验证物理交割只在显式声明的日线回测中生效。"""

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


def run_expiry(monkeypatch, spot=90, strategy=HoldPut, end="20260908", commission=0, missing_expiry=False):
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
    engine = Backtester({"US.SPY": stock, "US.SPY260904P00100000": option}, strategy,
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
    assert engine.get_closed_trades()[0]["pnl"] == pytest.approx(200)


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


def test_legacy_strategy_keeps_original_settlement_semantics(monkeypatch):
    class Legacy(HoldPut):
        option_settlement = None
    engine = run_expiry(monkeypatch, strategy=Legacy)
    assert not hasattr(engine.cerebro.broker, "option_settlement_events")
    assert engine.cerebro.broker.getcash() == pytest.approx(100200)
    assert any(p.size == -1 for p in engine.cerebro.broker.positions.values())


def test_physical_settlement_rejects_intraday_approximation():
    engine = Backtester({}, HoldPut, timeframe="Minutes", verbose=False, enable_plot=False)
    with pytest.raises(ValueError, match="daily bars"):
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
