import math

import pytest

from common.options.greeks import (
    OptionGreeks,
    aggregate_option_greeks,
    compute_option_greeks,
    effective_volatility,
)


def test_greeks_are_finite_and_iv_precedes_hv():
    assert effective_volatility(0.2, 0.4) == 0.2
    assert effective_volatility(None, 0.4) == 0.4
    greeks = compute_option_greeks('CALL', 100, 100, 30 / 365, 0.2, contract_multiplier=100)
    assert all(math.isfinite(value) for value in greeks)
    assert greeks.delta > 0
    assert greeks.gamma > 0


def test_put_call_signs_and_zero_volatility_are_stable():
    call = compute_option_greeks('CALL', 100, 100, 0, 0, contract_multiplier=100)
    put = compute_option_greeks('PUT', 100, 100, 0, 0, contract_multiplier=100)
    assert call.delta == 0
    assert put.delta == 0
    assert all(math.isfinite(value) for value in call)


def test_combination_aggregation_uses_signed_quantity():
    leg = OptionGreeks(1, 2, 3, 4, 5)
    result = aggregate_option_greeks([(leg, 2), (leg, -1)])
    assert result == OptionGreeks(1, 2, 3, 4, 5)


def test_percentage_volatility_and_rates_are_normalized_explicitly():
    assert effective_volatility(25, None, units='percent') == 0.25
    decimal = compute_option_greeks(
        'CALL', 100, 100, 30 / 365, 0.25,
        risk_free_rate=0.05,
        contract_multiplier=1,
    )
    percent = compute_option_greeks(
        'CALL', 100, 100, 30 / 365, 25,
        risk_free_rate=5,
        volatility_units='percent',
        rate_units='percent',
        contract_multiplier=1,
    )
    assert percent.delta == pytest.approx(decimal.delta)
    assert percent.gamma == pytest.approx(decimal.gamma)
