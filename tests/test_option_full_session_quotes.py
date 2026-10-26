import pandas as pd

from data_providers.manager import DataManager
from data_providers.thetadata_provider import ThetaDataProvider


def test_theta_full_session_quote_normalizer_keeps_regular_session_rows():
    raw = pd.DataFrame({
        "timestamp": [
            "2026-10-22 13:29:00+00:00",  # 09:29 ET，过滤
            "2026-10-22 13:30:00+00:00",  # 09:30 ET
            "2026-10-22 19:59:00+00:00",  # 15:59 ET
            "2026-10-22 20:01:00+00:00",  # 16:01 ET，过滤
        ],
        "bid": [1.0, 1.1, 1.2, 1.3],
        "ask": [1.2, 1.3, 1.4, 1.5],
        "bid_size": [10, 11, 12, 13],
        "ask_size": [20, 21, 22, 23],
    })

    result = ThetaDataProvider._normalise_full_option_quotes(raw)

    assert result is not None
    assert len(result) == 2
    assert result["bid"].tolist() == [1.1, 1.2]
    assert result["session_date"].tolist() == ["2026-10-22", "2026-10-22"]


def test_data_manager_option_quote_facade_uses_selected_provider():
    class QuoteProvider:
        def get_option_history_quotes(self, *args, **kwargs):
            return pd.DataFrame(
                {"bid": [1.0], "ask": [1.1]},
                index=pd.to_datetime(["2026-10-22 13:30:00"]),
            )

    manager = object.__new__(DataManager)
    manager.provider_map = {"quotes": QuoteProvider()}
    manager.providers = []
    manager._composed_providers = {}

    result = manager.get_option_history_quotes(
        "US.AAPL261023P00250000",
        "20261022",
        "20261022",
        specified_sources="quotes",
    )

    assert result is not None
    assert result.iloc[0]["ask"] == 1.1


def test_theta_trade_request_uses_trade_endpoint_without_interval():
    provider = ThetaDataProvider(client=object())
    calls = []

    def fake_history(method, start, end, **kwargs):
        calls.append((method, kwargs))
        return pd.DataFrame({
            "timestamp": ["2026-10-23 13:30:00+00:00"],
            "price": [1.0], "size": [10],
        })

    provider._chunked_history = fake_history
    result = provider.get_option_history_trades(
        "US.AAPL261120P00250000", "20261023", "20261023", cache=False,
    )

    assert result is not None
    assert calls[0][0] == "option_history_trade"
    assert "interval" not in calls[0][1]
