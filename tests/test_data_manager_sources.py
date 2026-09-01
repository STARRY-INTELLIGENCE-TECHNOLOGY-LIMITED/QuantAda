import pandas as pd

from data_providers.base_provider import BaseDataProvider
from data_providers.manager import DataManager, resolve_platform_default_source


def test_platform_default_data_source_is_owned_by_data_providers():
    assert resolve_platform_default_source("ib") == "ibkr"
    assert resolve_platform_default_source("ib_broker") == "ibkr"
    assert resolve_platform_default_source("gm") == "gm"
    assert resolve_platform_default_source("unknown") == ""


def test_data_manager_normalizes_platform_source_aliases():
    assert DataManager._split_source_names("ib tiingo") == ["ibkr", "tiingo"]
    assert DataManager._split_source_names("gmi") == ["gmi"]
    assert DataManager._split_source_names("futu") == ["futu"]


def test_data_manager_applies_runtime_token_only_to_selected_provider():
    class GmDataProvider:
        is_external_mode = True
        token = "EXTERNAL_MODE"

    class OtherDataProvider:
        is_external_mode = True
        token = "EXTERNAL_MODE"

    gm_provider = GmDataProvider()
    other_provider = OtherDataProvider()
    manager = object.__new__(DataManager)
    manager.providers = [gm_provider, other_provider]

    assert manager.apply_runtime_token("runtime-token", specified_sources="gm") is True
    assert gm_provider.token == "runtime-token"
    assert gm_provider.is_external_mode is False
    assert other_provider.token == "EXTERNAL_MODE"
    assert other_provider.is_external_mode is True


def test_data_manager_parses_comma_separated_sources(monkeypatch):
    calls = []

    class TiingoDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            calls.append("tiingo")
            return None

    class AkshareDataProvider(BaseDataProvider):
        PRIORITY = 2

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            calls.append("akshare")
            idx = pd.date_range("2026-01-10", periods=3, freq="D")
            return pd.DataFrame(
                {
                    "open": [10.0, 10.1, 10.2],
                    "high": [10.2, 10.3, 10.4],
                    "low": [9.8, 9.9, 10.0],
                    "close": [10.0, 10.1, 10.2],
                    "volume": [10000, 10000, 10000],
                },
                index=idx,
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [TiingoDataProvider(), AkshareDataProvider()],
    )

    dm = DataManager()
    df = dm.get_data(
        "AAPL",
        start_date="2026-01-01",
        end_date="2026-02-01",
        specified_sources="tiingo, akshare",
    )

    assert calls == ["tiingo", "akshare"], "Comma-separated data_source should be tried in order."
    assert df is not None and not df.empty, "Should return data from the available provider."


def test_data_manager_keeps_intraday_bars_for_date_only_end_boundary(monkeypatch):
    class IntradayDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            return pd.DataFrame(
                {
                    "open": [10.0, 10.1],
                    "high": [10.2, 10.3],
                    "low": [9.8, 9.9],
                    "close": [10.1, 10.2],
                    "volume": [1000, 1100],
                },
                index=pd.to_datetime(["2024-01-02 09:30:00", "2024-01-02 15:00:00"]),
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [IntradayDataProvider()],
    )

    dm = DataManager()
    df = dm.get_data(
        "US.AAPL",
        start_date="20240102",
        end_date="20240102",
        specified_sources="intraday",
        timeframe="Minutes",
    )

    assert df is not None
    assert list(df.index) == [
        pd.Timestamp("2024-01-02 09:30:00"),
        pd.Timestamp("2024-01-02 15:00:00"),
    ]
