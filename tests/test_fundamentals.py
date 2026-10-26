import pandas as pd

from common.fundamentals import merge_fundamental_snapshots, normalize_fundamental_snapshots


def test_fundamental_snapshots_are_aligned_by_filing_date():
    price = pd.DataFrame(
        {"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0], "close": [1.0, 1.0], "volume": [1, 1]},
        index=pd.to_datetime(["2026-01-01", "2026-02-01"]),
    )
    snapshots = pd.DataFrame([
        {
            "ticker": "US.AAPL", "asof": "2026-01-15", "report_end": "2025-12-31",
            "revenue": 100, "gross_profit": 40, "operating_income": 20,
            "net_income": 15, "stock_based_compensation": 5,
            "cash": 80, "debt": 30, "shares": 10, "buybacks": 2,
            "source_id": "sec:1",
        },
    ])

    result = merge_fundamental_snapshots(price, snapshots, "US.AAPL")

    assert result["fundamental_status"].tolist() == ["no_snapshot", "available"]
    assert pd.isna(result["fundamental_revenue"].iloc[0])
    assert result["fundamental_revenue"].iloc[1] == 100.0
    assert result["fundamental_source_id"].tolist() == ["", "sec:1"]


def test_fundamental_normalizer_rejects_duplicate_filing_dates():
    frame = pd.DataFrame([
        {"ticker": "US.AAPL", "asof": "2026-01-01", "report_end": "2025-12-31", "source_id": "a"},
        {"ticker": "US.AAPL", "asof": "2026-01-01", "report_end": "2025-12-31", "source_id": "b"},
    ])
    result = normalize_fundamental_snapshots(frame)
    assert len(result) == 1
    assert result.iloc[0]["source_id"] == "b"
