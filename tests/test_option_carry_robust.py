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


def test_option_carry_robust_requires_risk_capital_data():
    assert evaluate(_stats(option_risk_data_coverage=0.5)) == -100.0


def test_option_carry_robust_prefers_annualized_return_after_drawdown():
    efficient = evaluate(_stats(total_return_pct=25.0))
    weak = evaluate(_stats(total_return_pct=8.0))
    assert efficient > weak


def test_option_carry_robust_hard_penalizes_fewer_than_six_trades_per_year():
    score = evaluate(_stats(option_trade_count=11, total_trades=40))

    assert score == -94.5


def test_option_carry_robust_scales_positive_score_with_activity():
    low_activity = evaluate(_stats(
        option_trade_count=18, total_trades=36, total_return_pct=50.0,
    ))
    normal_activity = evaluate(_stats(
        option_trade_count=36, total_trades=36, total_return_pct=50.0,
    ))

    assert normal_activity > low_activity


def test_option_carry_robust_counts_short_option_trades_not_spread_legs():
    score = evaluate(_stats(option_trade_count=11, total_trades=100))

    assert score == -94.5


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
