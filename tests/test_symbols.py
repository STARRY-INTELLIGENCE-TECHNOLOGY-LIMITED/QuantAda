from types import SimpleNamespace

from common.symbols import (
    normalize_symbol_key,
    strip_known_venue_suffix,
    symbol_lookup_keys,
    symbols_match,
)


def test_venue_suffix_aliases_match_bidirectionally():
    assert symbols_match('AAPL.SMART', 'AAPL')
    assert symbols_match('AAPL', 'AAPL.ARCA')
    assert symbols_match('QQQ.ISLAND', 'QQQ')
    assert symbols_match('QQQ', 'QQQ.ISLAND')
    assert symbols_match('AAPL.ARCA', 'AAPL.SMART')


def test_market_prefix_symbols_do_not_share_an_alias():
    assert not symbols_match('HK.00700', 'HK.09988')
    assert not symbols_match('SHSE.600519', 'SHSE.510300')
    assert not symbols_match('SZSE.000001', 'SZ.000001')
    assert symbols_match('HK.00700', 'HK.00700')


def test_dotted_tickers_are_not_stripped_as_venues():
    assert strip_known_venue_suffix('BRK.B') == 'BRK.B'
    assert not symbols_match('BRK.A', 'BRK.B')
    assert symbol_lookup_keys('BRK.B') == ('BRK.B',)


def test_symbol_lookup_keys_keep_market_prefix_intact():
    assert symbol_lookup_keys('HK.00700') == ('HK.00700',)
    assert symbol_lookup_keys('AAPL.SMART') == ('AAPL.SMART', 'AAPL')
    assert symbol_lookup_keys('US.AAPL.SMART') == ('US.AAPL.SMART', 'US.AAPL')
    assert normalize_symbol_key(SimpleNamespace(_name='qqq.island')) == 'QQQ.ISLAND'
