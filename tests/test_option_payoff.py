from types import SimpleNamespace

import pytest

from common.options.payoff import (
    OptionLeg,
    PayoffInputError,
    UnderlyingLeg,
    analyze_payoff,
    format_payoff_plan,
)
from strategies.base_strategy import BaseStrategy


def test_short_put_reports_profit_loss_and_breakeven():
    analysis = analyze_payoff([
        OptionLeg("SPY-P100", "PUT", -1, 100, 5, 100),
    ], spot=100)

    assert analysis.max_profit == 500
    assert analysis.max_loss == 9500
    assert analysis.breakevens == (95.0,)
    assert analysis.profit_intervals
    assert analysis.loss_intervals


def test_covered_call_is_bounded_and_naked_call_loss_is_unbounded():
    covered = analyze_payoff(
        [OptionLeg("C110", "CALL", -1, 110, 5, 100)],
        [UnderlyingLeg("SPY", 100, 100)],
        spot=100,
    )
    naked = analyze_payoff([
        OptionLeg("C110", "CALL", -1, 110, 5, 100),
    ], spot=100)

    assert covered.unbounded_loss is False
    assert covered.max_profit == pytest.approx(1500)
    assert naked.unbounded_loss is True
    assert naked.max_loss is None


def test_payoff_plan_contains_leg_table_and_ranges():
    analysis = analyze_payoff([
        OptionLeg("SPY-P100", "PUT", -1, 100, 5, 100),
    ], spot=100)
    content = format_payoff_plan("CSP", analysis, [
        OptionLeg("SPY-P100", "PUT", -1, 100, 5, 100),
    ])

    assert "最大盈利" in content
    assert "盈亏平衡点" in content
    assert "SPY-P100" in content


def test_mixed_expiry_strategy_is_rejected_by_static_payoff_model():
    with pytest.raises(PayoffInputError, match="mixed expiries"):
        analyze_payoff([
            OptionLeg("P100", "PUT", -1, 100, 5, 100, "2026-01-16"),
            OptionLeg("P100B", "PUT", -1, 100, 5, 100, "2026-02-20"),
        ])


def test_missing_contract_multiplier_fails_closed():
    with pytest.raises(PayoffInputError, match='contract_multiplier'):
        analyze_payoff([
            OptionLeg("P100", "PUT", -1, 100, 5),
        ])


class _PlanStrategy(BaseStrategy):
    params = {}

    def init(self):
        pass

    def next(self):
        pass


def test_strategy_option_payoff_uses_live_plan_boundary(monkeypatch):
    pushed = []

    class Broker:
        is_live = True

        def _runtime_setting(self, name, default=None):
            return True if name == "PRINT_PLAN" else default

        def log(self, *_args, **_kwargs):
            pass

    strategy = _PlanStrategy(Broker())
    monkeypatch.setattr(
        "strategies.base_strategy.runtime_notifications.push_plan",
        lambda content, level="INFO": pushed.append((content, level)) or True,
    )

    strategy.publish_option_payoff(
        [OptionLeg("SPY-P100", "PUT", -1, 100, 5, 100)],
        spot=100,
        title="CSP Plan",
    )

    assert len(pushed) == 1
    assert "CSP Plan" in pushed[0][0]
