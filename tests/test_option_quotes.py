from types import SimpleNamespace

import pandas as pd

from strategies.options.support import matches_chain_window, quote_snapshot


def _params():
    return SimpleNamespace(
        min_dte=1,
        max_dte=60,
        min_delta=-0.30,
        max_delta=-0.10,
        min_open_interest=0.0,
        min_iv_percentile=0.0,
        max_spread_pct=0.20,
    )


def _meta():
    return {"expiry": pd.Timestamp("2026-11-20")}


def test_last_trade_does_not_fabricate_an_executable_nbbo():
    quote = quote_snapshot(pd.Series({"close": 1.25, "open_interest": 1000}))

    assert quote["last"] == 1.25
    assert pd.isna(quote["bid"])
    assert pd.isna(quote["ask"])
    assert quote["has_nbbo"] is False
    assert not matches_chain_window(_meta(), quote, "2026-10-23", _params())


def test_zero_size_top_of_book_is_not_executable():
    quote = quote_snapshot(pd.Series({
        "close": 1.25,
        "bid": 1.20,
        "ask": 1.25,
        "bid_size": 0,
        "ask_size": 10,
        "open_interest": 1000,
    }))

    assert quote["has_nbbo"] is False
    assert not matches_chain_window(_meta(), quote, "2026-10-23", _params())


def test_valid_nbbo_is_kept_and_passes_chain_window():
    quote = quote_snapshot(pd.Series({
        "close": 1.25,
        "bid": 1.20,
        "ask": 1.25,
        "bid_size": 10,
        "ask_size": 10,
        "open_interest": 1000,
        "delta": -0.20,
    }))

    assert quote["has_nbbo"] is True
    assert matches_chain_window(_meta(), quote, "2026-10-23", _params())
