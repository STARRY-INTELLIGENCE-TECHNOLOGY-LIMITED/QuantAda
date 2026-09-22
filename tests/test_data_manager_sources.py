import pandas as pd

from data_providers.base_provider import BaseDataProvider
from data_providers.manager import DataManager, resolve_platform_default_source


def _market_cache_csv(tmp_path, filename):
    folder = tmp_path / "market_cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename



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
    cached = pd.read_csv(_market_cache_csv(tmp_path, 'US_AAPL.csv'), index_col='datetime', parse_dates=True)
    assert len(cached) == 2
    assert float(cached.loc['2024-01-01', 'close']) == 3.5


def test_cache_data_recovers_from_corrupt_file(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'CACHE_DATA', True)
    manager = object.__new__(DataManager)
    manager.data_path = str(tmp_path)
    (_market_cache_csv(tmp_path, 'US_AAPL.csv')).write_text('not,a,valid,cache\n', encoding='utf-8')
    frame = pd.DataFrame({'open': [1], 'high': [1], 'low': [1], 'close': [1], 'volume': [1]},
                         index=pd.to_datetime(['2024-01-01']))
    manager._cache_data(frame, 'US.AAPL')
    cached = pd.read_csv(_market_cache_csv(tmp_path, 'US_AAPL.csv'), index_col='datetime', parse_dates=True)
    assert len(cached) == 1


def test_cache_data_removes_nonfinite_rows_from_existing_cache(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'CACHE_DATA', True)
    manager = object.__new__(DataManager)
    manager.data_path = str(tmp_path)
    path = _market_cache_csv(tmp_path, 'US_AAPL.csv')
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


def test_hybrid_live_result_is_not_written_to_persistent_cache(monkeypatch):
    class HybridProvider(BaseDataProvider):
        HYBRID_ONLY = True

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            return pd.DataFrame(
                {'open': [1], 'high': [1], 'low': [1], 'close': [1], 'volume': [1]},
                index=pd.to_datetime(['2024-01-01']),
            )

    manager = object.__new__(DataManager)
    manager._live_mode = True
    writes = []
    manager._cache_data = lambda *args, **kwargs: writes.append(True)

    result = manager._fetch_from_providers(
        'US.AAPL', '20240101', '20240101', [HybridProvider()], 'Days', 1
    )

    assert result is not None
    assert writes == []


def test_hybrid_backtest_result_is_written_to_persistent_cache(monkeypatch):
    class HybridProvider(BaseDataProvider):
        HYBRID_ONLY = True

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            return pd.DataFrame(
                {'open': [1], 'high': [1], 'low': [1], 'close': [1], 'volume': [1]},
                index=pd.to_datetime(['2024-01-01']),
            )

    manager = object.__new__(DataManager)
    manager._live_mode = False
    writes = []
    manager._cache_data = lambda *args, **kwargs: writes.append(True)

    result = manager._fetch_from_providers(
        'US.AAPL', '20240101', '20240101', [HybridProvider()], 'Days', 1
    )

    assert result is not None
    assert writes == [True]


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
    assert (_market_cache_csv(tmp_path, 'US_AAPL240119P00150000.csv')).is_file()


def test_get_data_does_not_print_per_symbol_fetch_chatter(tmp_path, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config, "CACHE_DATA", True)
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))

    class ThetaDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            return pd.DataFrame(
                {
                    "open": [100.0], "high": [101.0], "low": [99.0],
                    "close": [100.5], "volume": [100],
                },
                index=pd.to_datetime(["2024-01-02"]),
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [ThetaDataProvider()],
    )
    manager = DataManager()
    capsys.readouterr()
    result = manager.get_data(
        "US.SPY", "20240102", "20240102", specified_sources="theta"
    )
    out = capsys.readouterr().out
    assert result is not None
    assert "Using specified data sources" not in out
    assert "Attempting to fetch data" not in out
    assert "Successfully fetched data" not in out
    assert "cached to" not in out
    assert "Filtering final data" not in out
    assert "Using complete cached data" not in out


def test_option_cache_through_expiry_covers_long_request_window(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "CACHE_DATA", True)
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    pd.DataFrame(
        {
            "open": [1.0, 1.2],
            "high": [1.3, 1.4],
            "low": [0.9, 1.1],
            "close": [1.2, 1.3],
            "volume": [10, 11],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-19"]),
    ).rename_axis("datetime").to_csv(_market_cache_csv(tmp_path, "US_AAPL240119P00150000.csv"))
    calls = []

    class ThetaDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            calls.append((symbol, start_date, end_date))
            return pd.DataFrame(
                {
                    "open": [9.0], "high": [9.0], "low": [9.0],
                    "close": [9.0], "volume": [1],
                },
                index=pd.to_datetime(["2024-01-02"]),
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [ThetaDataProvider()],
    )
    manager = DataManager()
    result = manager.get_data(
        "US.AAPL240119P00150000",
        "20220101",
        "20250105",
        specified_sources="theta",
    )

    assert calls == []
    assert result is not None
    assert list(result.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-19"]


def test_option_cache_covering_contract_life_is_complete(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "CACHE_DATA", True)
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    expiry = pd.Timestamp("2024-01-19")
    index = pd.bdate_range(expiry - pd.Timedelta(days=45), expiry)
    pd.DataFrame(
        {
            "open": [1.0] * len(index),
            "high": [1.2] * len(index),
            "low": [0.9] * len(index),
            "close": [1.1] * len(index),
            "volume": [10] * len(index),
        },
        index=index,
    ).rename_axis("datetime").to_csv(_market_cache_csv(tmp_path, "US_AAPL240119P00150000.csv"))
    calls = []

    class ThetaDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            calls.append((symbol, start_date, end_date))
            return None

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [ThetaDataProvider()],
    )
    manager = DataManager()
    result = manager.get_data(
        "US.AAPL240119P00150000",
        "20220101",
        "20250105",
        specified_sources="theta",
    )

    assert calls == []
    assert result is not None
    assert result.index.min() <= expiry - pd.Timedelta(days=45)
    assert result.index.max() >= expiry


def test_option_cache_missing_expiry_is_incomplete(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "CACHE_DATA", True)
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    pd.DataFrame(
        {
            "open": [1.0],
            "high": [1.2],
            "low": [0.9],
            "close": [1.1],
            "volume": [10],
        },
        index=pd.to_datetime(["2024-01-02"]),
    ).rename_axis("datetime").to_csv(_market_cache_csv(tmp_path, "US_AAPL240119P00150000.csv"))
    calls = []

    class ThetaDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            calls.append((symbol, start_date, end_date))
            return pd.DataFrame(
                {
                    "open": [9.0], "high": [9.0], "low": [9.0],
                    "close": [9.0], "volume": [1],
                },
                index=pd.to_datetime(["2024-01-19"]),
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [ThetaDataProvider()],
    )
    manager = DataManager()
    result = manager.get_data(
        "US.AAPL240119P00150000",
        "20220101",
        "20250105",
        specified_sources="theta",
    )

    assert calls == [("US.AAPL240119P00150000", "20220101", "20250105")]
    assert result is not None
    assert list(result.index.strftime("%Y-%m-%d")) == ["2024-01-19"]


def test_live_mode_bypasses_complete_cache_for_explicit_online_source(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, 'CACHE_DATA', True)
    monkeypatch.setattr(config, 'DATA_PATH', str(tmp_path))
    pd.DataFrame(
        {'open': [1], 'high': [1], 'low': [1], 'close': [1], 'volume': [1]},
        index=pd.to_datetime(['2024-01-01']),
    ).rename_axis('datetime').to_csv(_market_cache_csv(tmp_path, 'US_AAPL.csv'))

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
    ).rename_axis('datetime').to_csv(_market_cache_csv(tmp_path, 'US_AAPL.csv'))

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


def test_base_provider_close_defaults_to_noop():
    class StatelessProvider(BaseDataProvider):
        def get_data(self, *args, **kwargs):
            return None

    assert StatelessProvider().close() is None


def test_data_manager_closes_short_lived_provider_without_closing_futu():
    class ThetaDataProvider(BaseDataProvider):
        def __init__(self):
            self.closed = 0

        def get_data(self, *args, **kwargs):
            return None

        def close(self):
            self.closed += 1

    class FutuDataProvider(BaseDataProvider):
        def __init__(self):
            self.closed = 0

        def get_data(self, *args, **kwargs):
            return None

    class HybridDataProvider:
        def __init__(self, theta):
            self.theta_provider = theta

    theta = ThetaDataProvider()
    futu = FutuDataProvider()
    manager = object.__new__(DataManager)
    manager.providers = [theta, futu]
    manager._composed_providers = {"hybrid": HybridDataProvider(theta)}

    assert manager.close_after_fetch() == 1
    assert theta.closed == 1
    assert futu.closed == 0


def test_data_manager_close_after_fetch_preserves_injected_futu_context():
    from data_providers.futu_provider import FutuDataProvider

    class InjectedContext:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    context = InjectedContext()
    provider = FutuDataProvider(quote_ctx=context)
    manager = object.__new__(DataManager)
    manager.providers = [provider]
    manager._composed_providers = {}

    assert manager.close_after_fetch() == 1
    assert context.closed is False


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


def test_daily_cache_treats_weekend_end_as_complete(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "CACHE_DATA", True)
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10, 20],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-05"]),
    ).rename_axis("datetime").to_csv(_market_cache_csv(tmp_path, "US_SPY.csv"))
    calls = []

    class FutuDataProvider(BaseDataProvider):
        PRIORITY = 1

        def get_data(self, symbol, start_date=None, end_date=None,
                     timeframe="Days", compression=1):
            calls.append((symbol, start_date, end_date))
            return pd.DataFrame(
                {"open": [9], "high": [9], "low": [9], "close": [9], "volume": [1]},
                index=pd.to_datetime(["2024-01-05"]),
            )

    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self, provider_dir=None: [FutuDataProvider()],
    )
    manager = DataManager()
    result = manager.get_data(
        "US.SPY",
        "20240102",
        "20240106",
        specified_sources="futu",
    )
    assert calls == []
    assert result is not None
    assert list(result.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-05"]


def test_cache_write_creates_missing_data_path_and_subdir(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "CACHE_DATA", True)
    missing = tmp_path / "missing-parent" / "runtime-data"
    manager = object.__new__(DataManager)
    manager.data_path = str(missing)
    frame = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
        index=pd.to_datetime(["2024-01-01"]),
    )
    manager._cache_data(frame, "US.AAPL")
    cached = missing / "market_cache" / "US_AAPL.csv"
    assert cached.is_file()
