import math

from metrics.option_carry_robust import evaluate


def _stats(**overrides):
    values = {
        "option_trade_count": 24,
        "option_risk_data_coverage": 1.0,
        "years": 2.0,
        "total_trades": 36,
        "monthly_win_rate": 0.6,
        "total_return_pct": 8.0,
        "option_annual_risk_return_pct": 8.0,
        "option_premium_yield_pct": 14.0,
        "sharpe": 1.2,
        "mdd": 5.0,
        "option_worst_trade_risk_pct": -8.0,
        "option_cvar10_risk_pct": -6.0,
        "option_worst_mae_pct": 18.0,
    }
    values.update(overrides)
    return values


def test_option_carry_robust_prefers_annualized_return_after_drawdown():
    assert evaluate(_stats(total_return_pct=25.0)) > evaluate(_stats(total_return_pct=8.0))


def test_option_carry_robust_large_drawdown_can_overturn_higher_return():
    steady = evaluate(_stats(total_return_pct=16.0, mdd=4.0))
    blowup = evaluate(_stats(total_return_pct=40.0, mdd=35.0))

    assert steady > blowup


def test_option_carry_robust_low_coverage_does_not_erase_account_return():
    covered = evaluate(_stats())
    thin_coverage = evaluate(_stats(option_risk_data_coverage=0.5))
    better_account = evaluate(_stats(
        option_risk_data_coverage=0.5, total_return_pct=40.0,
    ))
    weak_account = evaluate(_stats(option_risk_data_coverage=1.0, total_return_pct=1.0))

    assert covered > thin_coverage
    assert thin_coverage != -100.0
    assert better_account > weak_account


def test_option_carry_robust_low_sample_stays_ordered_by_account_return():
    better = evaluate(_stats(option_trade_count=6, total_return_pct=30.0))
    busier = evaluate(_stats(option_trade_count=14, total_return_pct=2.0))
    same_return_busier = evaluate(_stats(option_trade_count=14, total_return_pct=30.0))

    assert better > busier
    assert same_return_busier > better


def test_option_carry_robust_stops_rewarding_trades_after_sample_floor():
    enough = evaluate(_stats(option_trade_count=16, total_trades=16))
    busy = evaluate(_stats(option_trade_count=80, total_trades=80))

    assert enough == busy


def test_option_carry_robust_counts_short_option_trades_not_spread_legs():
    short_sample = evaluate(_stats(option_trade_count=11, total_trades=100))
    enough_shorts = evaluate(_stats(option_trade_count=24, total_trades=24))

    assert enough_shorts > short_sample
    assert short_sample != -94.5


def test_option_carry_robust_account_beats_clean_low_return_book():
    rich = evaluate(_stats(
        total_return_pct=20.0,
        mdd=6.0,
        option_trade_count=16,
        option_worst_trade_risk_pct=-80.0,
        option_cvar10_risk_pct=-40.0,
        option_worst_mae_pct=90.0,
        option_annual_risk_return_pct=2.0,
        option_risk_data_coverage=0.80,
        monthly_win_rate=0.2,
    ))
    clean = evaluate(_stats(
        total_return_pct=0.4,
        mdd=1.0,
        option_trade_count=40,
        option_worst_trade_risk_pct=-1.0,
        option_cvar10_risk_pct=-1.0,
        option_worst_mae_pct=5.0,
        option_annual_risk_return_pct=40.0,
        option_risk_data_coverage=1.0,
        monthly_win_rate=0.95,
        average_committed_fraction=0.50,
    ))

    assert rich > clean


def test_option_carry_robust_uses_risk_capital_return_as_tie_break():
    better_carry = evaluate(_stats(option_annual_risk_return_pct=20.0))
    weaker_carry = evaluate(_stats(option_annual_risk_return_pct=0.0))

    assert better_carry > weaker_carry
    assert better_carry - weaker_carry <= 5.0


def test_option_carry_robust_bounds_tail_penalty():
    clean = evaluate(_stats())
    ugly = evaluate(_stats(
        option_worst_trade_risk_pct=-90.0,
        option_cvar10_risk_pct=-50.0,
        option_worst_mae_pct=100.0,
    ))

    assert clean > ugly
    assert clean - ugly <= 5.0


def test_option_carry_robust_ignores_monthly_win_rate_and_sharpe():
    base = evaluate(_stats())

    assert base == evaluate(_stats(monthly_win_rate=0.95, sharpe=3.0))
    assert base == evaluate(_stats(monthly_win_rate=0.05, sharpe=-2.0))


def test_option_carry_robust_penalizes_low_wheel_capital_utilization_through_wrapper():
    class StrategyView:
        def __init__(self, utilization):
            self.utilization = utilization

        def get_research_summary(self):
            return {"average_committed_fraction": self.utilization}

    class BacktestWrapper:
        def __init__(self, utilization):
            self.strategy = StrategyView(utilization)

    stats = _stats(total_return_pct=50.0)
    active = evaluate(stats, strat=BacktestWrapper(0.40))
    idle = evaluate(stats, strat=BacktestWrapper(0.02))

    assert active > idle


def test_option_carry_robust_does_not_penalize_missing_wheel_fields():
    class ZeroLifecycle:
        def get_research_summary(self):
            return {
                "completed_wheel_cycles": 0,
                "completed_stock_exits": 0,
                "observations": 12,
            }

    assert evaluate(_stats()) == evaluate(_stats(), strat=ZeroLifecycle())


def test_option_carry_robust_cycles_cannot_overturn_account_return():
    class Cycled:
        def get_research_summary(self):
            return {"completed_wheel_cycles": 6, "observations": 20}

    assert evaluate(_stats(), strat=Cycled()) > evaluate(_stats())
    assert evaluate(_stats(total_return_pct=40.0)) > evaluate(
        _stats(total_return_pct=2.0), strat=Cycled(),
    )


def test_option_carry_robust_rejects_non_finite_inputs():
    assert math.isfinite(evaluate(None))
    assert math.isfinite(evaluate(_stats(
        total_return_pct=float("nan"),
        mdd=float("inf"),
        option_annual_risk_return_pct=float("-inf"),
    )))
    assert evaluate(_stats(total_return_pct=-100.0)) < evaluate(_stats(total_return_pct=0.0))


def test_option_carry_robust_missing_drawdown_is_conservative():
    known_risk = evaluate(_stats(mdd=5.0))

    for invalid in (None, float("nan"), float("inf")):
        score = evaluate(_stats(mdd=invalid))
        assert math.isfinite(score)
        assert score < known_risk


def test_option_carry_robust_short_window_uses_minimum_annualization_period():
    short_window = evaluate(_stats(years=0.01, total_return_pct=1.0))
    minimum_window = evaluate(_stats(years=0.25, total_return_pct=1.0))

    assert short_window == minimum_window


def test_option_carry_robust_coverage_transition_is_smooth():
    just_below_floor = evaluate(_stats(option_risk_data_coverage=0.799))
    at_floor = evaluate(_stats(option_risk_data_coverage=0.80))
    no_coverage = evaluate(_stats(option_risk_data_coverage=0.0))

    assert at_floor > just_below_floor
    assert at_floor - just_below_floor < 0.10
    assert no_coverage < just_below_floor


def test_option_carry_robust_invalid_tail_data_does_not_improve_score():
    valid = evaluate(_stats())
    invalid = evaluate(_stats(
        option_worst_trade_risk_pct=float("nan"),
        option_cvar10_risk_pct=float("nan"),
        option_worst_mae_pct=float("nan"),
    ))

    assert math.isfinite(invalid)
    assert invalid <= valid


def test_option_carry_robust_all_secondary_adjustments_stay_bounded():
    class ExtremeStress:
        def get_research_summary(self):
            return {
                "nav": 100.0,
                "risk_stress_loss_down": 100.0,
                "risk_known": True,
                "observations": 100,
            }

    plain = evaluate(_stats(
        option_trade_count=16,
        total_trades=16,
        option_annual_risk_return_pct=0.0,
        option_worst_trade_risk_pct=0.0,
        option_cvar10_risk_pct=0.0,
        option_worst_mae_pct=0.0,
    ))
    extreme = evaluate(_stats(
        option_trade_count=16,
        total_trades=16,
        option_annual_risk_return_pct=-40.0,
        option_worst_trade_risk_pct=-100.0,
        option_cvar10_risk_pct=-100.0,
        option_worst_mae_pct=100.0,
    ), strat=ExtremeStress())

    assert extreme < plain
    assert plain - extreme <= 5.0
