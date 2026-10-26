from types import SimpleNamespace

import pandas as pd

from common.options.analytics import build_option_snapshot
from common.options.market_inputs import forward_dividend_yield_series


def test_forward_dividend_yield_requires_visible_announcement():
    index = pd.DatetimeIndex(["2026-09-01"])
    events = [
        {
            "ex_date": "2026-10-01",
            "announcement_date": "2026-09-02",
            "cash_per_share": 1.0,
        },
    ]
    hidden = forward_dividend_yield_series(
        index, pd.Series(pd.Timestamp("2026-10-31"), index=index),
        pd.Series(100.0, index=index), events,
    )
    assert hidden.iloc[0] == 0.0


def test_option_snapshot_uses_dynamic_rate_column():
    index = pd.date_range("2026-01-01", periods=3, freq="D")
    frame = pd.DataFrame({
        "open": [1.0, 1.0, 1.0], "high": [1.1, 1.1, 1.1],
        "low": [0.9, 0.9, 0.9], "close": [1.0, 1.0, 1.0],
        "volume": [1, 1, 1], "spot": [100.0, 100.0, 100.0],
        "strike": [100.0, 100.0, 100.0], "expiry": ["2026-12-31"] * 3,
        "option_type": ["CALL"] * 3, "iv": [0.2, 0.2, 0.2],
        "risk_free_rate": [0.01, 0.05, 0.05], "dividend_yield": [0.0] * 3,
    }, index=index)
    params = SimpleNamespace(value_window=2, error_window=2, iv_window=2, min_history=2)

    snapshot = build_option_snapshot(frame, "US.AAPL261231C00100000", params)

    values = snapshot["indicators"]["option_fair_value"]
    assert values.iloc[1] != values.iloc[0]
