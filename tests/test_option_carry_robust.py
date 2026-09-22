from metrics.option_carry_robust import evaluate


def _stats(**overrides):
    values = {
        "option_trade_count": 24,
        "option_risk_data_coverage": 1.0,
        "years": 2.0,
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


def test_option_carry_robust_prefers_risk_efficiency():
    efficient = evaluate(_stats(option_annual_risk_return_pct=12.0))
    weak = evaluate(_stats(option_annual_risk_return_pct=2.0))
    assert efficient > weak
