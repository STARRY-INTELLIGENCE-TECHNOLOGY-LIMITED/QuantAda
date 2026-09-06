import pytest

from common.options.risk import (
    OptionMarginError,
    OptionRiskLeg,
    compute_option_margin,
)


def test_cash_secured_put_reports_margin_assignment_and_stress():
    leg = OptionRiskLeg(
        'US.AAPL260918P320000', 'US.AAPL', 'PUT', -1, 320, 5, 300, 100
    )
    snapshot = compute_option_margin([leg], cash=40000)

    assert snapshot.margin_used == 32000
    assert snapshot.available_margin == 8000
    assert snapshot.assignment_obligations[0]['type'] == 'PUT_ASSIGNMENT_BUY'
    assert snapshot.max_loss_estimate == 31500


def test_covered_call_requires_underlying_and_rejects_naked_short():
    leg = OptionRiskLeg(
        'US.AAPL260918C320000', 'US.AAPL', 'CALL', -1, 320, 5, 300, 100
    )
    snapshot = compute_option_margin(
        [leg], cash=1000, underlying_positions={'US.AAPL': 100}
    )
    assert snapshot.margin_used == 0
    assert snapshot.assignment_obligations[0]['type'] == 'CALL_ASSIGNMENT_DELIVER'

    with pytest.raises(OptionMarginError, match='covered call'):
        compute_option_margin([leg], cash=1000, underlying_positions={})


def test_multi_leg_and_insufficient_put_collateral_fail_closed():
    put = OptionRiskLeg('P', 'US.AAPL', 'PUT', -1, 320, 5, 300, 100)
    call = OptionRiskLeg('C', 'US.AAPL', 'CALL', 1, 320, 5, 300, 100)
    with pytest.raises(OptionMarginError, match='multi-leg'):
        compute_option_margin([put, call], cash=100000)
    with pytest.raises(OptionMarginError, match='collateral'):
        compute_option_margin([put], cash=100)
