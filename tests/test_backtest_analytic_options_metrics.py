"""期权到期结算、无报价日估值与绩效指标的解析解回归（合成行情，无外部依赖）。

期权乘数 m=100。现金结算：到期盈亏 = 权利金 - 内在价值*m（空头）/ 内在价值*m - 权利金（多头）。
"""

import math

import numpy as np
import pandas as pd
import pytest

import config
from backtest.backtester import Backtester
from common.options.risk import OptionRiskLeg
from strategies.base_strategy import BaseStrategy
from test_backtest_analytic_helpers import completed, make_df, run_script

DATES = pd.bdate_range("2026-09-01", "2026-09-08")  # 第 4 根 (09-04) 为到期日
CASH = 100000.0


def _stock(spot_at_expiry, last=None):
    prices = [102, 102, 102, spot_at_expiry, last if last is not None else spot_at_expiry,
              last if last is not None else spot_at_expiry]
    frame = pd.DataFrame({key: prices for key in ("open", "high", "low", "close")}, index=DATES, dtype=float)
    frame["volume"] = 1000
    return frame


def _option(price, hole=None, bars=4):
    frame = pd.DataFrame({key: [float(price)] * bars for key in ("open", "high", "low", "close")},
                         index=DATES[:bars])
    frame["volume"], frame["contract_multiplier"] = 100, 100
    if hole is not None:
        frame = frame.drop(pd.Timestamp(hole))
    return frame


def _run(monkeypatch, datas, strategy, *, end="20260908", commission=0.0):
    monkeypatch.setattr(config, "PRINT_PLAN", False)
    monkeypatch.setattr(config, "LOG", False)
    monkeypatch.setattr(config, "LOT_SIZE", 1)
    engine = Backtester(datas, strategy, start_date="20260901", end_date=end, cash=CASH,
                        commission=commission, slippage=0, enable_plot=False, verbose=False)
    engine.run()
    return engine


def _long_call(equity_log=None):
    class LongCall(BaseStrategy):
        def init(self):
            pass

        def next(self):
            stock, option = self.broker.datas
            if equity_log is not None:
                equity_log.append(self.broker.getvalue())
            if len(stock) == 1:
                self.broker.submit_option_order(option, 1, "BUY_TO_OPEN", price=2)

    return LongCall


def test_long_call_otm_expiry_loses_exactly_the_premium(monkeypatch):
    # 现金 = C - 2*100；OTM 到期内在价值 0，权益 = 现金，仓位清零，已平仓盈亏 = -200。
    engine = _run(monkeypatch, {"US.SPY": _stock(95), "US.SPY260904C00100000": _option(2.0)}, _long_call())
    broker = engine.cerebro.broker
    assert broker.getcash() == pytest.approx(CASH - 200, abs=1e-6)
    assert broker.getvalue() == pytest.approx(CASH - 200, abs=1e-6)
    assert all(p.size == 0 for p in broker.positions.values())
    trade = engine.get_closed_trades()[0]
    assert trade["pnl"] == pytest.approx(-200)
    assert trade["settlement_event"] == "EXPIRED_OTM"


def test_long_call_itm_expiry_pnl_is_intrinsic_minus_premium(monkeypatch):
    # 价内 spot=107：现金 = C - 200 + 7*100，盈亏 = +500；到期后保持平稳，权益不再变化。
    engine = _run(monkeypatch, {"US.SPY": _stock(107), "US.SPY260904C00100000": _option(2.0)}, _long_call())
    broker = engine.cerebro.broker
    assert broker.getcash() == pytest.approx(CASH + 500, abs=1e-6)
    assert broker.getvalue() == pytest.approx(CASH + 500, abs=1e-6)
    assert engine.get_closed_trades()[0]["pnl"] == pytest.approx(500)


def test_equity_does_not_jump_on_a_day_without_option_quote(monkeypatch):
    # 价格恒定的多头 Call，第 3 根无报价：估值沿用上一有效价 2，权益逐 K 恒为 C（0 佣金 0 滑点）。
    log = []
    _run(monkeypatch,
         {"US.SPY": _stock(95, last=95), "US.SPY260904C00100000": _option(2.0, hole="2026-09-03")},
         _long_call(log), end="20260903")
    assert len(log) >= 3
    assert log == [pytest.approx(CASH, abs=1e-6)] * len(log)


@pytest.mark.parametrize("spot, pnl", [(110, 500), (95, 200)])
def test_covered_call_physical_assignment_nets_to_strike_delivery(monkeypatch, spot, pnl):
    # 第 1 根 102 买入 100 股，第 2 根卖出行权价 100 的 Call 收权利金 2。
    # 价内 spot=110：被指派按 100 交出股票。现金 = C - 10200 + 200 + 10000，总盈亏 0（持股成本 102 > 行权价 100）。
    # 价外 spot=95：Call 作废，保留股票，权益 = C - 10200 + 200 + 100*95 = C - 500。
    class CoveredCall(BaseStrategy):
        option_settlement = "physical"

        def init(self):
            pass

        def next(self):
            stock, option = self.broker.datas
            if len(stock) == 1:
                self.broker.buy(data=stock, size=100)
            elif len(stock) == 2:
                leg = OptionRiskLeg(option._name, stock._name, "CALL", -1, 100, 2, float(stock.close[0]), 100,
                                    expiry="2026-09-04")
                self.broker.submit_option_order(option, 1, "SELL_TO_OPEN", price=2, risk_leg=leg,
                                                allow_sell_to_open=True, underlying_positions={stock._name: 100})

    engine = _run(monkeypatch, {"US.SPY": _stock(spot), "US.SPY260904C00100000": _option(2.0)}, CoveredCall)
    broker = engine.cerebro.broker
    stock = engine.cerebro.datas[0]
    if spot > 100:
        assert [e["event"] for e in broker.option_settlement_events] == ["ASSIGNED_CALL"]
        assert broker.getposition(stock).size == 0
        assert broker.getcash() == pytest.approx(CASH - 10200 + 200 + 10000, abs=1e-6)
        assert broker.getvalue() == pytest.approx(CASH, abs=1e-6)
    else:
        assert [e["event"] for e in broker.option_settlement_events] == ["EXPIRED_OTM"]
        assert broker.getposition(stock).size == 100
        assert broker.getcash() == pytest.approx(CASH - 10200 + 200, abs=1e-6)
        assert broker.getvalue() == pytest.approx(CASH - 10200 + 200 + 100 * spot, abs=1e-6)


def _put_spread(settlement):
    class PutCreditSpread(BaseStrategy):
        option_settlement = settlement

        def init(self):
            pass

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

    return PutCreditSpread


@pytest.mark.parametrize("settlement", [None, "physical"])
@pytest.mark.parametrize("spot, pnl", [
    (105, 100),    # 全价外：净收权利金 (2-1)*100
    (97, -200),    # 仅空腿价内：100 - (100-97)*100
    (80, -400),    # 两腿皆价内：最大亏损 = 宽度 (100-95)*100 - 净权利金 100
])
def test_put_credit_spread_pnl_is_credit_minus_intrinsic_spread(monkeypatch, settlement, spot, pnl):
    engine = _run(monkeypatch, {
        "US.SPY": _stock(spot), "US.SPY260904P00100000": _option(2.0), "US.SPY260904P00095000": _option(1.0),
    }, _put_spread(settlement))
    broker = engine.cerebro.broker
    # 两种结算口径的账户权益一致：C + pnl；物理交割的股票腿按结算价入账，不额外产生盈亏。
    assert broker.getvalue() == pytest.approx(CASH + pnl, abs=1e-6)
    assert sum(1 for d in engine.cerebro.datas if d._name.startswith("US.SPY2") and broker.getposition(d).size) == 0
    if settlement is None:
        assert broker.getcash() == pytest.approx(CASH + pnl, abs=1e-6)
    assert sum(t["pnl"] for t in engine.get_closed_trades() if "SPY2609" in t["symbol"]) == pytest.approx(pnl)


# ---------------------------------------------------------------------------
# 绩效指标恒等式
# ---------------------------------------------------------------------------

def _hold_all(closes, cash=10000.0, **kwargs):
    # c=s=0、整数价、cash=10000：第 1 根以 100 买入 100 股，权益路径 = 100*close。
    df = make_df(closes)
    engine, _ = run_script({"A": df}, {1: [("tv", 0, 1e9)]}, cash=cash, **kwargs)
    return engine


def test_total_return_and_cagr_follow_final_value_and_years(monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 1)
    closes = [100.0] * 5 + [150.0] * 5 + [121.0] * 391  # 401 天；终值 = 100*121 = 12100
    engine = _hold_all(closes)
    metrics = engine.get_performance_metrics()
    final = engine.cerebro.broker.getvalue()
    assert final == pytest.approx(12100.0)
    assert metrics["total_return"] == pytest.approx(final / 10000.0 - 1)
    assert metrics["total_return"] * 100 == pytest.approx((final / metrics["initial_portfolio"] - 1) * 100)
    years = (metrics["end_date"] - metrics["start_date"]).days / 365.25
    assert years == pytest.approx(400 / 365.25)
    assert metrics["annual_return"] == pytest.approx((1 + metrics["total_return"]) ** (1 / years) - 1)
    # 复利回推：按 years 年化后再复利 years 年应还原总收益。
    assert (1 + metrics["annual_return"]) ** years == pytest.approx(1 + metrics["total_return"])


def test_max_drawdown_matches_scripted_equity_path(monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 1)
    # 权益 = 100*close：峰 120(12000) -> 谷 90(9000) 回撤 25%；其后 150 -> 120 仅 20%，最大回撤取 25%。
    engine = _hold_all([100, 120, 90, 150, 120])
    metrics = engine.get_performance_metrics()
    assert metrics["max_drawdown"] == pytest.approx(0.25, abs=1e-9)
    assert metrics["calmar_ratio"] == pytest.approx(metrics["annual_return"] / 0.25)


@pytest.mark.parametrize("timeframe, freq, factor", [
    ("Days", "D", 252.0),
    ("Weeks", "W-FRI", 52.0),
    ("Months", "ME", 12.0),
])
def test_sharpe_value_uses_bar_size_annualization(monkeypatch, timeframe, freq, factor):
    # Sharpe = mean(r)/std(r, ddof=0)*sqrt(factor)，r 为各 K 权益收益率（首个收益相对初始现金），无风险利率 0。
    monkeypatch.setattr(config, "LOT_SIZE", 1)
    closes = [100, 102, 101, 105, 103, 108, 107, 111, 109, 114, 112, 118]
    df = make_df(closes)
    df.index = pd.date_range("2024-01-31", periods=len(closes), freq=freq)
    engine, _ = run_script({"A": df}, {1: [("tv", 0, 1e9)]}, cash=10000.0, timeframe=timeframe)
    equity = np.array([10000.0] + [c * 100.0 for c in closes])
    returns = np.diff(equity) / equity[:-1]
    expected = returns.mean() / returns.std() * math.sqrt(factor)
    assert engine.get_performance_metrics()["sharpe_ratio"] == pytest.approx(expected, rel=1e-9)
