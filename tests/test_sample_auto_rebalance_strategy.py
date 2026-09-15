from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from strategies.sample_auto_rebalance_strategy import SampleAutoRebalanceStrategy


class DummyData:
    def __init__(self, name, df=None):
        self._name = name
        if df is not None:
            self.p = SimpleNamespace(dataname=df)


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
            "rebalance_when": "daily",
        }
    ]


def _price_frame(closes, start="2026-01-01", tz=None):
    index = pd.date_range(start, periods=len(closes), freq="D", tz=tz)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=index,
    )


def test_sample_auto_rebalance_rebuilds_roc_after_live_dataname_refresh(monkeypatch):
    current_dt = datetime(2026, 1, 6)
    # 5 根 K，roc_period=2：刷新前 BBB 更强，刷新后 AAA 今日大涨应反超。
    data_a = DummyData("AAA", _price_frame([10.0, 10.0, 10.2, 10.3, 10.4]))
    data_b = DummyData("BBB", _price_frame([10.0, 11.0, 12.0, 13.0, 14.0]))
    broker = DummyBroker([data_a, data_b], current_dt)
    strategy = SampleAutoRebalanceStrategy(
        broker=broker,
        params={"selectTopK": 1, "rebalance_threshold": 0.05, "roc_period": 2},
    )
    strategy.init()

    ranking_calls = []
    monkeypatch.setattr(
        strategy,
        "publish_rankings",
        lambda candidates, **kwargs: ranking_calls.append([item[0]._name for item in candidates]) or True,
    )
    monkeypatch.setattr(strategy, "execute_rebalance", lambda **kwargs: None)

    strategy.next()
    assert ranking_calls[-1][0] == "BBB"

    data_a.p.dataname = _price_frame([10.0, 10.0, 10.2, 10.3, 20.0])
    broker.current_dt = datetime(2026, 1, 6, 14, 45)
    strategy.next()
    assert ranking_calls[-1][0] == "AAA"


def test_sample_auto_rebalance_scores_tz_aware_index_with_naive_clock(monkeypatch):
    current_dt = datetime(2026, 1, 6, 14, 45)
    data_a = DummyData("AAA", _price_frame([10.0, 10.5, 11.0, 11.5, 12.0], tz="UTC"))
    broker = DummyBroker([data_a], current_dt)
    broker.is_live = True
    strategy = SampleAutoRebalanceStrategy(
        broker=broker,
        params={"selectTopK": 1, "rebalance_threshold": 0.05, "roc_period": 2},
    )
    strategy.init()

    ranking_calls = []
    rebalance_calls = []
    monkeypatch.setattr(
        strategy,
        "publish_rankings",
        lambda candidates, **kwargs: ranking_calls.append(list(candidates)) or True,
    )
    monkeypatch.setattr(
        strategy,
        "execute_rebalance",
        lambda **kwargs: rebalance_calls.append(kwargs),
    )

    strategy.next()

    assert ranking_calls, "时区索引不应让 asof 静默失败。"
    assert ranking_calls[0], "正动量标的应进入候选，不能因 tz 比较失败被当成空池清仓。"
    assert rebalance_calls[0]["target_symbols"] == [data_a]
