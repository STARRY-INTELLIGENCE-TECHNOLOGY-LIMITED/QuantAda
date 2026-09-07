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
    assert DataManager._split_source_names("thetadata") == ["theta"]


def test_cache_data_merges_segments_without_duplicate_timestamps(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'CACHE_DATA', True)
    manager = object.__new__(DataManager)
    manager.data_path = str(tmp_path)
    first = pd.DataFrame({'open': [1], 'high': [2], 'low': [0.5], 'close': [1.5], 'volume': [10]},
                         index=pd.to_datetime(['2024-01-01']))
    second = pd.DataFrame({'open': [3, 4], 'high': [4, 5], 'low': [2, 3], 'close': [3.5, 4.5], 'volume': [20, 30]},
                          index=pd.to_datetime(['2024-01-01', '2024-01-02']))
    manager._cache_data(first, 'US.AAPL')
    manager._cache_data(second, 'US.AAPL')
    cached = pd.read_csv(tmp_path / 'US_AAPL.csv', index_col='datetime', parse_dates=True)
    assert len(cached) == 2
    assert float(cached.loc['2024-01-01', 'close']) == 3.5


def test_cache_data_recovers_from_corrupt_file(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'CACHE_DATA', True)
    manager = object.__new__(DataManager)
    manager.data_path = str(tmp_path)
    (tmp_path / 'US_AAPL.csv').write_text('not,a,valid,cache\n', encoding='utf-8')
    frame = pd.DataFrame({'open': [1], 'high': [1], 'low': [1], 'close': [1], 'volume': [1]},
                         index=pd.to_datetime(['2024-01-01']))
    manager._cache_data(frame, 'US.AAPL')
    cached = pd.read_csv(tmp_path / 'US_AAPL.csv', index_col='datetime', parse_dates=True)
    assert len(cached) == 1


def test_single_online_source_reuses_complete_cache_and_refresh_bypasses_it(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, 'CACHE_DATA', True)
    monkeypatch.setattr(config, 'DATA_PATH', str(tmp_path))
    calls = []

    class ThetaDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            calls.append((symbol, start_date, end_date, timeframe, compression))
            return pd.DataFrame(
                {
                    'open': [100.0, 101.0], 'high': [101.0, 102.0],
                    'low': [99.0, 100.0], 'close': [100.5, 101.5],
                    'volume': [100, 110], 'contract_multiplier': [100.0, 100.0],
                },
                index=pd.to_datetime(['2024-01-02', '2024-01-03']),
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [ThetaDataProvider()],
    )

    manager = DataManager()
    first = manager.get_data(
        'US.AAPL240119P00150000', '20240102', '20240103', specified_sources='theta'
    )
    second = manager.get_data(
        'US.AAPL240119P00150000', '20240102', '20240103', specified_sources='theta'
    )
    refreshed = manager.get_data(
        'US.AAPL240119P00150000', '20240102', '20240103',
        specified_sources='theta', refresh=True,
    )

    assert first is not None and second is not None and refreshed is not None
    assert len(calls) == 2
    pd.testing.assert_frame_equal(first, second, check_names=False)
    assert (tmp_path / 'US_AAPL240119P00150000.csv').is_file()


def test_incomplete_cache_falls_back_to_online_source(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, 'CACHE_DATA', True)
    monkeypatch.setattr(config, 'DATA_PATH', str(tmp_path))
    pd.DataFrame(
        {'open': [1], 'high': [1], 'low': [1], 'close': [1], 'volume': [1]},
        index=pd.to_datetime(['2024-01-03']),
    ).rename_axis('datetime').to_csv(tmp_path / 'US_AAPL.csv')

    class ThetaDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            return pd.DataFrame(
                {'open': [1, 2], 'high': [1, 2], 'low': [1, 2],
                 'close': [1, 2], 'volume': [1, 2]},
                index=pd.to_datetime(['2024-01-02', '2024-01-03']),
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [ThetaDataProvider()],
    )
    manager = DataManager()

    result = manager.get_data(
        'US.AAPL', '20240102', '20240103', specified_sources='theta'
    )

    assert result is not None
    assert list(result['close']) == [1, 2]


def test_intraday_date_only_cache_uses_natural_day_coverage(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, 'CACHE_DATA', True)
    monkeypatch.setattr(config, 'DATA_PATH', str(tmp_path))
    calls = []

    class IntradayDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            calls.append(True)
            return pd.DataFrame(
                {'open': [1, 2], 'high': [1, 2], 'low': [1, 2],
                 'close': [1, 2], 'volume': [1, 2]},
                index=pd.to_datetime([
                    '2024-01-02 09:30:00', '2024-01-02 15:55:00',
                ]),
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [IntradayDataProvider()],
    )
    manager = DataManager()

    first = manager.get_data(
        'US.AAPL', '20240102', '20240102',
        specified_sources='intraday', timeframe='Minutes', compression=5,
    )
    second = manager.get_data(
        'US.AAPL', '20240102', '20240102',
        specified_sources='intraday', timeframe='Minutes', compression=5,
    )

    assert first is not None and second is not None
    assert len(calls) == 1


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


def test_theta_provider_accepts_runtime_token_without_network(monkeypatch):
    from data_providers.thetadata_provider import ThetaDataProvider

    provider = ThetaDataProvider()
    manager = object.__new__(DataManager)
    manager.providers = [provider]
    assert provider.is_external_mode is True
    assert manager.apply_runtime_token('runtime-theta-token', specified_sources='theta') is True
    assert provider.token == 'runtime-theta-token'
    assert provider.is_external_mode is False


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
