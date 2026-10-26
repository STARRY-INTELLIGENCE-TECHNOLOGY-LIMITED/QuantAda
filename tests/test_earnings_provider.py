import pandas as pd

from data_providers.earnings_provider import EarningsDataProvider


def test_earnings_archive_preserves_visible_from_and_supports_pit(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    archive = tmp_path / "earnings.csv"
    pd.DataFrame([
        {
            "symbol": "AAPL",
            "event_date": "2026-10-29",
            "snapshot_date": "2026-10-01",
            "period_text": "Sep/2026",
            "source": "nasdaq_archive",
        },
    ]).to_csv(archive, index=False)
    monkeypatch.setattr(config, "EARNINGS_ARCHIVE_PATH", str(archive))

    result = EarningsDataProvider().get_earnings_calendar(
        symbols=["US.AAPL"], start_date="20261001", end_date="20261101",
        as_of="2026-10-02",
    )

    assert len(result) == 1
    assert result.attrs["as_of_supported"] is True
    assert result.iloc[0]["visible_from"] == pd.Timestamp("2026-10-01")


def test_earnings_provider_refuses_historical_claim_without_archive(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "EARNINGS_ARCHIVE_PATH", str(tmp_path / "missing.csv"))
    provider = EarningsDataProvider()
    provider._download_day = lambda _day: {"data": {"rows": []}}

    result = provider.get_earnings_calendar(
        symbols=["US.AAPL"], start_date="2024-01-01", end_date="2024-01-02",
        as_of="2024-01-01",
    )

    assert result is None


def test_earnings_current_capture_is_idempotent(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "EARNINGS_ARCHIVE_PATH", str(tmp_path / "earnings.csv"))
    provider = EarningsDataProvider()
    provider._download_day = lambda _day: {
        "data": {"rows": [{"symbol": "AAPL", "fiscalQuarterEnding": "Sep/2026"}]}
    }
    assert provider.capture_current("2026-10-25", "2026-10-25") == 1
    assert provider.capture_current("2026-10-25", "2026-10-25") == 1
    stored = pd.read_csv(tmp_path / "earnings.csv")
    assert len(stored) == 1


def test_archive_does_not_claim_coverage_before_first_snapshot(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    archive = tmp_path / "earnings.csv"
    pd.DataFrame([{
        "symbol": "AAPL", "event_date": "2026-10-29", "snapshot_date": "2026-10-20",
    }]).to_csv(archive, index=False)
    monkeypatch.setattr(config, "EARNINGS_ARCHIVE_PATH", str(archive))
    provider = EarningsDataProvider()
    provider._download_day = lambda _day: {"data": {"rows": []}}

    result = provider.get_earnings_calendar(
        ["US.AAPL"], "2025-01-01", "2025-12-31", as_of="2025-12-31",
    )

    assert result is None


def test_archive_does_not_claim_empty_symbol_coverage_from_other_ticker(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    archive = tmp_path / "earnings.csv"
    pd.DataFrame([{
        "symbol": "MSFT", "event_date": "2026-10-29", "snapshot_date": "2026-10-20",
    }]).to_csv(archive, index=False)
    monkeypatch.setattr(config, "EARNINGS_ARCHIVE_PATH", str(archive))
    provider = EarningsDataProvider()

    result = provider.get_earnings_calendar(
        ["US.AAPL"], "2026-10-01", "2026-10-31", as_of="2026-10-22",
    )

    assert result is None


def test_archive_does_not_claim_point_in_time_coverage_for_partial_symbol_set(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    archive = tmp_path / "earnings.csv"
    pd.DataFrame([
        {"symbol": "AAPL", "event_date": "2026-10-29", "snapshot_date": "2026-10-20"},
    ]).to_csv(archive, index=False)
    monkeypatch.setattr(config, "EARNINGS_ARCHIVE_PATH", str(archive))

    result = EarningsDataProvider().get_earnings_calendar(
        ["US.AAPL", "US.MSFT"], "2026-10-01", "2026-10-31", as_of="2026-10-22",
    )

    assert result is None
