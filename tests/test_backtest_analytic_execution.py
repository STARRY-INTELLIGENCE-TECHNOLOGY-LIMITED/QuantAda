"""回测撮合的解析解回归：买入持有、往返、整手、轮动同 K 完成、风控与轮动卖单互斥。

记号：P 价格，c 佣金率，s 滑点率，C 初始现金，f_b=(1+s)(1+c) 为买入成本系数，f_s=(1-s)(1-c) 为卖出回款系数。
"""

import math

import pytest

from risk_controls.base_risk_control import BaseRiskControl
from test_backtest_analytic_helpers import completed, make_df, run_script

COSTS = [(0.0, 0.0), (0.001, 0.002)]
BAD = {"Margin", "Rejected", "Canceled", "Expired"}


def _floor_lot(x, lot):
    return math.floor(x / lot + 1e-12) * lot


def _fills(events, symbol=None, buy=None):
    return [
        e for e in completed(events)
        if (symbol is None or e["symbol"] == symbol) and (buy is None or e["buy"] == buy)
    ]


@pytest.mark.parametrize("c, s", COSTS)
@pytest.mark.parametrize("mode", ["close", "next_open"])
def test_buy_and_hold_shares_cash_equity_and_fill_bar(monkeypatch, c, s, mode):
    # n = floor(C / (P*(1+s)*(1+c)))；现金 = C - n*P*(1+s)*(1+c)；权益 = 现金 + n*P_last。
    price, cash = 37.0, 10000.0
    engine, events = run_script(
        {"A": make_df([price] * 5)}, {2: [("tv", 0, 1e9)]},
        cash=cash, commission=c, slippage=s, execution_price=mode, monkeypatch=monkeypatch,
    )
    n = math.floor(cash / (price * (1 + s) * (1 + c)))
    fill = _fills(events, "A", True)
    assert len(fill) == 1 and not BAD & {e["status"] for e in events}
    assert fill[0]["size"] == n
    assert fill[0]["price"] == pytest.approx(price * (1 + s), abs=1e-9)
    # close 在信号日（第 2 根）收盘成交；next_open 在第 3 根开盘成交。
    assert str(fill[0]["fill_date"]) == ("2024-01-02" if mode == "close" else "2024-01-03")
    broker = engine.cerebro.broker
    expected_cash = cash - n * price * (1 + s) * (1 + c)
    assert broker.getcash() == pytest.approx(expected_cash, abs=1e-6)
    assert broker.getvalue() == pytest.approx(expected_cash + n * price, abs=1e-6)


@pytest.mark.parametrize("c, s", COSTS)
@pytest.mark.parametrize("mode", ["close", "next_open"])
def test_round_trip_cash_closed_form(monkeypatch, c, s, mode):
    # 买 n 股后全部卖出：现金 = C - n*P*(1+s)(1+c) + n*P*(1-s)(1-c)，空仓后权益等于现金。
    price, cash = 41.0, 10000.0
    engine, events = run_script(
        {"A": make_df([price] * 6)}, {2: [("tv", 0, 1e9)], 4: [("tv", 0, 0)]},
        cash=cash, commission=c, slippage=s, execution_price=mode, monkeypatch=monkeypatch,
    )
    n = math.floor(cash / (price * (1 + s) * (1 + c)))
    sells = _fills(events, "A", False)
    assert len(sells) == 1 and sells[0]["size"] == -n
    assert sells[0]["price"] == pytest.approx(price * (1 - s), abs=1e-9)
    broker = engine.cerebro.broker
    expected = cash - n * price * (1 + s) * (1 + c) + n * price * (1 - s) * (1 - c)
    assert broker.getcash() == pytest.approx(expected, abs=1e-6)
    assert broker.getvalue() == pytest.approx(expected, abs=1e-6)
    assert all(p.size == 0 for p in broker.positions.values())


@pytest.mark.parametrize("lot", [1, 100])
@pytest.mark.parametrize("c, s", COSTS)
def test_lot_size_rounding_and_leftover_cash(monkeypatch, lot, c, s):
    # n = floor(C / (P*f_b) / lot) * lot，剩余现金 = C - n*P*f_b。
    price, cash = 37.0, 100000.0
    engine, events = run_script(
        {"A": make_df([price] * 4)}, {2: [("tv", 0, 1e9)]},
        cash=cash, commission=c, slippage=s, lot_size=lot, monkeypatch=monkeypatch,
    )
    n = _floor_lot(cash / (price * (1 + s) * (1 + c)), lot)
    fill = _fills(events, "A", True)[0]
    assert fill["size"] == n and n % lot == 0
    assert engine.cerebro.broker.getcash() == pytest.approx(cash - n * price * (1 + s) * (1 + c), abs=1e-6)


def test_close_mode_buy_ignores_next_bar_gap_down_high(monkeypatch):
    # 信号日收盘 100，次日整根跳空到 high=60；旧逻辑会把买价压到 60。COC 必须仍按 100*(1+s) 成交。
    s = 0.01
    df = make_df([100, 100, 55, 55], opens=[100, 100, 55, 55], highs=[100, 100, 60, 60], lows=[100, 100, 50, 50])
    _, events = run_script({"A": df}, {2: [("tv", 0, 1000)]}, cash=1000.0, slippage=s,
                           monkeypatch=monkeypatch)
    fill = _fills(events, "A", True)[0]
    assert fill["price"] == pytest.approx(100 * (1 + s), abs=1e-9)
    assert str(fill["fill_date"]) == "2024-01-02"


def test_close_mode_sell_ignores_next_bar_gap_up_low(monkeypatch):
    # 第 3 根收盘 100 卖出，第 4 根整根跳空到 low=150；旧逻辑会把卖价抬到 150。
    s = 0.01
    df = make_df([100, 100, 100, 160, 160], opens=[100, 100, 100, 155, 160],
                 highs=[100, 100, 100, 170, 170], lows=[100, 100, 100, 150, 150])
    _, events = run_script({"A": df}, {2: [("tv", 0, 1000)], 3: [("tv", 0, 0)]}, cash=1000.0,
                           slippage=s, monkeypatch=monkeypatch)
    sell = _fills(events, "A", False)[0]
    assert sell["price"] == pytest.approx(100 * (1 - s), abs=1e-9)
    assert str(sell["fill_date"]) == "2024-01-03"


def test_next_open_fills_at_next_open_and_shrinks_when_gap_up_unaffordable(monkeypatch):
    # 信号收盘 100 按 100 估算 n0=floor(C/(100*f_b))；次日开盘 110，现金不够则按开盘价缩量：
    # n = floor(C / (110*f_b))，成交价 110*(1+s)。
    c, s, cash = 0.001, 0.01, 10000.0
    df = make_df([100, 100, 110, 110], opens=[100, 100, 110, 110], highs=[100, 100, 130, 130],
                 lows=[100, 100, 90, 90])
    engine, events = run_script({"A": df}, {2: [("tv", 0, 1e9)]}, cash=cash, commission=c, slippage=s,
                                execution_price="next_open", monkeypatch=monkeypatch)
    fill = _fills(events, "A", True)[0]
    n = math.floor(cash / (110 * (1 + s) * (1 + c)))
    assert fill["size"] == n and n < math.floor(cash / (100 * (1 + s) * (1 + c)))
    assert fill["price"] == pytest.approx(110 * (1 + s), abs=1e-9)
    assert str(fill["fill_date"]) == "2024-01-03"
    assert not BAD & {e["status"] for e in events}
    assert engine.cerebro.broker.getcash() == pytest.approx(cash - n * 110 * (1 + s) * (1 + c), abs=1e-6)


def test_next_open_gap_down_fills_requested_size_at_lower_open(monkeypatch):
    # 开盘低于信号收盘：数量仍为 floor(C/(100*f_b))，成交价 90*(1+s)，多出的现金留在账户。
    c, s, cash = 0.001, 0.01, 10000.0
    df = make_df([100, 100, 90, 90], opens=[100, 100, 90, 90], highs=[100, 100, 120, 120],
                 lows=[100, 100, 80, 80])
    engine, events = run_script({"A": df}, {2: [("tv", 0, 1e9)]}, cash=cash, commission=c, slippage=s,
                                execution_price="next_open", monkeypatch=monkeypatch)
    fill = _fills(events, "A", True)[0]
    n = math.floor(cash / (100 * (1 + s) * (1 + c)))
    assert fill["size"] == n
    assert fill["price"] == pytest.approx(90 * (1 + s), abs=1e-9)
    assert engine.cerebro.broker.getcash() == pytest.approx(cash - n * 90 * (1 + s) * (1 + c), abs=1e-6)


@pytest.mark.parametrize("mode", ["close", "next_open"])
@pytest.mark.parametrize("lot", [1, 100])
@pytest.mark.parametrize("op", ["tv", "tp"])
@pytest.mark.parametrize("c, s", [(0.0, 0.0), (0.0003, 0.001), (0.002, 0.005)])
def test_full_rotation_completes_both_legs_on_same_bar(monkeypatch, c, s, op, lot, mode):
    # 先用全部现金买 A，下一信号 K 同时 A 清仓、B 满仓：卖单回款按 f_s 计入购买力，
    # nB = floor_lot((C1 + nA*PA*f_s) / (PB*f_b))，C1 = C - nA*PA*f_b。两腿必须同 K 成交，无 Margin 拒单。
    pa, pb, cash = 100.0, 50.0, 100000.0
    full = 1e9 if op == "tv" else 1.0
    datas = {"A": make_df([pa] * 6), "B": make_df([pb] * 6)}
    script = {2: [(op, 0, full)], 4: [(op, 0, 0.0), (op, 1, full)]}
    engine, events = run_script(datas, script, cash=cash, commission=c, slippage=s, lot_size=lot,
                                execution_price=mode, monkeypatch=monkeypatch)
    assert not BAD & {e["status"] for e in events}, events
    fb, fs = (1 + s) * (1 + c), (1 - s) * (1 - c)
    na = _floor_lot(cash / (pa * fb), lot)
    cash1 = cash - na * pa * fb
    nb = _floor_lot((cash1 + na * pa * fs) / (pb * fb), lot)
    a_sell, b_buy = _fills(events, "A", False), _fills(events, "B", True)
    assert len(a_sell) == 1 and len(b_buy) == 1
    assert a_sell[0]["fill_date"] == b_buy[0]["fill_date"]
    assert str(b_buy[0]["fill_date"]) == ("2024-01-04" if mode == "close" else "2024-01-05")
    assert a_sell[0]["size"] == -na
    assert b_buy[0]["size"] == nb
    expected_cash = cash1 + na * pa * fs - nb * pb * fb
    broker = engine.cerebro.broker
    assert broker.getcash() == pytest.approx(expected_cash, abs=1e-6)
    assert broker.getvalue() == pytest.approx(expected_cash + nb * pb, abs=1e-6)


class _SellWhileLongRisk(BaseRiskControl):
    """测试用风控：持有多仓时每根 K 都要求卖出。"""

    def check(self, data):
        return "SELL" if self.broker.getposition(data).size > 0 else None


@pytest.mark.parametrize("mode", ["close", "next_open"])
def test_risk_sell_and_rotation_sell_same_bar_never_goes_short(monkeypatch, mode):
    # 风控已在本 K 平仓 A，轮动脚本在同一 K 再对 A 下目标 0 的卖单：不得重复卖出把多仓打成空头。
    cash, price = 10000.0, 50.0
    datas = {"A": make_df([price] * 7), "B": make_df([price] * 7)}
    script = {2: [("tv", 0, 1e9)], 4: [("tv", 0, 0.0), ("tv", 1, 1e9)]}
    engine, events = run_script(datas, script, cash=cash, execution_price=mode, monkeypatch=monkeypatch,
                                risk_control_classes=[_SellWhileLongRisk], risk_control_params={})
    broker = engine.cerebro.broker
    sizes = {d._name: broker.getposition(d).size for d in engine.cerebro.datas}
    assert all(size >= 0 for size in sizes.values()), sizes
    a_bought = sum(e["size"] for e in _fills(events, "A", True))
    a_sold = sum(e["size"] for e in _fills(events, "A", False))
    assert a_sold == -a_bought
