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
    assert DataManager._split_source_names("sxsc_tushare") == ["sxsctushare"]
    assert DataManager._split_source_names("theta + futu") == ["hybrid"]


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


def test_cache_data_removes_nonfinite_rows_from_existing_cache(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'CACHE_DATA', True)
    manager = object.__new__(DataManager)
    manager.data_path = str(tmp_path)
    path = tmp_path / 'US_AAPL.csv'
    path.write_text(
        'datetime,open,high,low,close,volume\n'
        '2024-01-01,1,2,0.5,inf,10\n'
        '2024-01-02,2,3,1,2.5,20\n',
        encoding='utf-8',
    )
    incoming = pd.DataFrame(
        {'open': [3], 'high': [4], 'low': [2], 'close': [3.5], 'volume': [30]},
        index=pd.to_datetime(['2024-01-03']),
    )

    manager._cache_data(incoming, 'US.AAPL')

    cached = pd.read_csv(path)
    assert cached['datetime'].tolist() == ['2024-01-02', '2024-01-03']
    assert cached[['open', 'high', 'low', 'close', 'volume']].notna().all().all()


def test_hybrid_provider_result_is_not_written_to_persistent_cache(monkeypatch):
    class HybridProvider(BaseDataProvider):
        HYBRID_ONLY = True

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            return pd.DataFrame(
                {'open': [1], 'high': [1], 'low': [1], 'close': [1], 'volume': [1]},
                index=pd.to_datetime(['2024-01-01']),
            )

    manager = object.__new__(DataManager)
    writes = []
    manager._cache_data = lambda *args, **kwargs: writes.append(True)

    result = manager._fetch_from_providers(
        'US.AAPL', '20240101', '20240101', [HybridProvider()], 'Days', 1
    )

    assert result is not None
    assert writes == []


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


def test_live_mode_bypasses_complete_cache_for_explicit_online_source(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, 'CACHE_DATA', True)
    monkeypatch.setattr(config, 'DATA_PATH', str(tmp_path))
    pd.DataFrame(
        {'open': [1], 'high': [1], 'low': [1], 'close': [1], 'volume': [1]},
        index=pd.to_datetime(['2024-01-01']),
    ).rename_axis('datetime').to_csv(tmp_path / 'US_AAPL.csv')

    class OnlineProvider(BaseDataProvider):
        PRIORITY = 1

        def __init__(self):
            self.calls = 0

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            self.calls += 1
            return pd.DataFrame(
                {'open': [2], 'high': [2], 'low': [2], 'close': [2], 'volume': [2]},
                index=pd.to_datetime(['2024-01-01']),
            )

    provider = OnlineProvider()
    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [provider],
    )
    manager = DataManager()
    manager.set_live_mode(True)

    result = manager.get_data(
        'US.AAPL', '20240101', '20240101', specified_sources='onlineprovider'
    )

    assert provider.calls == 1
    assert float(result.iloc[0]['close']) == 2


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


def test_data_manager_close_does_not_call_dynamic_provider_attribute():
    class DynamicProvider:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            self.calls.append(name)
            return lambda: None

    provider = DynamicProvider()
    manager = object.__new__(DataManager)
    manager.providers = [provider]
    manager._composed_providers = {}

    manager.close()

    assert provider.calls == []


def test_data_manager_runtime_injection_does_not_call_dynamic_provider_attribute():
    class DynamicProvider:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            self.calls.append(name)
            return lambda *_args, **_kwargs: None

    provider = DynamicProvider()
    manager = object.__new__(DataManager)
    manager.providers = [provider]
    manager._composed_providers = {}
    manager.set_live_mode(True)
    manager.bind_broker(object())
    manager.apply_runtime_token('token')

    assert provider.calls == []


def test_data_manager_skips_failed_composition_and_uses_fallback(monkeypatch):
    class FallbackDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            return pd.DataFrame(
                {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
                index=pd.date_range("2026-01-01", periods=1),
            )

    manager = object.__new__(DataManager)
    manager.providers = [FallbackDataProvider()]
    manager.provider_map = {"fallback": manager.providers[0]}
    manager._composed_providers = {}
    manager._live_mode = False
    manager._bound_broker = None
    monkeypatch.setattr(
        "config.DATA_PROVIDER_COMPOSITIONS",
        {
            "hybrid": {
                "historical": "missing_theta",
                "realtime": "missing_futu",
                "factory": "data_providers.hybrid_provider:HybridDataProvider",
            }
        },
    )

    result = manager.get_data(
        "US.AAPL", "20260101", "20260101", specified_sources="hybrid,fallback"
    )

    assert result is not None
    assert result.iloc[0]["close"] == 1


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
