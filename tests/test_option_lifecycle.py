import pytest

from common.options.lifecycle import (
    ExpiredOptionError,
    OptionContract,
    assert_option_tradeable,
    roll_option_position,
    settle_option_expiry,
)


def test_otm_expiry_zeroes_option_without_cash_or_underlying_change():
    contract = OptionContract('US.AAPL260918C320000', 'CALL', 320, '2026-09-18', 100)
    result = settle_option_expiry(
        contract,
        2,
        300,
        cash=1000,
        underlying_position=5,
        at='2026-09-18',
    )

    assert result.event == 'EXPIRED_OTM'
    assert result.option_position_after == 0
    assert result.cash_after == 1000
    assert result.underlying_after == 5


def test_itm_put_physical_assignment_changes_cash_and_underlying_by_multiplier():
    contract = OptionContract('US.AAPL260918P320000', 'PUT', 320, '2026-09-18', 100)
    result = settle_option_expiry(
        contract,
        1,
        300,
        cash=1000,
        underlying_position=0,
        at='2026-09-18',
    )

    assert result.event == 'EXERCISED_PUT'
    assert result.cash_delta == 32000
    assert result.underlying_delta == -100


def test_itm_call_cash_settlement_and_short_put_assignment_are_signed():
    call = OptionContract('US.AAPL260918C320000', 'CALL', 320, '2026-09-18', 100, 'cash')
    result = settle_option_expiry(call, 2, 350, cash=1000, at='2026-09-18')
    assert result.event == 'EXERCISED_CASH'
    assert result.cash_delta == 6000

    put = OptionContract('US.AAPL260918P320000', 'PUT', 320, '2026-09-18', 100)
    assigned = settle_option_expiry(put, -1, 300, cash=1000, at='2026-09-18')
    assert assigned.event == 'ASSIGNED_PUT'
    assert assigned.cash_delta == -32000
    assert assigned.underlying_delta == 100


def test_expired_contract_is_not_tradeable_and_roll_is_deterministic():
    old = OptionContract('US.AAPL260918C320000', 'CALL', 320, '2026-09-18', 100)
    new = OptionContract('US.AAPL261016C325000', 'CALL', 325, '2026-10-16', 100)
    with pytest.raises(ExpiredOptionError):
        assert_option_tradeable(old, '2026-09-19')

    rolled = roll_option_position(
        old,
        new,
        1,
        old_spot=300,
        new_price=4,
        cash=1000,
        at='2026-09-18',
    )
    assert rolled.new_symbol == new.symbol
    assert rolled.new_option_position == 1
    assert rolled.new_cash == 600


def test_pre_expiry_roll_closes_at_market_price_without_assignment():
    old = OptionContract('P100-OLD', 'PUT', 100, '2026-09-18', 100)
    new = OptionContract('P100-NEW', 'PUT', 100, '2026-10-16', 100)
    rolled = roll_option_position(
        old,
        new,
        -1,
        old_spot=98,
        old_price=3,
        new_price=4,
        cash=1000,
        at='2026-08-20',
    )
    assert rolled.expiry.event == 'ROLLED_PRE_EXPIRY'
    assert rolled.expiry.expired is False
    # 空头买平支付 300，再卖开新合约收入 400。
    assert rolled.expiry.cash_delta == -300
    assert rolled.new_cash == 1100


def test_dividend_requires_explicit_entitlement_and_supports_dated_events():
    contract = OptionContract('P100', 'PUT', 100, '2026-09-18', 100)
    with pytest.raises(ValueError, match='dividend_entitled'):
        settle_option_expiry(
            contract, -1, 110, cash=1000, dividend_per_share=1,
            at='2026-09-18',
        )
    result = settle_option_expiry(
        contract,
        -1,
        110,
        cash=1000,
        underlying_position=100,
        dividend_events=[
            {'ex_date': '2026-08-01', 'per_share': 1},
            {'ex_date': '2026-10-01', 'per_share': 9},
        ],
        at='2026-09-18',
    )
    # OTM 期权本身不产生现金；仅计入到期日前已发生且明确提供的股息。
    assert result.event == 'EXPIRED_OTM'
    assert result.cash_delta == 100
