import pandas as pd

from data_providers.hybrid_provider import HybridDataProvider
from data_providers.manager import DataManager
from data_providers.overlay_provider import OverlayDataProvider


def _historical_option_frame():
    index = pd.date_range("2026-08-01", periods=25, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.0] * 25,
            "high": [1.2] * 25,
            "low": [0.8] * 25,
            "close": [1.0] * 25,
            "volume": [100.0] * 25,
            "option_type": ["PUT"] * 25,
            "strike": [300.0] * 25,
            "expiry": ["2026-10-16"] * 25,
            "iv": [0.20 + index_no * 0.001 for index_no in range(25)],
            "contract_multiplier": [100.0] * 25,
        },
        index=index,
    )


def test_hybrid_provider_keeps_backtest_path_theta_only():
    class Theta:
        def __init__(self):
            self.calls = 0

        def get_data(self, *args):
            self.calls += 1
            return _historical_option_frame()

    class Futu:
        def __init__(self):
            self.calls = 0

        def get_market_snapshot(self, _symbols):
            self.calls += 1
            return pd.DataFrame()

    theta = Theta()
    futu = Futu()
    provider = HybridDataProvider(theta_provider=theta, futu_provider=futu)

    result = provider.get_data("US.AAPL261016P300000", "20260801", "20260910")

    assert result is not None
    assert theta.calls == 1
    assert futu.calls == 0


def test_hybrid_provider_merges_futu_live_quote_and_recomputes_ivp():
    class Theta:
        def __init__(self):
            self.calls = 0

        def get_data(self, *args):
            self.calls += 1
            return _historical_option_frame()

    class Futu:
        def get_market_snapshot(self, _symbols):
            timestamp = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S")
            return pd.DataFrame([{
                "code": "US.AAPL261016P300000",
                "update_time": timestamp,
                "open_price": 2.0,
                "high_price": 2.2,
                "low_price": 1.8,
                "last_price": 2.1,
                "bid_price": 2.0,
                "ask_price": 2.2,
                "volume": 500,
                "option_type": "PUT",
                "option_strike_price": 300.0,
                "strike_time": "2026-10-16",
                "option_implied_volatility": 25.0,
                "option_delta": -0.14,
                "option_gamma": 0.01,
                "option_open_interest": 10000,
                "option_contract_multiplier": 100.0,
            }])

    theta = Theta()
    provider = HybridDataProvider(theta_provider=theta, futu_provider=Futu())
    provider.set_live_mode(True)

    result = provider.get_data("US.AAPL261016P300000", "20260801", "20260910")
    row = result.iloc[-1]

    assert row["close"] == 2.1
    assert row["bid"] == 2.0
    assert row["ask"] == 2.2
    assert row["delta"] == -0.14
    assert row["contract_multiplier"] == 100.0
    assert pd.notna(row["iv_percentile"])
    provider.get_data("US.AAPL261016P300000", "20260801", "20260910")
    assert theta.calls == 1


def test_hybrid_provider_rejects_stale_live_quote():
    class Theta:
        def get_data(self, *args):
            return _historical_option_frame()

    class Futu:
        def get_market_snapshot(self, _symbols):
            return pd.DataFrame([{
                "code": "US.AAPL261016P300000",
                "update_time": "2020-01-01 10:00:00",
                "open_price": 2.0,
                "high_price": 2.2,
                "low_price": 1.8,
                "last_price": 2.1,
                "volume": 1,
            }])

    provider = HybridDataProvider(theta_provider=Theta(), futu_provider=Futu())
    provider.set_live_mode(True)

    assert provider.get_data("US.AAPL261016P300000", "20260801", "20260910") is None


def test_hybrid_provider_rejects_live_quote_without_matching_symbol():
    class Theta:
        def get_data(self, *args):
            return _historical_option_frame()

    class Futu:
        def get_market_snapshot(self, _symbols):
            timestamp = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S")
            return pd.DataFrame([{
                "update_time": timestamp,
                "open_price": 2.0,
                "high_price": 2.2,
                "low_price": 1.8,
                "last_price": 2.1,
                "volume": 1,
            }])

    provider = HybridDataProvider(theta_provider=Theta(), futu_provider=Futu())
    provider.set_live_mode(True)

    assert provider.get_data("US.AAPL261016P300000", "20260801", "20260910") is None


def test_hybrid_provider_does_not_reuse_stale_option_risk_fields():
    class Theta:
        def get_data(self, *args):
            return _historical_option_frame()

    class Futu:
        def get_market_snapshot(self, _symbols):
            timestamp = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S")
            return pd.DataFrame([{
                "code": "US.AAPL261016P300000",
                "update_time": timestamp,
                "open_price": 2.0,
                "high_price": 2.2,
                "low_price": 1.8,
                "last_price": 2.1,
                "volume": 500,
                "bid_price": 2.0,
                "ask_price": 2.2,
                "option_type": "PUT",
                "option_strike_price": 300.0,
                "strike_time": "2026-10-16",
                "option_contract_multiplier": 100.0,
            }])

    provider = HybridDataProvider(theta_provider=Theta(), futu_provider=Futu())
    provider.set_live_mode(True)

    assert provider.get_data("US.AAPL261016P300000", "20260801", "20260910") is None


def test_hybrid_provider_rejects_nonfinite_live_option_risk_fields():
    class Theta:
        def get_data(self, *args):
            return _historical_option_frame()

    class Futu:
        def get_market_snapshot(self, _symbols):
            timestamp = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S")
            return pd.DataFrame([{
                "code": "US.AAPL261016P300000",
                "update_time": timestamp,
                "open_price": 2.0, "high_price": 2.2, "low_price": 1.8,
                "last_price": 2.1, "volume": 500,
                "option_type": "PUT", "option_strike_price": 300.0,
                "strike_time": "2026-10-16", "option_implied_volatility": float("inf"),
                "option_delta": -0.14, "option_gamma": 0.01,
                "option_open_interest": 10000, "option_contract_multiplier": 100.0,
                "bid_price": 2.0, "ask_price": 2.2,
            }])

    provider = HybridDataProvider(theta_provider=Theta(), futu_provider=Futu())
    provider.set_live_mode(True)

    assert provider.get_data("US.AAPL261016P300000", "20260801", "20260910") is None


def test_data_manager_supports_custom_composition_alias_key(monkeypatch):
    class Historical:
        def get_data(self, *args):
            return pd.DataFrame(
                {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
                index=pd.date_range("2026-01-01", periods=1),
            )

    class Realtime:
        pass

    manager = object.__new__(DataManager)
    manager.providers = []
    manager.provider_map = {"historical_test": Historical(), "realtime_test": Realtime()}
    manager._composed_providers = {}
    manager._live_mode = False
    manager._bound_broker = None
    monkeypatch.setattr(
        "config.DATA_PROVIDER_COMPOSITIONS",
        {
            "theta+futu": {
                "historical": "historical_test",
                "realtime": "realtime_test",
                "factory": "data_providers.overlay_provider:OverlayDataProvider",
            }
        },
    )

    assert manager._provider_for_source("theta+futu").historical_provider.__class__.__name__ == "Historical"


def test_data_manager_recognizes_explicit_hybrid_source():
    assert DataManager._split_source_names("theta+futu") == ["hybrid"]
    assert DataManager._split_source_names("theta + futu") == ["hybrid"]


def test_hybrid_provider_translates_theta_occ_and_futu_option_symbols():
    assert HybridDataProvider._provider_symbols(
        "US.AAPL261016P305000"
    ) == ("US.AAPL261016P00305000", "US.AAPL261016P305000")
    assert HybridDataProvider._provider_symbols(
        "US.AAPL261016P50000"
    ) == ("US.AAPL261016P00050000", "US.AAPL261016P50000")
    assert HybridDataProvider._provider_symbols(
        "US.MARA261016P9000"
    ) == ("US.MARA261016P00009000", "US.MARA261016P9000")


def test_hybrid_provider_normalizes_stock_venue_for_theta_history():
    assert HybridDataProvider._provider_symbols("NASDAQ.AAPL") == (
        "US.AAPL", "US.AAPL"
    )


def test_hybrid_provider_binds_broker_quote_context():
    class Quote:
        pass

    class Broker:
        def __init__(self):
            self.quote = Quote()

        def _get_quote_context(self):
            return self.quote

    provider = HybridDataProvider(
        theta_provider=type("Theta", (), {})(),
        futu_provider=type("Futu", (), {
            "_quote_ctx": None,
            "_owns_quote_ctx": False,
        })(),
    )
    provider.bind_broker(Broker())

    assert provider.futu_provider._quote_ctx is not None


def test_overlay_provider_is_provider_agnostic():
    class Historical:
        def get_data(self, symbol, *args):
            return pd.DataFrame(
                {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
                index=pd.date_range("2026-01-01", periods=1),
            )

    class Realtime:
        pass

    overlay = OverlayDataProvider(
        Historical(), Realtime(),
        symbol_mapper=lambda symbol: (f"H:{symbol}", f"R:{symbol}"),
        merge_realtime=lambda frame, **kwargs: frame,
    )
    overlay.set_live_mode(True)

    result = overlay.get_data("ABC", "20260101", "20260101")

    assert result is not None


def test_data_manager_builds_composition_from_configuration(monkeypatch):
    class Historical:
        def get_data(self, *args):
            return pd.DataFrame(
                {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
                index=pd.date_range("2026-01-01", periods=1),
            )

    class Realtime:
        pass

    manager = object.__new__(DataManager)
    manager.providers = []
    manager.provider_map = {"historical_test": Historical(), "realtime_test": Realtime()}
    manager._composed_providers = {}
    manager._live_mode = False
    manager._bound_broker = None
    monkeypatch.setattr(
        "config.DATA_PROVIDER_COMPOSITIONS",
        {
            "test_overlay": {
                "historical": "historical_test",
                "realtime": "realtime_test",
                "factory": "data_providers.overlay_provider:OverlayDataProvider",
                "factory_kwargs": {
                    "historical_provider": "historical",
                    "realtime_provider": "realtime",
                },
            }
        },
    )

    provider = manager._provider_for_source("test_overlay")

    assert isinstance(provider, OverlayDataProvider)
    assert provider.historical_provider.__class__.__name__ == "Historical"


def test_data_manager_composition_alias_is_lazy_and_receives_live_mode(monkeypatch):
    manager = object.__new__(DataManager)
    manager.providers = []
    manager.provider_map = {
        "historical_test": type("Historical", (), {
            "get_data": lambda self, *args: pd.DataFrame(
                {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
                index=pd.date_range("2026-01-01", periods=1),
            )
        })(),
        "realtime_test": object(),
    }
    manager._composed_providers = {}
    manager._live_mode = True
    manager._bound_broker = None
    monkeypatch.setattr(
        "config.DATA_PROVIDER_COMPOSITIONS",
        {
            "test_overlay": {
                "historical": "historical_test",
                "realtime": "realtime_test",
                "factory": "data_providers.overlay_provider:OverlayDataProvider",
            }
        },
    )

    provider = manager._provider_for_source("test_overlay")

    assert provider.live_mode is True
