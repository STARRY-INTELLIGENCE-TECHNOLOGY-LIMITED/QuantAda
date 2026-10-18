from datetime import datetime

import pandas as pd

from common.options.analytics import parse_expiry, parse_option_symbol
from strategies.options.support import underlying_feed


class _Feed:
    def __init__(self, name):
        self._name = name


def test_parse_expiry_compact_matches_strptime_rules():
    assert parse_expiry("240119") == pd.Timestamp(datetime.strptime("240119", "%y%m%d"))
    assert parse_expiry("690101") == pd.Timestamp(datetime.strptime("690101", "%y%m%d"))
    assert parse_expiry("20240119") == pd.Timestamp(datetime.strptime("20240119", "%Y%m%d"))
    assert pd.isna(parse_expiry("20241340"))
    assert parse_expiry("2024-01-19") == pd.Timestamp("2024-01-19")


def test_parse_option_symbol_cache_keeps_futu_and_occ_strikes():
    futu = parse_option_symbol("US.MARA261016P9000")
    occ = parse_option_symbol("US.AAPL240119P00150000")
    again = parse_option_symbol("US.AAPL240119P00150000")

    assert futu["strike"] == 9.0
    assert occ["strike"] == 150.0
    assert occ["option_type"] == "PUT"
    assert occ["expiry"] == pd.Timestamp("2024-01-19")
    assert again == occ
    assert again is not occ


def test_underlying_feed_finds_stock_and_skips_option_symbol():
    stock = _Feed("US.AAPL")
    option = _Feed("US.AAPL240119P00150000")
    broker = type("Broker", (), {})()
    broker.datas = [option, stock]

    assert underlying_feed(broker, "AAPL") is stock
    assert underlying_feed(broker, "US.AAPL") is stock
    assert underlying_feed(broker, option._name) is None


def test_underlying_feed_reuses_index_without_reparsing(monkeypatch):
    stock = _Feed("US.AAPL")
    options = [_Feed("US.AAPL240119C%08d" % strike) for strike in range(100, 180)]
    broker = type("Broker", (), {})()
    broker.datas = [stock, *options]
    calls = {"n": 0}
    real = parse_option_symbol

    def counting(symbol, strike_scale=1000.0):
        calls["n"] += 1
        return real(symbol, strike_scale=strike_scale)

    monkeypatch.setattr("strategies.options.support.parse_option_symbol", counting)

    assert underlying_feed(broker, "AAPL") is stock
    built = calls["n"]
    assert built == len(broker.datas)
    for _ in range(30):
        assert underlying_feed(broker, "US.AAPL") is stock
        assert underlying_feed(broker, options[0]._name) is None
    assert calls["n"] == built


def test_underlying_feed_index_invalidates_on_replace_and_append():
    old = _Feed("US.AAPL")
    broker = type("Broker", (), {})()
    broker.datas = [old]
    assert underlying_feed(broker, "AAPL") is old

    replacement = _Feed("US.MSFT")
    broker.datas = [replacement]
    assert underlying_feed(broker, "AAPL") is None
    assert underlying_feed(broker, "MSFT") is replacement

    added = _Feed("US.NVDA")
    broker.datas.append(added)
    assert underlying_feed(broker, "NVDA") is added
    assert underlying_feed(broker, "MSFT") is replacement


def test_underlying_feed_keeps_first_match():
    first = _Feed("US.AAPL")
    second = _Feed("NASDAQ.AAPL")
    broker = type("Broker", (), {})()
    broker.datas = [first, second]

    assert underlying_feed(broker, "AAPL") is first


def test_settlement_stock_feeds_reuses_index_and_keeps_last_match(monkeypatch):
    from backtest.option_settlement import settlement_stock_feeds
    from common.options.analytics import parse_option_symbol as real_parse

    first = _Feed("US.AAPL")
    last = _Feed("NASDAQ.AAPL")
    option = _Feed("US.AAPL240119P00150000")
    owner = type("Owner", (), {})()
    owner.datas = [option, first, last]
    calls = {"n": 0}

    def counting(symbol, strike_scale=1000.0):
        calls["n"] += 1
        return real_parse(symbol, strike_scale=strike_scale)

    monkeypatch.setattr("backtest.option_settlement.parse_option_symbol", counting)
    assert settlement_stock_feeds(owner)["AAPL"] is last
    built = calls["n"]
    assert built == 3
    assert settlement_stock_feeds(owner)["AAPL"] is last
    assert calls["n"] == built
    owner.datas.append(_Feed("US.MSFT"))
    assert settlement_stock_feeds(owner)["MSFT"]._name == "US.MSFT"
