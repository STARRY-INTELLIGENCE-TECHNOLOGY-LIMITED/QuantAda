import pandas as pd

from data_providers.rates_provider import RatesDataProvider


def test_rates_provider_normalizes_treasury_percentages():
    raw = pd.DataFrame({
        "Date": ["2024-01-02", "2024-01-03"],
        "1 Mo": [5.40, 5.41],
        "3 Mo": [5.35, 5.36],
        "6 Mo": [5.20, 5.21],
        "1 Yr": [4.80, 4.81],
        "2 Yr": [4.30, 4.31],
    })
    result = RatesDataProvider._normalize(raw)

    assert result["risk_free_rate"].tolist() == [0.0535, 0.0536]
    assert result.attrs == {}


def test_rates_provider_uses_cached_year_without_network(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    provider = RatesDataProvider()
    calls = []
    monkeypatch.setattr(provider, "_download_year", lambda year: calls.append(year) or None)
    path = provider.cache_dir / "treasury_curve_2024.csv"
    path.write_text(
        "date,rate_1m,rate_3m,rate_6m,rate_1y,rate_2y,risk_free_rate\n"
        "2024-01-02,0.054,0.0535,0.052,0.048,0.043,0.0535\n",
        encoding="utf-8",
    )

    result = provider.get_rate_curve("20240101", "20240103")

    assert calls == []
    assert result.iloc[0]["risk_free_rate"] == 0.0535


def test_rates_provider_keeps_stale_cache_when_refresh_fails(tmp_path, monkeypatch):
    import os
    import time
    import config

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    provider = RatesDataProvider()
    path = provider.cache_dir / "treasury_curve_2024.csv"
    path.write_text(
        "date,rate_1m,rate_3m,rate_6m,rate_1y,rate_2y,risk_free_rate\n"
        "2024-01-02,0.054,0.0535,0.052,0.048,0.043,0.0535\n",
        encoding="utf-8",
    )
    old = time.time() - provider.CACHE_TTL_SECONDS - 10
    os.utime(path, (old, old))
    monkeypatch.setattr(provider, "_download_year", lambda _year: None)

    result = provider.get_rate_curve("20240101", "20240103")

    assert result is not None and result.iloc[0]["risk_free_rate"] == 0.0535
