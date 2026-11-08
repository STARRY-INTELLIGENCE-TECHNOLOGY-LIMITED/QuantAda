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


def test_live_indicator_lookup_normalizes_python_datetime_microseconds():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    series = pd.Series([1.0, 2.0, 3.0], index=idx)
    strategy = _DummyStrategy(_DummyBroker(is_live=True, indicator_cache_obj={}))
    data = SimpleNamespace(_name="AAA")

    indicator_cache.register_indicator(strategy, "AAA", "score", series)

    got = indicator_cache.get_indicator(
        strategy,
        data,
        "score",
        pd.Timestamp("2024-01-02 12:34:56.789123").to_pydatetime(),
    )

    assert got == 2.0


def test_live_indicator_lookup_converts_current_time_to_series_timezone():
    idx = pd.date_range("2024-01-02 14:30:00", periods=2, freq="min", tz="UTC")
    series = pd.Series([1.0, 2.0], index=idx)
    strategy = _DummyStrategy(_DummyBroker(is_live=True, indicator_cache_obj={}))
    data = SimpleNamespace(_name="AAA")

    indicator_cache.register_indicator(strategy, "AAA", "score", series)

    got = indicator_cache.get_indicator(
        strategy,
        data,
        "score",
        pd.Timestamp("2024-01-02 09:30:30", tz="America/New_York"),
    )

    assert got == 1.0



def test_live_indicator_lookup_converts_aware_clock_to_naive_series():
    idx = pd.date_range("2024-01-02 14:30:00", periods=2, freq="min")
    series = pd.Series([1.0, 2.0], index=idx)
    strategy = _DummyStrategy(_DummyBroker(is_live=True, indicator_cache_obj={}))
    data = SimpleNamespace(_name="AAA")

    indicator_cache.register_indicator(strategy, "AAA", "score", series)

    got = indicator_cache.get_indicator(
        strategy,
        data,
        "score",
        pd.Timestamp("2024-01-02 14:30:30", tz="UTC"),
    )

    assert got == 1.0


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


def test_get_cached_indicator_series_ignores_entry_after_dataframe_id_reuse(monkeypatch):
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    old_frame = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    new_frame = pd.DataFrame({"close": [7.0, 8.0, 9.0]}, index=idx)
    cache = {}
    strategy = _DummyStrategy(_DummyBroker(is_live=False, indicator_cache_obj=cache))
    # 模拟原 DataFrame 回收后 id 被新对象复用：两张表得到相同 id，缓存键完全一致。
    monkeypatch.setattr(indicator_cache, "id", lambda obj: 42, raising=False)

    stale = indicator_cache.get_cached_indicator_series(
        strategy, _DummyData("AAA", old_frame, idx[-1].to_pydatetime()), "score", (5,),
        lambda: pd.Series([1.0, 1.0, 1.0], index=idx),
    )
    del old_frame
    fresh = indicator_cache.get_cached_indicator_series(
        strategy, _DummyData("AAA", new_frame, idx[-1].to_pydatetime()), "score", (5,),
        lambda: pd.Series([2.0, 2.0, 2.0], index=idx),
    )

    assert stale.tolist() == [1.0, 1.0, 1.0]
    assert fresh.tolist() == [2.0, 2.0, 2.0]
    assert len(cache) == 1


def test_get_cached_indicator_series_does_not_retain_dataframe():
    import gc
    import weakref

    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    dataframe = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    probe = weakref.ref(dataframe)
    cache = {}
    strategy = _DummyStrategy(_DummyBroker(is_live=False, indicator_cache_obj=cache))
    indicator_cache.get_cached_indicator_series(
        strategy, _DummyData("AAA", dataframe, idx[-1].to_pydatetime()), "score", (5,),
        lambda: pd.Series([1.0, 2.0, 3.0], index=idx),
    )
    del dataframe
    gc.collect()

    assert probe() is None


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


def test_fast_dict_cache_does_not_leak_values_between_trials():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    shared_cache = {}
    data = _DummyData("AAA", pd.DataFrame(index=idx), idx[1].to_pydatetime())

    first = _DummyStrategy(_DummyBroker(is_live=False, indicator_cache_obj=shared_cache))
    indicator_cache.register_indicator(first, "AAA", "roc", pd.Series([1.0, 2.0, 3.0], index=idx))
    second = _DummyStrategy(_DummyBroker(is_live=False, indicator_cache_obj=shared_cache))
    indicator_cache.register_indicator(second, "AAA", "roc", pd.Series([10.0, 20.0, 30.0], index=idx))

    assert indicator_cache.get_indicator(first, data, "roc", None) == 2.0
    assert indicator_cache.get_indicator(second, data, "roc", None) == 20.0


def test_fast_dict_cache_reuses_identical_content_and_rejects_key_collision():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    shared_cache = {}
    data = _DummyData("AAA", pd.DataFrame(index=idx), idx[1].to_pydatetime())
    strategy = _DummyStrategy(_DummyBroker(is_live=False, indicator_cache_obj=shared_cache))

    indicator_cache.register_indicator(strategy, "AAA", "roc", pd.Series([1.0, 2.0, 3.0], index=idx))
    first_dict = strategy._fast_dict_registry["AAA"]["roc"]
    indicator_cache.register_indicator(strategy, "AAA", "roc", pd.Series([1.0, 2.0, 3.0], index=idx))
    assert strategy._fast_dict_registry["AAA"]["roc"] is first_dict

    # 模拟键碰撞：缓存条目保存的序列与当前内容不同，必须重建而不是复用。
    (key,) = [key for key in shared_cache if key[0] == "fast_dict"]
    stale = pd.Series([7.0, 8.0, 9.0], index=idx)
    shared_cache[key] = (stale, dict(zip([dt.to_pydatetime() for dt in idx], stale.values)))
    indicator_cache.register_indicator(strategy, "AAA", "roc", pd.Series([1.0, 2.0, 3.0], index=idx))

    assert indicator_cache.get_indicator(strategy, data, "roc", None) == 2.0
