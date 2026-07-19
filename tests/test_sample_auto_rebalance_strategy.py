from datetime import datetime

import pandas as pd

from strategies.sample_auto_rebalance_strategy import SampleAutoRebalanceStrategy


class DummyData:
    def __init__(self, name):
        self._name = name


class DummyBroker:
    def __init__(self, datas, current_dt):
        self.datas = datas
        self.current_dt = current_dt
        self.is_live = False

    @property
    def datetime(self):
        current_dt = self.current_dt

        class DateTimeProxy:
            @staticmethod
            def datetime(ago=0):
                return current_dt

        return DateTimeProxy()

    def log(self, txt, dt=None):
        return None


def test_sample_auto_rebalance_publishes_rankings_before_rebalance(monkeypatch):
    current_dt = datetime(2026, 4, 3)
    index = pd.date_range("2026-04-01", periods=3, freq="D")
    data_a = DummyData("AAA")
    data_b = DummyData("BBB")
    data_c = DummyData("CCC")
    broker = DummyBroker([data_a, data_b, data_c], current_dt)
    strategy = SampleAutoRebalanceStrategy(
        broker=broker,
        params={"selectTopK": 1, "rebalance_threshold": 0.05},
    )
    strategy.roc_signals = {
        "AAA": pd.Series([0.1, 0.2, 1.0], index=index),
        "BBB": pd.Series([0.1, 0.2, 3.0], index=index),
        "CCC": pd.Series([0.1, 0.2, -1.0], index=index),
    }

    ranking_calls = []
    rebalance_calls = []
    monkeypatch.setattr(
        strategy,
        "publish_rankings",
        lambda candidates, **kwargs: ranking_calls.append((list(candidates), kwargs)) or True,
    )
    monkeypatch.setattr(
        strategy,
        "execute_rebalance",
        lambda **kwargs: rebalance_calls.append(kwargs),
    )

    strategy.next()

    assert ranking_calls == [
        (
            [(data_b, 3.0), (data_a, 1.0)],
            {"title": "ranked_symbols", "dt": current_dt},
        )
    ]
    assert rebalance_calls == [
        {
            "target_symbols": [data_b],
            "top_k": 1,
            "rebalance_threshold": 0.05,
        }
    ]
