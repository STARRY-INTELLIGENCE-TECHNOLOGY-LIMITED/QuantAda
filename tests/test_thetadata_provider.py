import pandas as pd

from data_providers.thetadata_provider import ThetaDataProvider


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
    assert fake.calls[0][1]["end_date"] <= pd.Timestamp.today().date()


def test_integer_yyyymmdd_input_is_supported():
    fake = _FakeTheta()
    result = ThetaDataProvider(client=fake).get_data("US.AAPL", 20240101, 20240105)
    assert result is not None
    assert fake.calls[0][1]["start_date"].isoformat() == "2024-01-01"
