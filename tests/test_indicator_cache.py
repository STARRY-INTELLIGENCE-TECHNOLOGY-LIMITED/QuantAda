from types import SimpleNamespace

import pandas as pd

from common import indicator_cache


class _DummyDateTime:
    def __init__(self, dt):
        self._dt = dt

    def datetime(self, index):
        if index != 0:
            raise IndexError(index)
        return self._dt


class _DummyData:
    def __init__(self, name, dataframe, current_dt):
        self._name = name
        self.p = SimpleNamespace(dataname=dataframe)
        self.datetime = _DummyDateTime(current_dt)


class _DummyBroker:
    def __init__(self, is_live=False, indicator_cache_obj=None):
        self.is_live = is_live
        self.indicator_cache = indicator_cache_obj


class _DummyStrategy:
    def __init__(self, broker):
        self.broker = broker


def test_register_indicator_uses_fast_dict_for_backtest_lookup():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    series = pd.Series([1.0, 2.0, 3.0], index=idx)
    strategy = _DummyStrategy(_DummyBroker(is_live=False, indicator_cache_obj={}))
    data = _DummyData("AAA", pd.DataFrame(index=idx), idx[1].to_pydatetime())

    indicator_cache.register_indicator(strategy, "AAA", "score", series)

    assert indicator_cache.get_indicator(strategy, data, "score", idx[1].to_pydatetime()) == 2.0
    assert strategy._fast_dict_registry["AAA"]["score"] is not None


def test_live_indicator_lookup_uses_series_asof_without_fast_dict_cache():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    series = pd.Series([1.0, 2.0, 3.0], index=idx)
    strategy = _DummyStrategy(_DummyBroker(is_live=True, indicator_cache_obj={}))
    data = SimpleNamespace(_name="AAA")

    indicator_cache.register_indicator(strategy, "AAA", "score", series)

    got = indicator_cache.get_indicator(strategy, data, "score", pd.Timestamp("2024-01-02 12:00:00"))

    assert got == 2.0
    assert strategy._fast_dict_registry["AAA"] == {}


def test_get_cached_indicator_series_reuses_optimizer_cache_only_offline():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    dataframe = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    data = _DummyData("AAA", dataframe, idx[-1].to_pydatetime())
    cache = {}
    strategy = _DummyStrategy(_DummyBroker(is_live=False, indicator_cache_obj=cache))
    calls = {"count": 0}

    def compute():
        calls["count"] += 1
        return pd.Series([10.0, 20.0, 30.0], index=idx)

    first = indicator_cache.get_cached_indicator_series(strategy, data, "score", (5,), compute)
    second = indicator_cache.get_cached_indicator_series(strategy, data, "score", (5,), compute)

    assert first is second
    assert calls["count"] == 1


def test_get_cached_indicator_series_recomputes_in_live_mode():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    dataframe = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    data = _DummyData("AAA", dataframe, idx[-1].to_pydatetime())
    strategy = _DummyStrategy(_DummyBroker(is_live=True, indicator_cache_obj={}))
    calls = {"count": 0}

    def compute():
        calls["count"] += 1
        return pd.Series([10.0, 20.0, 30.0], index=idx)

    indicator_cache.get_cached_indicator_series(strategy, data, "score", (5,), compute)
    indicator_cache.get_cached_indicator_series(strategy, data, "score", (5,), compute)

    assert calls["count"] == 2


def test_bounded_indicator_cache_evicts_oldest_entry():
    cache = indicator_cache.BoundedIndicatorCache(max_entries=2)

    cache["a"] = 1
    cache["b"] = 2
    assert cache.get("a") == 1
    cache["c"] = 3

    assert "a" in cache
    assert "b" not in cache
    assert "c" in cache
