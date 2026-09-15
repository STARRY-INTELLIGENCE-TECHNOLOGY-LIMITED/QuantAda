import importlib
from types import SimpleNamespace

import pandas as pd
import pytest

from common.options.analytics import parse_option_symbol

from strategies.options.sample_cash_secured_put_strategy import SampleCashSecuredPutStrategy
from strategies.options.sample_covered_call_strategy import SampleCoveredCallStrategy
from strategies.options.sample_long_option_strategy import SampleLongCallStrategy, SampleLongPutStrategy
from strategies.options.sample_put_credit_spread_strategy import SamplePutCreditSpreadStrategy


class DummyData:
    def __init__(self, name, dataframe):
        self._name = name
        self.p = SimpleNamespace(dataname=dataframe)


class DummyBroker:
    def __init__(self, datas, positions=None, cash=100000.0):
        self.datas = datas
        self.positions = positions or {}
        self.cash = cash
        self.orders = []
        self.is_live = False

    def get_position(self, data):
        return self.positions.get(data._name, SimpleNamespace(size=0, price=0.0))

    getposition = get_position

    def get_current_price(self, data):
        return float(data.p.dataname.iloc[-1]["close"])

    def get_contract_multiplier(self, data):
        return 100.0 if parse_option_symbol(data._name).get("option_type") else 1.0

    def submit_option_order(self, data, volume, effect, price=None, **kwargs):
        self.orders.append({
            "symbol": data._name,
            "volume": volume,
            "effect": effect,
            "price": price,
            "kwargs": kwargs,
        })
        return object()

    def submit_option_spread(self, legs, volume=1, **kwargs):
        self.orders.extend({
            "symbol": leg["data"]._name,
            "volume": volume,
            "effect": leg["effect"],
            "price": leg.get("price"),
            "kwargs": leg,
        } for leg in legs)
        return list(self.orders[-len(legs):])

    def log(self, *_args, **_kwargs):
        return None


def _underlying(close=16.0, name="US.MARA"):
    index = pd.DatetimeIndex(["2026-09-14"])
    return DummyData(name, pd.DataFrame(
        {"open": [close], "high": [close], "low": [close], "close": [close], "volume": [1000]},
        index=index,
    ))


def _option(symbol, option_type, strike, delta, bid=1.0, ask=1.1, close=1.05, expiry="2026-10-16"):
    index = pd.DatetimeIndex(["2026-09-14"])
    return DummyData(symbol, pd.DataFrame(
        {
            "open": [close], "high": [ask], "low": [bid], "close": [close], "volume": [100],
            "option_type": [option_type], "strike": [strike], "expiry": [expiry],
            "delta": [delta], "iv_percentile": [40.0], "bid": [bid], "ask": [ask],
            "last": [close], "open_interest": [500], "contract_multiplier": [100.0],
        },
        index=index,
    ))


def test_long_put_buys_to_open():
    underlying = _underlying()
    put = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15)
    broker = DummyBroker([underlying, put])
    strategy = SampleLongPutStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders[0]["effect"] == "BUY_TO_OPEN"
    assert broker.orders[0]["symbol"] == put._name


def test_long_call_buys_to_open():
    underlying = _underlying()
    call = _option("US.MARA261016C00018000", "CALL", 18.0, 0.18)
    broker = DummyBroker([underlying, call])
    strategy = SampleLongCallStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders[0]["effect"] == "BUY_TO_OPEN"



def test_long_put_does_not_stack_another_contract():
    underlying = _underlying()
    held = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15)
    other = _option("US.MARA261016P00014000", "PUT", 14.0, -0.12)
    broker = DummyBroker(
        [underlying, held, other],
        positions={held._name: SimpleNamespace(size=1, price=1.0)},
    )
    strategy = SampleLongPutStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders == []
    assert strategy.last_signals[0]["action"] == "HOLD_LONG"


def test_long_put_sells_to_close_at_defense_dte():
    underlying = _underlying()
    put = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15, expiry="2026-09-21")
    broker = DummyBroker(
        [underlying, put],
        positions={put._name: SimpleNamespace(size=1, price=1.0)},
    )
    strategy = SampleLongPutStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders[0]["effect"] == "SELL_TO_CLOSE"
    assert broker.orders[0]["price"] == pytest.approx(1.0)


def test_cash_secured_put_sells_to_open_with_risk_leg():
    underlying = _underlying()
    put = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15)
    broker = DummyBroker([underlying, put])
    strategy = SampleCashSecuredPutStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders[0]["effect"] == "SELL_TO_OPEN"
    assert broker.orders[0]["kwargs"]["allow_sell_to_open"] is True
    assert broker.orders[0]["kwargs"]["risk_leg"].strike == 15.0


def test_covered_call_requires_shares():
    underlying = _underlying()
    call = _option("US.MARA261016C00018000", "CALL", 18.0, 0.18)
    broker = DummyBroker([underlying, call])
    strategy = SampleCoveredCallStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders == []
    assert strategy.last_signals[0]["blocked_reason"] == "covered_call_shares_insufficient"


def test_covered_call_sells_when_shares_cover():
    underlying = _underlying()
    call = _option("US.MARA261016C00018000", "CALL", 18.0, 0.18)
    broker = DummyBroker(
        [underlying, call],
        positions={underlying._name: SimpleNamespace(size=100, price=16.0)},
    )
    strategy = SampleCoveredCallStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders[0]["effect"] == "SELL_TO_OPEN"
    assert broker.orders[0]["kwargs"]["risk_leg"].option_type == "CALL"


def test_put_credit_spread_submits_two_legs():
    underlying = _underlying()
    short = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15, bid=1.20, ask=1.30)
    long = _option("US.MARA261016P00012000", "PUT", 12.0, -0.08, bid=0.40, ask=0.45)
    broker = DummyBroker([underlying, short, long])
    strategy = SamplePutCreditSpreadStrategy(broker)
    strategy.init()
    strategy.next()
    assert [item["effect"] for item in broker.orders] == ["SELL_TO_OPEN", "BUY_TO_OPEN"]
    assert broker.orders[0]["symbol"] == short._name
    assert broker.orders[1]["symbol"] == long._name


def test_put_credit_spread_blocks_without_protective_leg():
    underlying = _underlying()
    short = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15)
    broker = DummyBroker([underlying, short])
    strategy = SamplePutCreditSpreadStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders == []
    assert strategy.last_signals[0]["blocked_reason"] == "protective_put_unavailable"


def test_put_credit_spread_closes_atomically():
    underlying = _underlying()
    short = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15, ask=0.40, expiry="2026-09-21")
    long = _option("US.MARA261016P00012000", "PUT", 12.0, -0.08, bid=0.10, expiry="2026-09-21")
    broker = DummyBroker(
        [underlying, short, long],
        positions={
            short._name: SimpleNamespace(size=-1, price=1.20),
            long._name: SimpleNamespace(size=1, price=0.45),
        },
    )
    strategy = SamplePutCreditSpreadStrategy(broker)
    strategy.init()
    strategy.next()
    assert [item["effect"] for item in broker.orders] == ["BUY_TO_CLOSE", "SELL_TO_CLOSE"]
    assert broker.orders[0]["price"] == pytest.approx(0.40)
    assert broker.orders[1]["price"] == pytest.approx(0.10)


def test_long_put_allows_zero_bid_when_ask_exists():
    underlying = _underlying()
    put = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15, bid=0.0, ask=1.10, close=1.05)
    broker = DummyBroker([underlying, put])
    strategy = SampleLongPutStrategy(broker)
    strategy.init()
    strategy.next()
    assert broker.orders[0]["effect"] == "BUY_TO_OPEN"


def test_option_contract_falls_back_when_row_type_is_nan():
    import numpy as np
    from strategies.options.support import option_contract
    put = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15)
    put.p.dataname.loc[:, "option_type"] = np.nan
    put.p.dataname.loc[:, "strike"] = np.nan
    row = put.p.dataname.iloc[-1]
    meta = option_contract(put, row)
    assert meta["option_type"] == "PUT"
    assert meta["strike"] == 15.0

def test_option_limit_price_only_crosses_when_live():
    from strategies.options.support import option_limit_price
    put = _option("US.MARA261016P00015000", "PUT", 15.0, -0.15, bid=1.0, ask=1.1, close=1.05)
    broker = DummyBroker([put])
    quote = {"bid": 1.0, "ask": 1.1}
    assert option_limit_price(broker, put, quote, "BUY_TO_OPEN") == 1.1
    assert option_limit_price(broker, put, quote, "SELL_TO_OPEN") == 1.0
    broker.is_live = True
    assert option_limit_price(broker, put, quote, "BUY_TO_OPEN") == pytest.approx(1.15)
    assert option_limit_price(broker, put, quote, "SELL_TO_OPEN") == pytest.approx(0.95)


@pytest.mark.parametrize("modname, clsname", [
    ("strategies.options.sample_cash_secured_put_strategy", "SampleCashSecuredPutStrategy"),
    ("strategies.options.sample_covered_call_strategy", "SampleCoveredCallStrategy"),
    ("strategies.options.sample_long_option_strategy", "SampleLongPutStrategy"),
    ("strategies.options.sample_long_option_strategy", "SampleLongCallStrategy"),
    ("strategies.options.sample_put_credit_spread_strategy", "SamplePutCreditSpreadStrategy"),
    ("strategies.sample_auto_rebalance_strategy", "SampleAutoRebalanceStrategy"),
    ("strategies.sample_macd_cross_strategy", "SampleMacdCrossStrategy"),
])
def test_sample_option_strategy_docstring_has_run_command(modname, clsname):
    module = importlib.import_module(modname)
    text = "\n".join(filter(None, [module.__doc__, getattr(module, clsname).__doc__]))
    assert "python run.py" in text
    assert clsname in text
    for line in text.splitlines():
        assert len(line) <= 200
