import pandas as pd
import pytest

from data_providers.eodhd_provider import EODHDDataProvider, EODHDRequestError


class FakeEODHDClient:
    def __init__(self, frame=None, errors=()):
        self.frame = frame
        self.errors = list(errors)
        self.calls = []

    def get_historical_data(self, symbol, interval, iso8601_start, iso8601_end):
        self.calls.append((symbol, interval, iso8601_start, iso8601_end))
        if self.errors:
            error = self.errors.pop(0)
            raise error
        return self.frame

    def _rest_get(self, endpoint, ticker, querystring):
        self.calls.append((endpoint, ticker, querystring))
        if endpoint == "div":
            return pd.DataFrame(
                [
                    {
                        "date": "2026-10-10",
                        "declarationDate": "2026-10-01",
                        "value": 0.5,
                        "unadjustedValue": 0.5,
                        "currency": "USD",
                    }
                ]
            )
        return self.frame


def _frame():
    return pd.DataFrame(
        {
            "date": ["2026-08-04", "2026-08-03", "2026-08-03"],
            "open": [123.0, 121.0, 121.5],
            "high": [125.0, 124.0, 124.5],
            "low": [122.0, 120.0, 120.5],
            "close": [124.0, 123.0, 123.5],
            "adjusted_close": [124.0, 123.0, 123.5],
            "volume": [1000, 2000, 2100],
            "symbol": ["LULU.US"] * 3,
            "interval": ["d"] * 3,
        }
    )


def test_eodhd_daily_data_normalizes_ticker_index_and_ohlcv():
    client = FakeEODHDClient(_frame())
    provider = EODHDDataProvider(api_token="test-token", client=client)

    result = provider.get_data("US.LULU", "20260801", "20260804")

    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert list(result.index) == [pd.Timestamp("2026-08-03"), pd.Timestamp("2026-08-04")]
    assert result.loc[pd.Timestamp("2026-08-03"), "close"] == 123.5
    assert result.attrs["eodhd_symbol"] == "LULU.US"
    assert result.attrs["price_adjustment"] == "unadjusted_close"
    assert client.calls == [("LULU.US", "d", "2026-08-01", "2026-08-04")]


def test_eodhd_provider_supports_us_equities_only():
    assert EODHDDataProvider.supports_symbol("US.AAPL")
    assert EODHDDataProvider.supports_symbol("AAPL")
    assert not EODHDDataProvider.supports_symbol("HK.00700")
    assert not EODHDDataProvider.supports_symbol("US.AAPL260918P00150000")
    assert EODHDDataProvider._eodhd_symbol("US.BRK.B") == "BRK-B.US"


def test_eodhd_provider_handles_native_week_and_month_intervals():
    client = FakeEODHDClient(_frame())
    provider = EODHDDataProvider(api_token="test-token", client=client)

    assert provider.get_data("US.LULU", timeframe="Weeks") is not None
    assert provider.get_data("US.LULU", timeframe="Months") is not None
    assert [call[1] for call in client.calls] == ["w", "m"]
    assert provider.get_data("US.LULU", timeframe="Minutes", compression=5) is None
    assert provider.get_data("US.LULU", compression=2) is None


def test_eodhd_provider_does_not_retry_parse_errors(monkeypatch, capsys):
    import data_providers.eodhd_provider as module

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    client = FakeEODHDClient(errors=[KeyError("date")])
    provider = EODHDDataProvider(api_token="test-token", client=client)

    assert provider.get_data("US.LULU", "2026-08-01", "2026-08-04") is None
    assert len(client.calls) == 1
    assert "KeyError" in capsys.readouterr().out


def test_eodhd_provider_retries_transient_errors_but_not_auth_errors(monkeypatch):
    import data_providers.eodhd_provider as module

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    client = FakeEODHDClient(_frame(), errors=[EODHDRequestError(503), EODHDRequestError(429)])
    provider = EODHDDataProvider(api_token="secret-not-for-logs", client=client)

    result = provider.get_data("US.LULU", "2026-08-01", "2026-08-04")

    assert result is not None
    assert len(client.calls) == 3

    denied = FakeEODHDClient(errors=[EODHDRequestError(401, "invalid token")])
    assert EODHDDataProvider(api_token="secret-not-for-logs", client=denied).get_data("US.LULU") is None
    assert len(denied.calls) == 1


def test_eodhd_provider_rejects_invalid_or_empty_responses():
    provider = EODHDDataProvider(api_token="test-token", client=FakeEODHDClient(pd.DataFrame()))
    assert provider.get_data("US.LULU") is None
    assert provider.get_data("US.LULU", "not-a-date", "2026-08-04") is None
    assert provider.get_data("HK.00700") is None


def test_bounded_eodhd_sdk_request_uses_timeout_and_keeps_token_out_of_logs(monkeypatch):
    import data_providers.eodhd_provider as module

    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return [{
                "date": "2026-08-03",
                "open": 121.0,
                "high": 124.0,
                "low": 120.0,
                "close": 123.0,
                "adjusted_close": 123.0,
                "volume": 2000,
            }]

    def fake_get(url, *, params, timeout):
        calls.append((url, dict(params), timeout))
        return Response()

    monkeypatch.setattr(module.requests, "get", fake_get)
    client = module._BoundedEODHDClient("0123456789abcdef01234567")
    result = client.get_historical_data(
        "LULU.US",
        interval="d",
        iso8601_start="2026-08-01",
        iso8601_end="2026-08-04",
    )

    assert result.loc[pd.Timestamp("2026-08-03"), "close"] == 123.0
    assert calls[0][0] == "https://eodhd.com/api/eod/LULU.US"
    assert calls[0][1]["api_token"] == "0123456789abcdef01234567"
    assert calls[0][2] == (5.0, 20.0)


def test_bounded_eodhd_sdk_rejects_warning_payloads_without_retry(monkeypatch):
    import data_providers.eodhd_provider as module

    if module._BoundedEODHDClient is None:
        pytest.skip("eodhd SDK is required")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return [{"warning": "Data is limited by one year as you have free subscription"}]

    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: Response())
    client = module._BoundedEODHDClient("0123456789abcdef01234567")

    with pytest.raises(EODHDRequestError, match="limited by one year"):
        client._rest_get("eod", "AAPL.US", "&period=d&from=2024-01-02&to=2024-01-10")

    denied = FakeEODHDClient(errors=[EODHDRequestError(200, "Data is limited by one year")])
    provider = EODHDDataProvider(api_token="test-token", client=denied)
    assert provider.get_data("US.AAPL", "20240102", "20240110") is None
    assert len(denied.calls) == 1


def test_eodhd_provider_resolves_environment_token_before_config(monkeypatch):
    import data_providers.eodhd_provider as module

    monkeypatch.setattr(module.config, "EODHD_API_TOKEN", "config-token")
    monkeypatch.setenv("EODHD_API_TOKEN", "environment-token")
    provider = EODHDDataProvider(client=FakeEODHDClient(_frame()))

    assert provider.token == "environment-token"


def test_eodhd_dividend_calendar_preserves_announcement_point_in_time():
    client = FakeEODHDClient(_frame())
    provider = EODHDDataProvider(api_token="test-token", client=client)

    before_announcement = provider.get_dividend_calendar(
        ["US.LULU"], "2026-08-01", "2026-10-31", as_of="2026-10-01"
    )
    after_announcement = provider.get_dividend_calendar(
        ["US.LULU"], "2026-08-01", "2026-10-31", as_of="2026-10-02"
    )

    assert before_announcement is not None and before_announcement.empty
    assert before_announcement.attrs["as_of_supported"] is True
    assert len(after_announcement) == 1
    assert after_announcement.iloc[0]["ex_date"] == pd.Timestamp("2026-10-10")
    assert after_announcement.iloc[0]["announcement_date"] == pd.Timestamp("2026-10-01")
    assert after_announcement.iloc[0]["cash_per_share"] == 0.5
    assert after_announcement.iloc[0]["currency"] == "USD"
