from types import SimpleNamespace

import pandas as pd

from strategies.options.support import held_protective_put, reserved_underlying_keys
from strategies.options.sample_put_credit_spread_strategy import SamplePutCreditSpreadStrategy


class _Data:
    def __init__(self, name, frame):
        self._name = name
        self.p = SimpleNamespace(dataname=frame)


class _Broker:
    def __init__(self, datas, positions=None, pending=None, live=False):
        self.datas = datas
        self.positions = positions or {}
        self.pending = pending or []
        self.is_live = live
        self.orders = []

    def getposition(self, data):
        return self.positions.get(data._name, SimpleNamespace(size=0, price=0.0))

    def get_pending_orders(self):
        return self.pending

    def submit_option_order(self, data, volume, effect, price=None, **kwargs):
        self.orders.append((data._name, effect, price))
        return object()

    def submit_option_spread(self, legs, volume=1, **kwargs):
        self.orders.append(("spread", legs))
        return object()


def _frame(close, bid, ask, strike, day="2024-01-02"):
    return pd.DataFrame(
        {
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [1.0],
            "bid": [bid],
            "ask": [ask],
            "delta": [-0.12],
            "strike": [strike],
            "expiry": ["2024-01-19"],
            "option_type": ["PUT"],
            "open_interest": [1000.0],
        },
        index=pd.to_datetime([day]),
    )


def test_reserved_underlying_keys_counts_zero_padded_position():
    held = _Data("US.AAPL240119P00150000", _frame(0.0, 0.0, 0.0, 150.0))
    broker = _Broker([held], positions={held._name: SimpleNamespace(size=-1, price=2.0)})

    assert reserved_underlying_keys(broker, {"PUT"}) == {"AAPL"}


def test_held_protective_put_keeps_position_when_quote_is_missing():
    short = _Data("US.AAPL240119P00150000", _frame(2.0, 1.9, 2.1, 150.0))
    protective = _Data("US.AAPL240119P00120000", _frame(0.0, 0.0, 0.0, 120.0))
    broker = _Broker(
        [short, protective],
        positions={protective._name: SimpleNamespace(size=1, price=0.4)},
    )
    meta = {
        "underlying_key": "AAPL",
        "expiry": pd.Timestamp("2024-01-19"),
        "strike": 150.0,
    }

    found = held_protective_put(broker, meta, pd.Timestamp("2024-01-02"))

    assert found is not None
    assert found["quote"] is None
    assert found["meta"]["symbol"] == protective._name


def test_sample_pcs_does_not_flatten_short_when_protective_quote_is_missing():
    short = _Data("US.AAPL240119P00150000", _frame(2.0, 1.9, 2.1, 150.0))
    protective = _Data("US.AAPL240119P00120000", _frame(0.0, 0.0, 0.0, 120.0))
    underlying = _Data(
        "US.AAPL",
        pd.DataFrame(
            {"open": [100], "high": [100], "low": [100], "close": [100], "volume": [1]},
            index=pd.to_datetime(["2024-01-02"]),
        ),
    )
    broker = _Broker(
        [underlying, short, protective],
        positions={
            short._name: SimpleNamespace(size=-1, price=2.0),
            protective._name: SimpleNamespace(size=1, price=0.4),
        },
    )
    strategy = SamplePutCreditSpreadStrategy(broker, params={"defense_dte": 21})
    strategy.init()
    strategy.next()

    assert broker.orders == []
    assert strategy.last_signals[0]["blocked_reason"] == "protective_put_quote_unavailable"
