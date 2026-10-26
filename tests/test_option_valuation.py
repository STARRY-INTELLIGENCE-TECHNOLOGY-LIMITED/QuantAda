import pandas as pd
import pytest

from common.options.valuation import (
    load_valuation_snapshots,
    merge_valuation_snapshots,
    normalize_valuation_snapshots,
    resolve_valuation_anchor,
)


def test_snapshot_loader_keeps_audit_fields_and_normalizes_dates(tmp_path):
    path = tmp_path / "valuation.csv"
    pd.DataFrame(
        [
            {
                "ticker": "US.SPY",
                "asof": "2026-09-11",
                "v": 100.0,
                "w": 82.0,
                "confidence": 7.5,
                "method": "dcf",
                "source_id": "report-20260911-1",
            }
        ]
    ).to_csv(path, index=False)

    loaded = load_valuation_snapshots(path)

    assert loaded.loc[0, "ticker"] == "US.SPY"
    assert loaded.loc[0, "asof"] == pd.Timestamp("2026-09-11")
    assert loaded.loc[0, "v"] == 100.0
    assert loaded.loc[0, "w"] == 82.0
    assert loaded.loc[0, "method"] == "dcf"
    assert loaded.loc[0, "source_id"] == "report-20260911-1"


def test_snapshot_point_in_time_lookup_ignores_same_day_and_future_rows():
    snapshots = pd.DataFrame(
        [
            {"ticker": "US.SPY", "asof": "2026-09-11", "v": 100, "w": 80, "confidence": 8, "method": "dcf", "source_id": "old"},
            {"ticker": "US.SPY", "asof": "2026-09-14", "v": 130, "w": 104, "confidence": 9, "method": "dcf", "source_id": "same-day"},
            {"ticker": "US.SPY", "asof": "2026-09-15", "v": 140, "w": 112, "confidence": 9, "method": "dcf", "source_id": "future"},
        ]
    )
    prices = pd.DataFrame(
        {"close": [90, 95]},
        index=pd.to_datetime(["2026-09-11", "2026-09-14"]),
    )

    anchor = resolve_valuation_anchor(
        prices,
        "US.SPY",
        "2026-09-14",
        snapshots=snapshots,
    )

    assert anchor is not None
    assert anchor.v == 100
    assert anchor.w == 80
    assert anchor.asof == pd.Timestamp("2026-09-11")
    assert anchor.source_id == "old"
    assert anchor.source == "point_in_time_snapshot"


def test_snapshot_with_no_prior_value_fails_closed_even_when_proxy_is_enabled():
    snapshots = pd.DataFrame(
        [{"ticker": "US.SPY", "asof": "2026-09-14", "v": 100, "w": 80, "confidence": 8, "method": "dcf", "source_id": "same-day"}]
    )
    prices = pd.DataFrame(
        {"close": [90, 95]},
        index=pd.to_datetime(["2026-09-11", "2026-09-14"]),
    )

    anchor = resolve_valuation_anchor(
        prices,
        "US.SPY",
        "2026-09-14",
        snapshots=snapshots,
        w_multiple=0.8,
        allow_proxy=True,
        min_history=2,
    )

    assert anchor is None


def test_inline_v_derives_w_only_from_explicit_multiplier():
    prices = pd.DataFrame(
        {"close": [90, 95], "underlying_fair_value": [100, 120]},
        index=pd.to_datetime(["2026-09-11", "2026-09-14"]),
    )

    anchor = resolve_valuation_anchor(
        prices,
        "US.SPY",
        "2026-09-14",
        w_multiple=0.8,
    )

    assert anchor is not None
    assert anchor.v == 100
    assert anchor.w == 80
    assert anchor.asof == pd.Timestamp("2026-09-11")
    assert anchor.source == "weighted_derived"


def test_inline_snapshot_asof_must_precede_decision_even_when_price_row_is_earlier():
    prices = pd.DataFrame(
        {
            "close": [90, 95],
            "underlying_fair_value": [100, 120],
            "valuation_asof": ["2026-09-14", "2026-09-14"],
            "valuation_method": ["dcf", "dcf"],
            "valuation_source_id": ["same-day", "same-day"],
        },
        index=pd.to_datetime(["2026-09-11", "2026-09-12"]),
    )

    anchor = resolve_valuation_anchor(
        prices,
        "US.SPY",
        "2026-09-14",
        w_multiple=0.8,
        allow_proxy=True,
        min_history=2,
    )

    assert anchor is None


def test_snapshot_loader_derives_missing_w_only_when_requested():
    source = pd.DataFrame(
        [{"ticker": "US.SPY", "asof": "2026-09-11", "v": 100.0, "confidence": 8, "method": "dcf", "source_id": "source-1"}]
    )

    unweighted = normalize_valuation_snapshots(source)
    assert pd.isna(unweighted.loc[0, "w"])

    normalized = normalize_valuation_snapshots(source, w_multiple=0.8)

    assert normalized.loc[0, "w"] == 80.0


def test_lagged_median_is_an_explicit_proxy_and_never_reads_same_day_close():
    prices = pd.DataFrame(
        {"close": [80, 100, 1000]},
        index=pd.to_datetime(["2026-09-09", "2026-09-10", "2026-09-11"]),
    )

    anchor = resolve_valuation_anchor(
        prices,
        "US.SPY",
        "2026-09-11",
        w_multiple=0.8,
        value_window=2,
        min_history=2,
        allow_proxy=True,
    )

    assert anchor is not None
    assert anchor.v == 90
    assert anchor.w == 72
    assert anchor.asof == pd.Timestamp("2026-09-10")
    assert anchor.source == "lagged_median_proxy"


def test_invalid_or_duplicate_snapshots_are_rejected():
    duplicate = pd.DataFrame(
        [
            {"ticker": "US.SPY", "asof": "2026-09-11", "v": 100, "w": 80, "confidence": 8, "method": "dcf", "source_id": "source-1"},
            {"ticker": "US.SPY", "asof": "2026-09-11", "v": 101, "w": 81, "confidence": 8, "method": "dcf", "source_id": "source-2"},
        ]
    )
    with pytest.raises(ValueError, match="只能有一条"):
        normalize_valuation_snapshots(duplicate)

    invalid = pd.DataFrame(
        [{"ticker": "US.SPY", "asof": "2026-09-11", "v": 0, "w": 80, "confidence": 8, "method": "dcf", "source_id": "source-1"}]
    )
    with pytest.raises(ValueError, match="无效"):
        normalize_valuation_snapshots(invalid)


def test_w_multiple_must_be_a_conservative_positive_ratio():
    with pytest.raises(ValueError, match="w_multiple"):
        normalize_valuation_snapshots(
            pd.DataFrame([{"ticker": "US.SPY", "asof": "2026-09-11", "v": 100, "confidence": 8, "method": "dcf", "source_id": "source-1"}]),
            w_multiple=1.2,
        )


def test_snapshot_audit_identity_is_required():
    source = pd.DataFrame(
        [{"ticker": "US.SPY", "asof": "2026-09-11", "v": 100, "w": 80, "confidence": 8}]
    )

    with pytest.raises(ValueError, match="method, source_id"):
        normalize_valuation_snapshots(source)


def test_stale_external_snapshot_blocks_proxy_fallback():
    prices = pd.DataFrame(
        {"close": [90, 95]},
        index=pd.to_datetime(["2026-09-11", "2026-09-14"]),
    )
    snapshots = pd.DataFrame(
        [{"ticker": "US.SPY", "asof": "2024-01-01", "v": 100, "w": 80,
          "confidence": 8, "method": "dcf", "source_id": "old"}]
    )

    merged = merge_valuation_snapshots(prices, snapshots, "US.SPY", max_age_days=550)
    anchor = resolve_valuation_anchor(
        merged,
        "US.SPY",
        "2026-09-14",
        w_multiple=0.8,
        allow_proxy=True,
        min_history=2,
    )

    assert merged["valuation_status"].tolist() == ["stale", "stale"]
    assert anchor is None


def test_explicit_source_without_estimates_blocks_price_proxy():
    prices = pd.DataFrame(
        {"close": [90, 95]},
        index=pd.to_datetime(["2026-09-11", "2026-09-14"]),
    )
    empty = pd.DataFrame(columns=["ticker", "asof", "v", "w", "confidence", "method", "source_id"])
    merged = merge_valuation_snapshots(prices, empty, "US.SPY", max_age_days=550)

    anchor = resolve_valuation_anchor(
        merged,
        "US.SPY",
        "2026-09-14",
        w_multiple=0.8,
        allow_proxy=True,
        min_history=2,
    )

    assert merged["valuation_status"].tolist() == ["no_snapshot", "no_snapshot"]
    assert anchor is None
