import pandas as pd

from common.options.events import attach_earnings_calendar, earnings_blackout, normalize_earnings_calendar


def test_normalize_earnings_calendar_filters_and_deduplicates():
    events = pd.DataFrame({
        "security": ["US.AAPL", "NASDAQ.AAPL", "US.MSFT"],
        "earnings_date": ["2024-02-01", "2024-02-01", "2024-02-02"],
    })
    result = normalize_earnings_calendar(events, symbols=["US.AAPL"], start_date="20240101", end_date="20240301")

    assert len(result) == 1
    assert result.iloc[0]["event_date"] == pd.Timestamp("2024-02-01")


def test_attach_earnings_calendar_and_blackout_are_deterministic():
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=pd.to_datetime(["2024-01-01"]),
    )
    events = pd.DataFrame({"security": ["US.AAPL"], "earnings_date": ["2024-02-01"]})
    attach_earnings_calendar({"US.AAPL": frame}, events)

    rows = frame.attrs["earnings_calendar"]
    assert rows[0]["event_date"] == "2024-02-01"
    assert earnings_blackout(rows, "2024-01-27", before_days=7, after_days=2)
    assert not earnings_blackout(rows, "2024-02-20", before_days=7, after_days=2)
