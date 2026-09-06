import pandas as pd

from common.options.chain import OPTION_CHAIN_COLUMNS, normalize_option_chain


def _valid_chain():
    return pd.DataFrame([{
        'code': 'US.AAPL260918P320000',
        'option_type': 'PUT',
        'strike_price': 320.0,
        'strike_time': '2026-09-18',
        'stock_price': 325.0,
        'bid_price': 3.1,
        'ask_price': 3.3,
        'last_price': 3.2,
        'volume': 100,
        'open_interest': 2000,
        'implied_volatility': 0.25,
        'delta': -0.4,
        'gamma': 0.02,
        'theta': -0.03,
        'vega': 0.12,
        'rho': -0.05,
        'option_contract_multiplier': 100,
        'currency': 'USD',
    }])


def test_option_chain_normalization_has_stable_schema_and_timestamp():
    result = normalize_option_chain(
        _valid_chain(),
        'US.AAPL',
        timestamp='2026-01-02T10:00:00Z',
        as_of='2026-01-02T10:00:00Z',
    )

    assert list(result.columns) == list(OPTION_CHAIN_COLUMNS)
    assert result.iloc[0]['underlying'] == 'US.AAPL'
    assert result.iloc[0]['option_type'] == 'PUT'
    assert result.iloc[0]['contract_multiplier'] == 100
    assert result.iloc[0]['timestamp'] == pd.Timestamp('2026-01-02T10:00:00Z')


def test_option_chain_missing_multiplier_duplicate_and_expired_fail_closed():
    missing_multiplier = _valid_chain().drop(columns=['option_contract_multiplier'])
    assert normalize_option_chain(missing_multiplier, 'US.AAPL') is None

    duplicate = pd.concat([_valid_chain(), _valid_chain()], ignore_index=True)
    assert normalize_option_chain(duplicate, 'US.AAPL') is None

    assert normalize_option_chain(
        _valid_chain(),
        'US.AAPL',
        as_of='2027-01-01T00:00:00Z',
    ) is None


def test_option_chain_missing_quote_fails_closed_but_discovery_mode_is_explicit():
    incomplete = _valid_chain().drop(columns=['bid_price'])
    assert normalize_option_chain(
        incomplete, 'US.AAPL', timestamp='2026-01-02T10:00:00Z'
    ) is None
    discovered = normalize_option_chain(
        incomplete, 'US.AAPL', timestamp='2026-01-02T10:00:00Z',
        require_quotes=False,
    )
    assert discovered is not None
    assert 'bid' in discovered.attrs['missing_quote_fields']


def test_chain_without_source_timestamp_is_marked_caller_timestamp():
    result = normalize_option_chain(
        _valid_chain(), 'US.AAPL', timestamp='2026-01-02T10:00:00Z'
    )
    assert result.attrs['timestamp_source'] == 'caller'
