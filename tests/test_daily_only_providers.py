"""只实现日线的 Provider 遇到其它周期必须返回 None，让责任链回退而不是把日线写成分钟缓存。"""

import pandas as pd
import pytest


class _ExplodingApi:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected API call: {name}")


@pytest.mark.parametrize(
    "timeframe, compression",
    [("Minutes", 5), ("Seconds", 1), ("Weeks", 1), ("Days", 2)],
)
def test_tushare_skips_non_daily_requests(timeframe, compression):
    import data_providers.tushare_provider as module

    provider = object.__new__(module.TushareDataProvider)
    provider.pro = _ExplodingApi()

    assert provider.get_data("SHSE.600519", "20240101", "20240131", timeframe, compression) is None


@pytest.mark.parametrize(
    "timeframe, compression",
    [("Minutes", 5), ("Weeks", 1), ("Days", 2)],
)
def test_sxsc_tushare_skips_non_daily_requests(monkeypatch, timeframe, compression):
    import data_providers.sxsctushare_provider as module

    provider = object.__new__(module.SxscTushareDataProvider)
    provider.token = "token"
    monkeypatch.setattr(module.requests, "post", lambda *a, **k: pytest.fail("unexpected request"))

    assert provider.get_data("SHSE.600519", "20240101", "20240131", timeframe, compression) is None


@pytest.mark.parametrize(
    "timeframe, compression",
    [("Minutes", 5), ("Weeks", 1), ("Days", 2)],
)
def test_tiingo_skips_non_daily_requests(timeframe, compression):
    import data_providers.tiingo_provider as module

    provider = object.__new__(module.TiingoDataProvider)
    provider.client = _ExplodingApi()

    assert provider.get_data("AAPL", "2024-01-01", "2024-01-31", timeframe, compression) is None


@pytest.mark.parametrize(
    "symbol, timeframe, compression",
    [("SHSE.600519", "Seconds", 1), ("SHSE.600519", "Days", 2), ("SHSE.510300", "Weeks", 1)],
)
def test_akshare_rejects_unsupported_or_daily_fallback_periods(monkeypatch, symbol, timeframe, compression):
    import data_providers.akshare_provider as module

    monkeypatch.setattr(module, "ak", _ExplodingApi())
    provider = module.AkshareDataProvider()

    assert provider.get_data(symbol, "20240101", "20240131", timeframe, compression) is None


def test_akshare_still_serves_native_weekly_stock_bars(monkeypatch):
    import data_providers.akshare_provider as module

    calls = []

    class Api:
        def stock_zh_a_hist(self, **kwargs):
            calls.append(kwargs)
            return pd.DataFrame({
                "日期": ["2024-01-05"], "开盘": [1.0], "最高": [1.2], "最低": [0.9],
                "收盘": [1.1], "成交量": [100.0],
            })

    monkeypatch.setattr(module, "ak", Api())

    result = module.AkshareDataProvider().get_data("SHSE.600519", "20240101", "20240131", "Weeks", 1)

    assert calls[0]["period"] == "weekly"
    assert len(result) == 1
