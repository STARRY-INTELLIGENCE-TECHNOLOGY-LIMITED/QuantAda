import pandas as pd
import pytest

import config
import data_providers.sec_provider as sec_module
from data_providers.base_provider import BaseDataProvider
from data_providers.manager import DataManager
from data_providers.sec_provider import SecDataProvider, point_in_time_fcfe_rows_from_sec_facts


def _fact(value, start, end, filed, accn):
    return {
        "form": "10-K",
        "fp": "FY",
        "start": start,
        "end": end,
        "filed": filed,
        "accn": accn,
        "val": value,
    }


def _facts():
    years = [
        (2022, 80_000_000.0, 10_000_000.0, 10_000_000.0),
        (2023, 100_000_000.0, 10_000_000.0, 10_000_000.0),
        (2024, 120_000_000.0, 10_000_000.0, 10_000_000.0),
    ]
    cfo, capex, shares = [], [], []
    for year, cfo_value, capex_value, share_value in years:
        end = f"{year}-12-31"
        filed = f"{year + 1}-02-15"
        start = f"{year}-01-01"
        accn = f"0000000000-{year + 1}-000001"
        cfo.append(_fact(cfo_value, start, end, filed, accn))
        capex.append(_fact(capex_value, start, end, filed, accn))
        shares.append(_fact(share_value, start, end, filed, accn))
    return {
        "facts": {
            "us-gaap": {
                "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": cfo}},
                "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": capex}},
                "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": shares}},
            }
        }
    }


def test_sec_annual_facts_to_point_in_time_fcfe_estimate():
    rows = point_in_time_fcfe_rows_from_sec_facts(_facts())

    assert len(rows) == 3
    assert list(rows["fcfe"]) == [70_000_000.0, 90_000_000.0, 110_000_000.0]
    estimates = SecDataProvider()
    estimates.user_agent = "QuantAda test contact test@example.com"
    estimates._load_json = lambda cache_name, url: (
        {"0": {"cik_str": 1234, "ticker": "AAA", "title": "Example"}}
        if cache_name == "company_tickers"
        else _facts()
    )

    result = estimates.get_valuation_snapshots("US.AAA")

    assert result is not None
    assert list(result["ticker"]) == ["US.AAA"]
    assert result.loc[0, "asof"] == pd.Timestamp("2025-02-15")
    assert result.loc[0, "v"] == pytest.approx(90_000_000 * 1.02 / (0.10 - 0.02) / 10_000_000)
    assert result.loc[0, "method"] == "fcfe_perpetuity_v1"
    assert result.loc[0, "source_id"] == "sec:0000000000-2025-000001"


def _add_quarter(facts, *, year, quarter, filed, cfo, capex):
    end_by_quarter = {1: (3, 31), 2: (6, 30), 3: (9, 30)}
    month, day = end_by_quarter[quarter]
    start = f"{year}-01-01"
    end = f"{year}-{month:02d}-{day:02d}"
    form = "10-Q"
    fp = f"Q{quarter}"
    accn = f"0000000000-{filed[:4]}-{quarter:06d}"
    for tag, value, unit in (
        ("NetCashProvidedByUsedInOperatingActivities", cfo, "USD"),
        ("PaymentsToAcquirePropertyPlantAndEquipment", capex, "USD"),
        ("WeightedAverageNumberOfDilutedSharesOutstanding", 10_000_000.0, "shares"),
    ):
        rows = facts["facts"]["us-gaap"][tag]["units"][unit]
        rows.append({
            "form": form,
            "fp": fp,
            "start": start,
            "end": end,
            "filed": filed,
            "accn": accn,
            "val": value,
        })


def test_sec_10q_filing_rebuilds_ttm_fcfe_without_future_periods():
    facts = _facts()
    for year, quarter, filed, cfo, capex in (
        (2024, 1, "2024-05-01", 25_000_000.0, 2_000_000.0),
        (2024, 2, "2024-08-01", 60_000_000.0, 3_000_000.0),
        (2024, 3, "2024-11-01", 85_000_000.0, 5_000_000.0),
        (2025, 1, "2025-05-01", 30_000_000.0, 2_000_000.0),
        (2025, 2, "2025-08-01", 65_000_000.0, 4_000_000.0),
        (2025, 3, "2025-11-01", 95_000_000.0, 6_000_000.0),
    ):
        _add_quarter(facts, year=year, quarter=quarter, filed=filed, cfo=cfo, capex=capex)

    provider = SecDataProvider()
    provider.user_agent = "QuantAda test contact test@example.com"
    provider._load_json = lambda cache_name, url: (
        {"0": {"cik_str": 1234, "ticker": "AAA", "title": "Example"}}
        if cache_name == "company_tickers"
        else facts
    )
    snapshots = provider.get_valuation_snapshots("US.AAA")

    quarter_values = snapshots.loc[snapshots["asof"].ge(pd.Timestamp("2025-05-01"))]
    assert list(quarter_values["asof"]) == [
        pd.Timestamp("2025-05-01"),
        pd.Timestamp("2025-08-01"),
        pd.Timestamp("2025-11-01"),
    ]
    assert quarter_values["v"].notna().all()
    assert quarter_values["method"].eq("fcfe_perpetuity_v1").all()
    assert quarter_values["v"].iloc[-1] > quarter_values["v"].iloc[0]


def test_sec_provider_refuses_non_us_symbols_and_is_not_an_ohlcv_source():
    provider = SecDataProvider()
    provider._load_json = lambda *_args: None
    assert provider.get_data("US.AAA") is None
    assert provider.get_valuation_snapshots("HK.00700") is not None
    assert provider.get_valuation_snapshots("US.AAA") is None


def test_sec_provider_exposes_actual_filing_dates_as_visible_events():
    provider = SecDataProvider()
    provider._load_json = lambda cache_name, url: (
        {"0": {"cik_str": 1234, "ticker": "AAA", "title": "Example"}}
        if cache_name == "company_tickers" else _facts()
    )

    result = provider.get_earnings_calendar(
        symbols=["US.AAA"], start_date="2023-01-01", end_date="2026-12-31",
    )

    assert result is not None
    assert result.attrs["as_of_supported"] is True
    assert result.iloc[0]["event_date"] == pd.Timestamp("2023-02-15")
    assert result.iloc[0]["visible_from"] == pd.Timestamp("2023-02-15")
    assert result.iloc[0]["event_role"] == "filing"


def test_sec_provider_extracts_extended_fundamentals_from_companyfacts():
    facts = _facts()
    facts["facts"]["us-gaap"].update({
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {"USD": [_fact(200_000_000.0, "2022-01-01", "2022-12-31", "2023-02-15", "r1"),
                              _fact(220_000_000.0, "2023-01-01", "2023-12-31", "2024-02-15", "r2"),
                              _fact(240_000_000.0, "2024-01-01", "2024-12-31", "2025-02-15", "r3")]}
        },
        "GrossProfit": {"units": {"USD": [_fact(80_000_000.0, "2022-01-01", "2022-12-31", "2023-02-15", "r1"),
                                             _fact(90_000_000.0, "2023-01-01", "2023-12-31", "2024-02-15", "r2"),
                                             _fact(100_000_000.0, "2024-01-01", "2024-12-31", "2025-02-15", "r3")]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            {"form": "10-K", "fp": "FY", "end": "2024-12-31", "filed": "2025-02-15", "val": 70_000_000.0}
        ]}},
    })
    provider = SecDataProvider()
    provider._load_json = lambda cache_name, url: (
        {"0": {"cik_str": 1234, "ticker": "AAA", "title": "Example"}}
        if cache_name == "company_tickers" else facts
    )

    result = provider.get_fundamental_snapshots("US.AAA")

    assert result is not None and not result.empty
    latest = result.iloc[-1]
    assert latest["revenue"] == 240_000_000.0
    assert latest["gross_profit"] == 100_000_000.0
    assert latest["cash"] == 70_000_000.0


def test_sec_raw_companyfacts_cache_is_reused_across_provider_instances(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path), raising=False)
    calls = []

    def download(url, user_agent):
        calls.append((url, user_agent))
        return {"facts": {}}

    monkeypatch.setattr(sec_module, "_download_json", download)
    first = SecDataProvider()
    first.user_agent = "QuantAda test contact test@example.com"
    assert first._load_json("companyfacts_0000001234", "https://example.test/facts") == {"facts": {}}

    second = SecDataProvider()
    second.user_agent = ""
    assert second._load_json("companyfacts_0000001234", "https://example.test/facts") == {"facts": {}}
    assert len(calls) == 1


class PriceDataProvider(BaseDataProvider):
    PRIORITY = 1

    def __init__(self):
        self.calls = 0

    def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        self.calls += 1
        index = pd.to_datetime(["2026-09-11", "2026-09-14", "2026-09-15"])
        return pd.DataFrame(
            {"open": [90, 91, 92], "high": [91, 92, 93], "low": [89, 90, 91],
             "close": [90, 91, 92], "volume": [1000, 1100, 1200]},
            index=index,
        )


class ValuationDataProvider(BaseDataProvider):
    PRIORITY = 2
    VALUATION_ONLY = True
    VALUATION_MAX_AGE_DAYS = 550

    def __init__(self):
        self.calls = 0

    def get_data(self, symbol, start_date=None, end_date=None, timeframe="Days", compression=1):
        return None

    def get_valuation_snapshots(self, symbol, start_date=None, end_date=None):
        self.calls += 1
        return pd.DataFrame(
            [{"ticker": symbol, "asof": "2026-09-11", "v": 100, "confidence": 7.0,
              "method": "test", "source_id": "test-report"}]
        )


def test_data_manager_attaches_and_caches_valuation_columns(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path), raising=False)
    monkeypatch.setattr(config, "CACHE_DATA", True, raising=False)
    providers = [PriceDataProvider(), ValuationDataProvider()]
    monkeypatch.setattr(
        DataManager,
        "auto_discover_and_sort_providers",
        lambda self: providers,
    )
    manager = DataManager()

    frame = manager.get_data(
        "US.AAA",
        "20260911",
        "20260915",
        specified_sources="price,valuation",
    )

    assert frame is not None
    assert frame["underlying_fair_value"].tolist() == [100.0, 100.0, 100.0]
    assert frame["underlying_weighted_value"].isna().all()
    assert frame["valuation_status"].tolist() == ["available"] * 3
    assert frame["valuation_asof"].tolist() == [pd.Timestamp("2026-09-11")] * 3
    cached = pd.read_csv(tmp_path / "market_cache" / "US_AAA.csv")
    assert "underlying_fair_value" in cached.columns
    assert "valuation_source_id" in cached.columns

    repeated = manager.get_data(
        "US.AAA",
        "20260911",
        "20260915",
        specified_sources="price,valuation",
    )
    assert repeated["valuation_source_id"].tolist() == ["test-report"] * 3
    assert providers[0].calls == 1
    assert providers[1].calls == 2
