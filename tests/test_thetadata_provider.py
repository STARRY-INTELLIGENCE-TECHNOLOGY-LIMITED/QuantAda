import datetime as _dt
import threading
import time

import pandas as pd

from data_providers.base_provider import REQUEST_ATTEMPTS
from data_providers.thetadata_provider import ThetaDataProvider, _vendor_today


class _FakeTheta:
    def __init__(self):
        self.calls = []

    def stock_history_eod(self, **kwargs):
        self.calls.append(("stock_history_eod", kwargs))
        return pd.DataFrame(
            {
                "timestamp": ["2024-01-02", "2024-01-03"],
                "open": [100, 101], "high": [102, 103], "low": [99, 100],
                "close": [101, 102], "volume": [1000, 1100],
            }
        )

    def stock_history_ohlc(self, **kwargs):
        self.calls.append(("stock_history_ohlc", kwargs))
        return self.stock_history_eod(**kwargs)

    def option_history_eod(self, **kwargs):
        self.calls.append(("option_history_eod", kwargs))
        return pd.DataFrame(
            {
                "date": [20240102, 20240103],
                "open": [1.0, 1.2], "high": [1.3, 1.4], "low": [0.9, 1.1],
                "close": [1.2, 1.3], "volume": [10, 11],
            }
        )

    def option_history_ohlc(self, **kwargs):
        self.calls.append(("option_history_ohlc", kwargs))
        return self.option_history_eod(**kwargs)

    def option_history_greeks_eod(self, **kwargs):
        return pd.DataFrame(
            {
                "timestamp": ["2024-01-02", "2024-01-03"],
                "implied_volatility": [0.2, 0.21], "delta": [-0.1, -0.11],
                "gamma": [0.01, 0.011], "theta": [-0.02, -0.021],
                "vega": [0.1, 0.11], "rho": [-0.01, -0.011],
            }
        )

    def option_history_greeks_first_order(self, **kwargs):
        self.calls.append(("option_history_greeks_first_order", kwargs))
        return pd.DataFrame(
            {
                "timestamp": ["2024-01-02T15:00:00Z"],
                "implied_vol": [0.2], "delta": [-0.1], "theta": [-0.02],
                "vega": [0.1], "rho": [-0.01],
            }
        )

    def option_history_open_interest(self, **kwargs):
        self.calls.append(("option_history_open_interest", kwargs))
        return pd.DataFrame(
            {"timestamp": ["2024-01-02", "2024-01-03"], "open_interest": [100, 110]}
        )

    def option_list_expirations(self, **kwargs):
        return pd.DataFrame({'expiration': [20240119]})

    def option_snapshot_greeks_all(self, **kwargs):
        return pd.DataFrame({
            'timestamp': ['2024-01-02T15:00:00Z'], 'symbol': ['AAPL'], 'strike': [150], 'right': ['put'],
            'spot': [155], 'bid': [1.1], 'ask': [1.3], 'last': [1.2], 'volume': [10],
            'open_interest': [100], 'iv': [0.2], 'delta': [-0.1], 'gamma': [0.01],
            'theta': [-0.02], 'vega': [0.1], 'rho': [-0.01], 'currency': ['USD'],
        })

    def option_snapshot_quote(self, **kwargs):
        return pd.DataFrame({
            'timestamp': ['2024-01-02T15:00:00Z'], 'option_symbol': ['US.AAPL240119P00150000'],
            'bid': [1.1], 'ask': [1.3], 'last': [1.2], 'volume': [10],
            'open_interest': [100], 'currency': ['USD'],
        })

    def option_snapshot_open_interest(self, **kwargs):
        return pd.DataFrame({
            'timestamp': ['2024-01-02T15:00:00Z'], 'option_symbol': ['US.AAPL240119P00150000'],
            'open_interest': [100],
        })


def test_stock_history_is_normalized():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data("US.AAPL", "20240101", "20240105")
    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(result.index, pd.DatetimeIndex)
    assert result.index.is_monotonic_increasing
    assert fake.calls[0][0] == "stock_history_eod"


def test_intraday_history_uses_supported_interval():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data("US.AAPL", "20240101", "20240102", "Minutes", 5)
    assert result is not None
    assert any(name == "stock_history_ohlc" and call["interval"] == "5m" for name, call in fake.calls)


def test_option_history_parses_occ_and_enriches_greeks():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data(
        "US.AAPL240119P00150000", "20240101", "20240105"
    )
    assert result is not None
    assert result.attrs["option_symbol"] == "US.AAPL240119P00150000"
    assert result.attrs["contract_multiplier"] == 100.0
    assert result["delta"].notna().all()
    assert result["open_interest"].notna().all()
    assert fake.calls[0][1]["right"] == "P"


def test_adjusted_option_history_is_rejected_without_verified_multiplier():
    class Adjusted(_FakeTheta):
        def option_history_eod(self, **kwargs):
            frame = super().option_history_eod(**kwargs)
            frame["is_adjusted"] = True
            return frame

    result = ThetaDataProvider(client=Adjusted()).get_data(
        "US.AAPL240119P00150000", "20240101", "20240105"
    )
    assert result is None


def test_daily_history_uses_date_boundary_for_cross_feed_alignment():
    result = ThetaDataProvider(client=_FakeTheta()).get_data(
        "US.AAPL", "20240101", "20240105"
    )

    assert list(result.index) == [
        pd.Timestamp("2024-01-02", tz="UTC"),
        pd.Timestamp("2024-01-03", tz="UTC"),
    ]


def test_theta_provider_reads_public_config_token(monkeypatch):
    import data_providers.thetadata_provider as theta_module

    monkeypatch.delenv("THETADATA_API_KEY", raising=False)
    monkeypatch.setattr(theta_module.config, "THETADATA_TOKEN", "runtime-token")

    provider = theta_module.ThetaDataProvider()

    assert provider.token == "runtime-token"
    assert provider.is_external_mode is False


def test_option_intraday_enrichment_uses_same_frequency_greeks():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data(
        "US.AAPL240119P00150000", "20240102", "20240102", "Minutes", 5
    )

    assert result is not None
    assert any(name == "option_history_greeks_first_order" for name, _ in fake.calls)
    assert not any(name == "option_history_greeks_eod" for name, _ in fake.calls)


def test_option_history_greeks_public_method_uses_standard_intraday_endpoint():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_option_history_greeks(
        "US.AAPL240119P00150000", "20240102", "20240102", "Minutes", 5
    )

    assert result is not None
    assert any(name == "option_history_greeks_first_order" for name, _ in fake.calls)


def test_invalid_or_unsupported_request_fails_closed():
    fake = _FakeTheta()
    provider = ThetaDataProvider(client=fake)
    assert provider.get_data("US.AAPL", "20240101", "20240105", "Seconds") is None
    assert provider.get_data("US.AAPL240119P00150000", "20240101", "20240105", "Minutes", 2) is None


def test_option_chain_builds_occ_symbols_and_unified_schema():
    chain = ThetaDataProvider(client=_FakeTheta()).get_option_chain("US.AAPL", normalized=True)
    assert chain is not None
    assert chain.iloc[0]["option_symbol"] == "US.AAPL240119P00150000"
    assert chain.iloc[0]["contract_multiplier"] == 100
    assert chain.attrs["quote_complete"] is True


def test_option_chain_with_expiration_column_does_not_raise_static_method_error():
    fake = _FakeTheta()
    fake.option_snapshot_greeks_all = lambda **kwargs: pd.DataFrame({
        'timestamp': ['2024-01-02T15:00:00Z'], 'symbol': ['AAPL'],
        'expiration': [20240119], 'strike': [150], 'right': ['P'],
        'spot': [155], 'bid': [1.1], 'ask': [1.3], 'last': [1.2], 'volume': [10],
        'open_interest': [100], 'iv': [0.2], 'delta': [-0.1], 'gamma': [0.01],
        'theta': [-0.02], 'vega': [0.1], 'rho': [-0.01], 'currency': ['USD'],
    })
    chain = ThetaDataProvider(client=fake).get_option_chain('US.AAPL', normalized=True)
    assert chain is not None
    assert chain.iloc[0]['expiry'] == pd.Timestamp('2024-01-19', tz='UTC')


def test_option_chain_falls_back_to_standard_first_order_greeks():
    class StandardTheta(_FakeTheta):
        def option_snapshot_greeks_all(self, **kwargs):
            self.calls.append(("option_snapshot_greeks_all", kwargs))
            return None

        def option_snapshot_greeks_first_order(self, **kwargs):
            self.calls.append(("option_snapshot_greeks_first_order", kwargs))
            return pd.DataFrame({
                "timestamp": ["2024-01-02T15:00:00Z"], "symbol": ["AAPL"],
                "expiration": [20240119], "strike": [150], "right": ["P"],
                "bid": [1.1], "ask": [1.3], "delta": [-0.1],
                "theta": [-0.02], "vega": [0.1], "rho": [-0.01],
                "implied_vol": [0.2], "underlying_price": [155],
            })

    fake = StandardTheta()
    chain = ThetaDataProvider(client=fake).get_option_chain(
        "US.AAPL", expirations=["2024-01-19"], normalized=False
    )

    assert chain is not None
    assert chain.iloc[0]["option_symbol"] == "US.AAPL240119P00150000"
    assert any(name == "option_snapshot_greeks_first_order" for name, _ in fake.calls)


def test_permission_denied_method_is_not_retried_in_same_provider_run():
    class DeniedTheta:
        def __init__(self):
            self.calls = 0

        def option_snapshot_greeks_all(self, **kwargs):
            self.calls += 1
            raise RuntimeError("PERMISSION_DENIED: professional subscription required")

    fake = DeniedTheta()
    provider = ThetaDataProvider(client=fake)

    assert provider._call("option_snapshot_greeks_all", symbol="AAPL") is None
    assert provider._call("option_snapshot_greeks_all", symbol="AAPL") is None
    assert fake.calls == 1


def test_daily_quote_does_not_falsely_use_eod_close_as_nbbo():
    fake = _FakeTheta()
    provider = ThetaDataProvider(client=fake)
    assert provider.get_option_history_quote(
        'US.AAPL240119P00150000', '20240101', '20240105', timeframe='Days'
    ) is None
    assert fake.calls == []


def test_historical_chain_requires_as_of_boundary():
    provider = ThetaDataProvider(client=_FakeTheta())
    assert provider.get_option_chain("US.AAPL", start="2024-01-01", end="2024-01-31") is None


def test_theta_date_and_ms_of_day_are_not_parsed_as_epoch_nanoseconds():
    raw = pd.DataFrame({
        "date": [20240102], "ms_of_day": [34200000], "open": [1], "high": [1],
        "low": [1], "close": [1], "volume": [1],
    })
    result = ThetaDataProvider._normalise_ohlcv(raw)
    assert result.index[0] == pd.Timestamp("2024-01-02 09:30:00", tz="UTC")


def test_future_end_is_clipped_and_marked():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data("US.AAPL", "20240101", "20990101")
    assert result is not None
    assert result.attrs["future_data_unavailable"] is True
    assert fake.calls[0][1]["end_date"] <= _vendor_today()


def test_integer_yyyymmdd_input_is_supported():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data("US.AAPL", 20240101, 20240105)
    assert result is not None
    assert fake.calls[0][1]["start_date"].isoformat() == "2024-01-01"

def test_option_history_end_is_clipped_to_vendor_today(monkeypatch):
    vendor_today = _dt.date(2026, 9, 14)
    monkeypatch.setattr("data_providers.thetadata_provider._vendor_today", lambda: vendor_today)
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data(
        "US.MARA261009P00010000", "20260801", "20260915"
    )
    assert result is not None
    assert result.attrs["future_data_unavailable"] is True
    assert fake.calls[0][0] == "option_history_eod"
    assert fake.calls[0][1]["end_date"] == vendor_today

def test_historical_chain_uses_eod_greeks_not_snapshot():
    class HistoricalTheta(_FakeTheta):
        def option_history_greeks_eod(self, **kwargs):
            self.calls.append(("option_history_greeks_eod", kwargs))
            return pd.DataFrame({
                "symbol": ["AAPL"],
                "expiration": [20240119],
                "strike": [150],
                "right": ["PUT"],
                "timestamp": ["2024-01-02T16:00:00-05:00"],
                "open": [1.0], "high": [1.3], "low": [0.9], "close": [1.2],
                "volume": [10], "bid": [1.1], "ask": [1.3],
                "delta": [-0.1], "implied_vol": [0.2],
                "underlying_price": [155],
            })

        def option_snapshot_greeks_all(self, **kwargs):
            self.calls.append(("option_snapshot_greeks_all", kwargs))
            raise AssertionError("snapshot must not backfill historical as_of")

    fake = HistoricalTheta()
    chain = ThetaDataProvider(client=fake).get_option_chain(
        "US.AAPL", as_of="2024-01-02", normalized=True
    )
    assert chain is not None
    assert chain.iloc[0]["option_symbol"] == "US.AAPL240119P00150000"
    assert chain.iloc[0]["delta"] == -0.1
    assert any(name == "option_history_greeks_eod" for name, _ in fake.calls)
    assert not any(str(name).startswith("option_snapshot") for name, _ in fake.calls)


def test_future_as_of_historical_chain_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "data_providers.thetadata_provider._vendor_today",
        lambda: _dt.date(2026, 9, 14),
    )
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_option_chain(
        "US.AAPL", as_of="20260915", normalized=True
    )
    assert result is None
    assert fake.calls == []



def test_current_day_as_of_historical_chain_is_rejected(monkeypatch):
    vendor_today = _dt.date(2026, 9, 21)
    monkeypatch.setattr(
        "data_providers.thetadata_provider._vendor_today",
        lambda: vendor_today,
    )
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_option_chain(
        "US.AAPL", as_of="20260921", normalized=True
    )
    assert result is None
    assert fake.calls == []



def test_current_day_theta_error_is_counted_empty_not_retried():
    class CurrentDayTheta:
        def __init__(self):
            self.calls = 0

        def option_history_eod(self, **kwargs):
            self.calls += 1
            raise RuntimeError('INVALID_ARGUMENT: Cannot fetch current-day data without specifying an expiration')

    fake = CurrentDayTheta()
    provider = ThetaDataProvider(client=fake)
    assert provider._call("option_history_eod", symbol="SPY", expiration="*") is None
    assert fake.calls == 1
    assert provider.take_empty_result_count() == 1
    assert provider.take_retryable_fail_count() == 0



def test_invalid_argument_theta_error_is_not_retried(capsys):
    class BadArgTheta:
        def __init__(self):
            self.calls = 0

        def option_history_eod(self, **kwargs):
            self.calls += 1
            raise RuntimeError("StatusCode.INVALID_ARGUMENT: bad request")

    fake = BadArgTheta()
    provider = ThetaDataProvider(client=fake)
    assert provider._call("option_history_eod", symbol="SPY") is None
    assert fake.calls == 1
    assert provider.take_retryable_fail_count() == 1
    assert "option_history_eod failed:" in capsys.readouterr().out



def test_bulk_requests_use_longer_timeout():
    assert ThetaDataProvider._is_bulk_kwargs({"strike": "*"}) is True
    assert ThetaDataProvider._is_bulk_kwargs({"expiration": "*"}) is True
    assert ThetaDataProvider._is_bulk_kwargs({"strike": "150"}) is False
    assert ThetaDataProvider._timeout_seconds(bulk=True) == 120.0
    assert ThetaDataProvider._timeout_seconds() == 60.0


def test_historical_chain_filters_dte_and_sends_strike_range():
    class Multi(_FakeTheta):
        def option_list_expirations(self, **kwargs):
            self.calls.append(("option_list_expirations", kwargs))
            return pd.DataFrame({"expiration": [20240105, 20240119, 20240621]})

        def option_history_greeks_eod(self, **kwargs):
            self.calls.append(("option_history_greeks_eod", kwargs))
            expiration = kwargs["expiration"]
            return pd.DataFrame({
                "symbol": ["AAPL"],
                "expiration": [int(expiration.strftime("%Y%m%d"))],
                "strike": [150],
                "right": ["PUT"],
                "timestamp": ["2024-01-02T16:00:00-05:00"],
                "open": [1.0], "high": [1.3], "low": [0.9], "close": [1.2],
                "volume": [10], "bid": [1.1], "ask": [1.3],
                "delta": [-0.1], "implied_vol": [0.2],
                "underlying_price": [155],
            })

    fake = Multi()
    chain = ThetaDataProvider(client=fake).get_option_chain(
        "US.AAPL",
        as_of="2024-01-02",
        min_dte=10,
        max_dte=20,
        right="put",
        normalized=False,
    )
    assert chain is not None
    greeks = [kwargs for name, kwargs in fake.calls if name == "option_history_greeks_eod"]
    assert any(kwargs["expiration"] == "*" for kwargs in greeks)
    exact = [kwargs for kwargs in greeks if kwargs["expiration"] == _dt.date(2024, 1, 19)]
    assert exact
    assert exact[0]["right"] == "put"
    assert exact[0]["strike_range"] == 40


def test_historical_chain_uses_wildcard_batch_and_filters_expirations_locally():
    class Batch(_FakeTheta):
        def option_list_expirations(self, **kwargs):
            self.calls.append(("option_list_expirations", kwargs))
            return pd.DataFrame({"expiration": [20240119, 20240126, 20240216]})

        def option_history_greeks_eod(self, **kwargs):
            self.calls.append(("option_history_greeks_eod", kwargs))
            return pd.DataFrame({
                "expiration": ["2024-01-19", "2024-01-26", "2024-02-16"],
                "strike": [150.0, 151.0, 152.0],
                "right": ["PUT", "PUT", "PUT"],
                "timestamp": ["2024-01-02T16:00:00-05:00"] * 3,
                "open": [1.0] * 3, "high": [1.1] * 3,
                "low": [0.9] * 3, "close": [1.0] * 3,
                "volume": [10] * 3, "bid": [0.9] * 3,
                "ask": [1.1] * 3, "delta": [-0.1] * 3,
            })

    fake = Batch()
    chain = ThetaDataProvider(client=fake).get_option_chain(
        "US.AAPL",
        as_of="2024-01-02",
        min_dte=10,
        max_dte=20,
        right="put",
        normalized=False,
    )

    assert chain is not None
    assert len(chain) == 1
    assert chain.iloc[0]["expiry"] == pd.Timestamp("2024-01-19", tz="UTC")
    history_calls = [kwargs for name, kwargs in fake.calls if name == "option_history_greeks_eod"]
    assert len(history_calls) == 1
    assert history_calls[0]["expiration"] == "*"


def test_expiration_list_is_cached_across_as_of_dates():
    class Counted(_FakeTheta):
        def option_list_expirations(self, **kwargs):
            self.calls.append(("option_list_expirations", kwargs))
            return super().option_list_expirations(**kwargs)

        def option_history_greeks_eod(self, **kwargs):
            self.calls.append(("option_history_greeks_eod", kwargs))
            return super().option_history_greeks_eod(**kwargs)

    fake = Counted()
    provider = ThetaDataProvider(client=fake)
    first = provider.get_option_chain("US.AAPL", as_of="2024-01-02", normalized=False)
    second = provider.get_option_chain("US.AAPL", as_of="2024-01-03", normalized=False)
    assert first is not None and second is not None
    listed = [name for name, _ in fake.calls if name == "option_list_expirations"]
    assert listed == ["option_list_expirations"]


def test_timeout_resets_owned_client(monkeypatch):
    class Fake:
        def option_history_eod(self, **kwargs):
            return pd.DataFrame({"open": [1]})

    provider = ThetaDataProvider(api_key="token")
    provider.client = Fake()
    provider._owns_client = True

    class Alive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    monkeypatch.setattr("data_providers.thetadata_provider.threading.Thread", Alive)
    assert provider._call("option_history_eod", symbol="AAPL") is None
    assert provider.client is None


def test_timeout_keeps_injected_client(monkeypatch):
    class Fake:
        def option_history_eod(self, **kwargs):
            return pd.DataFrame({"open": [1]})

    fake = Fake()
    provider = ThetaDataProvider(client=fake)

    class Alive:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    monkeypatch.setattr("data_providers.thetadata_provider.threading.Thread", Alive)
    assert provider._call("option_history_eod", symbol="AAPL") is None
    assert provider.client is fake


def test_client_initialization_retries_after_sdk_error(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, **kwargs):
            self.closed = False

        def close(self):
            self.closed = True

    attempts = []

    class FakeModule:
        class ThetaClient:
            def __new__(cls, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("temporary connect failure")
                return FakeClient(**kwargs)

    monkeypatch.setattr(
        "data_providers.thetadata_provider.importlib.import_module",
        lambda _name: FakeModule,
    )
    provider = ThetaDataProvider(api_key="token")

    client = provider._get_client()

    assert isinstance(client, FakeClient)
    assert len(attempts) == 2
    assert "retrying client initialization" in capsys.readouterr().out


def test_option_history_is_clipped_to_contract_life():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data(
        "US.AAPL240119P00150000", "20220101", "20250105"
    )
    assert result is not None
    eod = [kwargs for name, kwargs in fake.calls if name == "option_history_eod"]
    assert eod
    assert eod[0]["end_date"] == _dt.date(2024, 1, 19)
    assert eod[0]["start_date"] >= _dt.date(2023, 1, 20)
    assert (eod[0]["end_date"] - eod[0]["start_date"]).days <= 365


def test_history_or_chunk_splits_before_sending_overlimit_span():
    class Counting(_FakeTheta):
        def stock_history_eod(self, **kwargs):
            self.calls.append(("stock_history_eod", kwargs))
            start = kwargs["start_date"]
            end = kwargs["end_date"]
            assert (end - start).days < 365
            return super().stock_history_eod(**kwargs)

    fake = Counting()
    provider = ThetaDataProvider(client=fake)
    frame = provider._history_or_chunk(
        "stock_history_eod",
        _dt.date(2022, 1, 1),
        _dt.date(2024, 1, 1),
        365,
        symbol="AAPL",
    )
    assert frame is not None
    spans = [
        (kwargs["end_date"] - kwargs["start_date"]).days
        for name, kwargs in fake.calls
        if name == "stock_history_eod"
    ]
    assert spans
    assert all(span < 365 for span in spans)
    assert len(spans) > 1


def test_select_chain_expirations_prefers_fridays():
    as_of = _dt.date(2024, 1, 5)
    expirations = [_dt.date(2024, 1, 5) + _dt.timedelta(days=offset) for offset in range(23, 53)]
    selected = ThetaDataProvider._select_chain_expirations(expirations, as_of, 23, 52)
    assert selected
    assert all(item.weekday() == 4 for item in selected)
    assert _dt.date(2024, 2, 9) in selected
    assert _dt.date(2024, 2, 16) in selected
    assert _dt.date(2024, 2, 5) not in selected


def test_empty_theta_error_is_counted_not_printed(capsys):
    class EmptyTheta:
        def option_history_greeks_eod(self, **kwargs):
            raise RuntimeError("No data found for: option_history_greeks_eod(SPY)")

    provider = ThetaDataProvider(client=EmptyTheta())
    assert provider._call("option_history_greeks_eod", symbol="SPY") is None
    assert provider.take_empty_result_count() == 1
    assert provider.take_empty_result_count() == 0
    assert "No data found" not in capsys.readouterr().out


def test_non_empty_theta_error_still_prints(capsys):
    class BrokenTheta:
        def option_history_eod(self, **kwargs):
            raise RuntimeError("unavailable")

    provider = ThetaDataProvider(client=BrokenTheta())
    assert provider._call("option_history_eod", symbol="SPY") is None
    assert provider.take_empty_result_count() == 0
    assert "option_history_eod failed: unavailable" in capsys.readouterr().out


def test_retryable_theta_error_is_retried():
    class FlakyTheta:
        def __init__(self):
            self.calls = 0

        def option_history_eod(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("unavailable")
            return pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]})

    fake = FlakyTheta()
    provider = ThetaDataProvider(client=fake)
    got = provider._call("option_history_eod", symbol="SPY")
    assert got is not None
    assert fake.calls == 2
    assert provider.take_retryable_fail_count() == 0


def test_invalid_session_resets_owned_client_before_retry():
    class StaleTheta:
        def __init__(self):
            self.calls = 0
            self.closed = 0

        def option_history_eod(self, **kwargs):
            self.calls += 1
            raise RuntimeError("UNAUTHENTICATED: Invalid session ID")

        def close(self):
            self.closed += 1

    class FreshTheta:
        def option_history_eod(self, **kwargs):
            return pd.DataFrame({
                "open": [1], "high": [1], "low": [1],
                "close": [1], "volume": [1],
            })

    stale = StaleTheta()
    fresh = FreshTheta()
    provider = ThetaDataProvider(api_key="token")
    provider.client = stale
    provider._owns_client = True
    clients = iter((stale, fresh))
    provider._get_client = lambda: next(clients)

    result = provider._call("option_history_eod", symbol="SPY")

    assert result is not None
    assert stale.calls == 1
    assert stale.closed == 1


def test_retryable_theta_error_counts_fail_after_attempts(capsys):
    class BrokenTheta:
        def __init__(self):
            self.calls = 0

        def option_history_eod(self, **kwargs):
            self.calls += 1
            raise RuntimeError("unavailable")

    fake = BrokenTheta()
    provider = ThetaDataProvider(client=fake)
    assert provider._call("option_history_eod", symbol="SPY") is None
    assert fake.calls == REQUEST_ATTEMPTS
    assert provider.take_retryable_fail_count() == 1
    assert provider.take_retryable_fail_count() == 0
    out = capsys.readouterr().out
    assert out.count("option_history_eod failed: unavailable") == 1


def test_empty_theta_error_is_not_retried():
    class EmptyTheta:
        def __init__(self):
            self.calls = 0

        def option_history_greeks_eod(self, **kwargs):
            self.calls += 1
            raise RuntimeError("No data found for: option_history_greeks_eod(SPY)")

    fake = EmptyTheta()
    provider = ThetaDataProvider(client=fake)
    assert provider._call("option_history_greeks_eod", symbol="SPY") is None
    assert fake.calls == 1
    assert provider.take_empty_result_count() == 1
    assert provider.take_retryable_fail_count() == 0


def test_concurrent_calls_reuse_single_client():
    started = threading.Barrier(4)
    seen = set()
    lock = threading.Lock()

    class SharedTheta:
        def option_history_eod(self, **kwargs):
            with lock:
                seen.add(id(self))
            started.wait(timeout=2)
            return pd.DataFrame({
                "open": [1], "high": [1], "low": [1],
                "close": [1], "volume": [1],
            })

    fake = SharedTheta()
    provider = ThetaDataProvider(client=fake)
    results = []

    def worker():
        results.append(provider._call("option_history_eod", symbol="SPY"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(item is not None for item in results)
    assert seen == {id(fake)}
    assert provider.client is fake
    assert provider._inflight == 0


def test_timeout_does_not_reauth_while_another_call_is_inflight():
    hold_started = threading.Event()
    release_hold = threading.Event()
    closed = {"n": 0}

    class SharedTheta:
        def option_history_eod(self, **kwargs):
            symbol = str(kwargs.get("symbol") or "")
            if symbol == "HOLD":
                hold_started.set()
                release_hold.wait(timeout=5)
                return pd.DataFrame({
                    "open": [1], "high": [1], "low": [1],
                    "close": [1], "volume": [1],
                })
            time.sleep(1.0)
            return pd.DataFrame({
                "open": [1], "high": [1], "low": [1],
                "close": [1], "volume": [1],
            })

        def close(self):
            closed["n"] += 1

    fake = SharedTheta()
    provider = ThetaDataProvider(api_key="token")
    provider.client = fake
    provider._owns_client = True
    slow_armed = threading.Event()
    provider._timeout_seconds = lambda bulk=False: 0.2 if slow_armed.is_set() else 5.0

    hold_result = {}

    def hold_worker():
        hold_result["value"] = provider._call("option_history_eod", symbol="HOLD")

    holder = threading.Thread(target=hold_worker)
    holder.start()
    assert hold_started.wait(timeout=2)
    slow_armed.set()
    timed = provider._call("option_history_eod", symbol="SLOW")
    assert timed is None
    assert provider.client is fake
    assert closed["n"] == 0
    release_hold.set()
    holder.join(timeout=5)
    assert hold_result.get("value") is not None
    assert closed["n"] >= 1
    assert provider.client is None


def test_thread_local_empty_counts_do_not_steal_across_workers():
    class EmptyTheta:
        def option_history_eod(self, **kwargs):
            raise RuntimeError("No data found for: option_history_eod(SPY)")

    provider = ThetaDataProvider(client=EmptyTheta())
    counts = []

    def worker():
        provider._call("option_history_eod", symbol="SPY")
        counts.append(provider.take_empty_result_count())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert counts == [1, 1, 1]
    assert provider.take_empty_result_count() == 0

