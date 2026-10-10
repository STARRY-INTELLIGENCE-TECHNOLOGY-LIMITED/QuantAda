import pandas as pd

import data_providers.cboe_provider as cboe_module
from data_providers.cboe_provider import CboeDataProvider


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def _ohlc_csv():
    return (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "01/02/2020,12,13,11,12.5\n"
        "01/03/2020,13,14,12,13.5\n"
    ).encode()


def _close_only_csv():
    return (
        "DATE,VVIX\n"
        "01/02/2020,90.0\n"
        "01/03/2020,85.5\n"
    ).encode()


def test_cboe_provider_normalizes_vix_csv_and_filters_window(monkeypatch, capsys):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return _Response(_ohlc_csv())

    monkeypatch.setattr(cboe_module, "urlopen", fake_urlopen)
    provider = CboeDataProvider()
    frame = provider.get_data("US.VIX", "20200103", "20200103")

    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert len(frame) == 1
    assert frame.index[0] == pd.Timestamp("2020-01-03")
    assert frame.iloc[0]["close"] == 13.5
    assert frame.attrs["cboe_value_kind"] == "ohlc"
    assert calls[0][0].endswith("/VIX_History.csv")
    assert "Fallback active for US.VIX" in capsys.readouterr().out


def test_cboe_provider_maps_index_code_and_fetches_once_per_day(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cboe_module,
        "urlopen",
        lambda request, timeout: (calls.append(request.full_url) or _Response(_ohlc_csv())),
    )
    provider = CboeDataProvider()

    assert provider.get_data("US.VIX3M", "20200101", "20200103") is not None
    assert provider.get_data("VIX3M.US", "20200101", "20200103") is not None
    assert len(calls) == 1
    assert calls[0].endswith("/VIX3M_History.csv")


def test_cboe_provider_normalizes_close_only_index_without_claiming_intraday_ohlc(monkeypatch):
    monkeypatch.setattr(cboe_module, "urlopen", lambda request, timeout: _Response(_close_only_csv()))
    frame = CboeDataProvider().get_data("US.VVIX", "20200103", "20200103")

    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 85.5
    assert frame.iloc[0]["open"] == frame.iloc[0]["high"] == frame.iloc[0]["low"] == 85.5
    assert frame.attrs["cboe_value_kind"] == "close_only"
    assert frame.attrs["cboe_ohlc_derived_from_close"] is True
    assert frame.iloc[0]["cboe_value_kind"] == "close_only"
    assert bool(frame.iloc[0]["cboe_ohlc_derived_from_close"]) is True


def test_cboe_provider_rejects_unknown_symbols_and_intraday(monkeypatch):
    calls = []
    monkeypatch.setattr(cboe_module, "urlopen", lambda request, timeout: calls.append(1))
    provider = CboeDataProvider()

    assert provider.get_data("US.VIXY", "20200101", "20200103") is None
    assert provider.get_data("US.VIX", "20200101", "20200103", "Minutes", 1) is None
    assert calls == []


def test_cboe_provider_exposes_canonical_symbols():
    symbols = CboeDataProvider.supported_symbols()

    assert "US.VIX" in symbols
    assert "US.VIX3M" in symbols
    assert "US.VVIX" in symbols
    assert all(symbol.startswith("US.") for symbol in symbols)
