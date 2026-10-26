import pandas as pd
import pytest

from common.options.events import (
    attach_dividend_calendar,
    attach_earnings_calendar,
    covered_call_early_assignment_risk,
    earnings_blackout,
    normalize_dividend_calendar,
    normalize_earnings_calendar,
)


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
    assert frame.attrs["earnings_calendar_available"] is True


def test_missing_earnings_provider_snapshot_is_distinct_from_empty_calendar():
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=pd.to_datetime(["2024-01-01"]),
    )

    attach_earnings_calendar({"US.AAPL": frame}, None)

    assert frame.attrs["earnings_calendar"] == ()
    assert frame.attrs["earnings_calendar_available"] is False


def test_dividend_calendar_normalizes_point_in_time_fields_and_attaches_status():
    events = pd.DataFrame({
        "security": ["US.AAPL", "US.MSFT"],
        "ex_date": ["2024-02-12", "2024-02-12"],
        "pub_date": ["2024-02-01", "2024-02-08"],
        "per_cash_div": [0.24, 0.75],
    })
    normalized = normalize_dividend_calendar(
        events, symbols=["US.AAPL"], start_date="20240101", end_date="20240301",
        as_of="2024-02-05",
    )
    assert len(normalized) == 1
    assert normalized.iloc[0]["announcement_date"] == pd.Timestamp("2024-02-01")
    assert normalized.iloc[0]["cash_per_share"] == 0.24

    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=pd.to_datetime(["2024-01-01"]),
    )
    normalized.attrs.update({"source": "futu", "as_of_supported": True})
    attach_dividend_calendar({"US.AAPL": frame}, normalized)
    assert frame.attrs["dividend_calendar"][0]["cash_per_share"] == 0.24
    assert frame.attrs["dividend_calendar_available"] is True
    assert frame.attrs["dividend_calendar_as_of_supported"] is True


def test_dividend_call_assignment_risk_uses_last_session_and_prior_publication():
    sessions = pd.to_datetime(["2026-09-10", "2026-09-11", "2026-09-14"])
    event = {
        "ex_date": "2026-09-14",
        "announcement_date": "2026-09-10",
        "cash_per_share": 0.50,
    }
    risk = covered_call_early_assignment_risk(
        [event], "2026-09-11", sessions, strike=100.0, spot=105.0, call_bid=5.20,
    )
    assert risk["ex_date"] == "2026-09-14"
    assert risk["call_extrinsic_value"] == pytest.approx(0.20)
    assert covered_call_early_assignment_risk(
        [event], "2026-09-10", sessions, strike=100.0, spot=105.0, call_bid=5.20,
    ) is None
    assert covered_call_early_assignment_risk(
        [event], "2026-09-11", sessions, strike=100.0, spot=105.0, call_bid=5.60,
    ) is None
    assert covered_call_early_assignment_risk(
        [event], "2026-09-11", sessions, strike=100.0, spot=105.0, call_bid=5.20,
        expiry="2026-09-11",
    ) is None
    unknown = covered_call_early_assignment_risk(
        [event], "2026-09-11", sessions, strike=100.0, spot=105.0, call_bid=None,
    )
    assert unknown["risk_unknown"] is True
    assert unknown["call_extrinsic_value"] is None


def test_earnings_blackout_ignores_schedule_until_snapshot_is_visible():
    rows = ({
        "event_date": "2026-10-29",
        "visible_from": "2026-10-20",
        "event_type": "earnings",
    },)
    assert not earnings_blackout(rows, "2026-10-19", before_days=7, after_days=2)
    assert earnings_blackout(rows, "2026-10-22", before_days=7, after_days=2)


def test_same_day_earnings_snapshot_is_not_visible_at_decision_open():
    rows = ({
        "event_date": "2026-10-29",
        "visible_from": "2026-10-22",
        "event_type": "earnings",
    },)
    assert not earnings_blackout(rows, "2026-10-22", before_days=7, after_days=2)


def test_filing_event_does_not_create_pre_filing_blackout():
    rows = ({
        "event_date": "2026-10-29",
        "event_role": "filing",
        "visible_from": "2026-10-29",
    },)
    assert not earnings_blackout(rows, "2026-10-28", before_days=7, after_days=2)
    assert not earnings_blackout(rows, "2026-10-29", before_days=7, after_days=2)
    assert earnings_blackout(rows, "2026-10-30", before_days=7, after_days=2)
