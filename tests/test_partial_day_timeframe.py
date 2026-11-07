"""截至指定时刻日 K 的解析、聚合与 DataManager 取数测试。"""

import pandas as pd
import pytest

from common.timeframe import aggregate_daily_cutoff, parse_daily_cutoff
from data_providers.base_provider import BaseDataProvider
from data_providers.manager import DataManager


def _minute_frame():
    first_day = pd.date_range("2024-01-02 09:31:00", periods=315, freq="min")
    second_day = pd.date_range("2024-01-03 09:31:00", periods=316, freq="min")
    index = first_day.append(second_day)
    values = pd.Series(range(len(index)), index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": values,
            "high": values + 10,
            "low": values - 10,
            "close": values + 1,
            "volume": 1.0,
            "openinterest": values,
        },
        index=index,
    )


def test_partial_day_timeframe_parser_canonicalizes_seconds():
    parsed = parse_daily_cutoff("day:14:45")

    assert parsed is not None
    assert parsed.strftime("%H:%M:%S") == "14:45:00"
    with pytest.raises(ValueError):
        parse_daily_cutoff("Day:24:00:00")


def test_partial_day_aggregation_includes_cutoff_bar_and_keeps_sessions():
    frame = _minute_frame()
    result = aggregate_daily_cutoff(
        frame,
        parse_daily_cutoff("Day:14:45:00"),
    )

    assert list(result.index) == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
    assert result.loc["2024-01-02", "open"] == 0
    assert result.loc["2024-01-02", "close"] == 315
    assert result.loc["2024-01-02", "high"] == 324
    assert result.loc["2024-01-02", "low"] == -10
    assert result.loc["2024-01-02", "volume"] == 315


def test_data_manager_fetches_minutes_then_caches_partial_day_bars(tmp_path, monkeypatch):
    import config

    source_frame = _minute_frame()
    calls = []

    class MinuteDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
            calls.append((symbol, start_date, end_date, timeframe, compression))
            return source_frame.copy()

    monkeypatch.setattr(config, "CACHE_DATA", True)
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(DataManager, "auto_discover_and_sort_providers", lambda self: [MinuteDataProvider()])

    manager = DataManager()
    result = manager.get_data(
        "SHSE.510300",
        start_date="20240102",
        end_date="20240103",
        specified_sources="minute",
        timeframe="Day:14:45:00",
    )

    assert len(result) == 2
    assert result.attrs["timeframe"] == "Day:14:45:00"
    assert calls and calls[0][3:] == ("Minutes", 5)
    assert calls[0][2] == "2024-01-03 14:45:00"
    assert list((tmp_path / "market_cache").glob("*Day_14_45_00*"))

    class FailingMinuteDataProvider(MinuteDataProvider):
        def get_data(self, *args, **kwargs):
            raise AssertionError("完整的 partial-day 缓存不应再次访问 Provider")

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self: [FailingMinuteDataProvider()],
    )
    cached_manager = DataManager()
    cached = cached_manager.get_data(
        "SHSE.510300",
        start_date="20240102",
        end_date="20240103",
        specified_sources="minute",
        timeframe="Day:14:45:00",
    )

    assert cached.attrs["data_origin"] == "cache"
    pd.testing.assert_frame_equal(
        cached[result.columns].reset_index(drop=True),
        result[result.columns].reset_index(drop=True),
    )


def test_partial_day_rejects_compression_other_than_one(monkeypatch):
    monkeypatch.setattr(DataManager, "auto_discover_and_sort_providers", lambda self: [])
    manager = DataManager()

    with pytest.raises(ValueError, match="compression=1"):
        manager.get_data("SHSE.510300", timeframe="Day:14:45:00", compression=5)


def test_backtester_allows_next_open_for_partial_day_timeframe():
    from backtest.backtester import Backtester

    class NoopStrategy:
        option_universe = None
        option_settlement = None

    tester = Backtester(
        datas={},
        strategy_class=NoopStrategy,
        timeframe="Day:14:45:00",
        compression=1,
        execution_price="next_open",
        enable_plot=False,
        verbose=False,
    )

    assert tester.execution_price == "next_open"


def test_gm_intraday_history_is_requested_in_bounded_chunks(monkeypatch):
    import data_providers.gm_provider as gm_module

    calls = []

    def fake_history(**kwargs):
        calls.append(kwargs)
        start = pd.Timestamp(kwargs["start_time"])
        return pd.DataFrame(
            {
                "open": [1.0], "high": [1.1], "low": [0.9],
                "close": [1.0], "volume": [10.0], "eob": [start],
            }
        )

    monkeypatch.setattr(gm_module, "history", fake_history)
    monkeypatch.setattr(gm_module, "set_option", lambda **kwargs: None)
    provider = gm_module.GmDataProvider(token="historical-token")

    result = provider.get_data(
        "SHSE.510300",
        start_date="2024-01-01",
        end_date="2024-01-15",
        timeframe="Minutes",
        compression=1,
    )

    assert result is not None
    assert len(calls) == 2
    assert all(call["frequency"] == "60s" for call in calls)
    assert calls[0]["end_time"] == "2024-01-14 23:59:59"
    assert calls[1]["start_time"] == "2024-01-15 00:00:00"


def test_akshare_provider_uses_intraday_endpoint_for_minute_requests(monkeypatch):
    import data_providers.akshare_provider as ak_module

    calls = []

    def fake_minute(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            {
                "时间": ["2026-11-04 14:45:00"],
                "开盘": [4.1], "最高": [4.2], "最低": [4.0],
                "收盘": [4.15], "成交量": [1000],
            }
        )

    monkeypatch.setattr(ak_module.ak, "fund_etf_hist_min_em", fake_minute)
    provider = ak_module.AkshareDataProvider()
    result = provider.get_data(
        "SHSE.510300",
        start_date="20261104",
        end_date="20261104",
        timeframe="Minutes",
        compression=1,
    )

    assert calls[0]["period"] == "1"
    assert calls[0]["start_date"] == "2026-11-04 09:30:00"
    assert result.loc[pd.Timestamp("2026-11-04 14:45:00"), "close"] == 4.15


def test_partial_day_rejects_provider_window_that_starts_too_late(monkeypatch):
    class LateProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
            index = pd.DatetimeIndex(["2026-10-08 09:35:00", "2026-10-12 09:35:00"])
            return pd.DataFrame(
                {
                    "open": [1.0, 1.0], "high": [1.1, 1.1], "low": [0.9, 0.9],
                    "close": [1.0, 1.0], "volume": [10.0, 10.0],
                },
                index=index,
            )

    monkeypatch.setattr(DataManager, "auto_discover_and_sort_providers", lambda self: [LateProvider()])
    manager = DataManager()

    assert manager.get_data(
        "SHSE.510300",
        start_date="2026-10-01",
        end_date="2026-10-12",
        specified_sources="late",
        timeframe="Day:14:45:00",
    ) is None


def test_partial_day_rejects_provider_window_that_ends_a_week_early(monkeypatch):
    class StaleProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
            index = pd.DatetimeIndex(["2026-10-01 09:35:00", "2026-10-05 09:35:00"])
            return pd.DataFrame(
                {
                    "open": [1.0, 1.0], "high": [1.1, 1.1], "low": [0.9, 0.9],
                    "close": [1.0, 1.0], "volume": [10.0, 10.0],
                },
                index=index,
            )

    monkeypatch.setattr(DataManager, "auto_discover_and_sort_providers", lambda self: [StaleProvider()])
    manager = DataManager()

    assert manager.get_data(
        "SHSE.510300",
        start_date="2026-10-01",
        end_date="2026-10-12",
        specified_sources="stale",
        timeframe="Day:14:45:00",
    ) is None


def test_partial_day_does_not_reuse_cache_that_starts_a_week_late(tmp_path, monkeypatch):
    import config

    calls = []

    class MinuteDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
            calls.append((timeframe, start_date, end_date))
            return None

    monkeypatch.setattr(config, "CACHE_DATA", True)
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(DataManager, "auto_discover_and_sort_providers", lambda self: [MinuteDataProvider()])
    manager = DataManager()
    cached = pd.DataFrame(
        {
            "open": [1.0], "high": [1.1], "low": [0.9],
            "close": [1.0], "volume": [10.0],
        },
        index=pd.DatetimeIndex(["2026-10-08"]),
    )
    manager._cache_data(cached, "SHSE.510300", "Day:14:45:00", 1)

    result = manager.get_data(
        "SHSE.510300",
        start_date="2026-10-01",
        end_date="2026-10-12",
        specified_sources="minute",
        timeframe="Day:14:45:00",
    )

    assert result is None
    assert calls and calls[0][0] == "Minutes"
