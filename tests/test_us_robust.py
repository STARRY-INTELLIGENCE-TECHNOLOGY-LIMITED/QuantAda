import math
from types import SimpleNamespace

from metrics.us_robust import evaluate


def _base_stats():
    return {
        "mdd": 8.0,
        "total_return_pct": 60.0,
        "years": 2.0,
        "total_trades": 40,
        "sharpe": 1.8,
        "calmar": 3.75,
        "profit_factor": 1.8,
        "monthly_win_rate": 0.60,
    }


def test_evaluate_returns_finite_score_for_normal_stats():
    score = evaluate(_base_stats())

    assert math.isfinite(score)


def test_evaluate_rewards_better_risk_adjusted_result():
    baseline = evaluate(_base_stats())
    better = _base_stats()
    better.update({"mdd": 5.0, "sharpe": 2.4, "calmar": 8.0})

    assert evaluate(better) > baseline


def test_evaluate_penalizes_high_drawdown_even_with_higher_return():
    baseline = evaluate(_base_stats())
    high_drawdown = _base_stats()
    high_drawdown.update({"mdd": 19.0, "total_return_pct": 70.0})

    assert evaluate(high_drawdown) < baseline


def test_evaluate_applies_optional_cost_turnover_benchmark_and_fold_penalties():
    baseline = evaluate(_base_stats())
    stressed = _base_stats()
    stressed.update(
        {
            "annual_cost_pct": 3.0,
            "annual_turnover": 30.0,
            "benchmark_annual_return_pct": 40.0,
            "fold_annual_returns_pct": [35.0, 20.0, -10.0],
        }
    )

    assert evaluate(stressed) < baseline


def test_evaluate_uses_strategy_monthly_returns_when_stat_is_missing():
    stats = _base_stats()
    stats.pop("monthly_win_rate")
    strat = SimpleNamespace(
        analyzers=SimpleNamespace(
            getbyname=lambda _name: SimpleNamespace(
                get_analysis=lambda: {
                    "2026-01": 0.04,
                    "2026-02": -0.02,
                    "2026-03": 0.01,
                }
            )
        )
    )

    assert evaluate(stats, strat=strat) > evaluate({**stats, "monthly_win_rate": 0.0})


def test_evaluate_softly_penalizes_small_samples_without_rejecting_them():
    stats = _base_stats()
    stats["total_trades"] = 3

    score = evaluate(stats)

    assert math.isfinite(score)
    assert score < evaluate(_base_stats())


def test_evaluate_penalizes_short_horizon_when_annual_return_is_equal():
    long_stats = _base_stats()
    short_stats = _base_stats()
    long_stats.update({"total_return_pct": 60.0, "years": 2.0})
    # 两年总收益 60% 的复合年化，折成半年总收益后两边年化相同。
    short_total = ((1.60 ** 0.5) ** 0.5 - 1.0) * 100.0
    short_stats.update({"total_return_pct": short_total, "years": 0.5})

    assert evaluate(short_stats) < evaluate(long_stats)


def test_evaluate_applies_hard_drawdown_guard():
    stats = _base_stats()
    stats["mdd"] = 25.0

    assert evaluate(stats) < -100.0


def test_evaluate_handles_missing_and_nonfinite_fields():
    score = evaluate(
        {
            "total_return_pct": float("nan"),
            "years": 0.0,
            "mdd": float("nan"),
            "sharpe": float("inf"),
        }
    )

    assert math.isfinite(score)
    assert score < 0.0


def test_large_total_return_uses_compound_annual_not_simple_average():
    simple_average = _base_stats()
    simple_average.update({"total_return_pct": 200.0, "years": 2.0})
    compound_match = _base_stats()
    compound_match.update({"total_return_pct": 300.0, "years": 2.0})

    assert evaluate(compound_match) > evaluate(simple_average)


def test_profit_factor_bonus_stays_capped():
    modest = _base_stats()
    modest["profit_factor"] = 27.0
    extreme = _base_stats()
    extreme["profit_factor"] = 1000.0

    assert evaluate(extreme) == evaluate(modest)


def test_trade_sample_penalty_stops_at_eight_and_does_not_scale_with_years():
    """多年低频但已满 8 笔时不得再按年加码；未满 8 笔只按固定缺口扣分。"""
    held = _base_stats()
    held.update({
        "total_return_pct": 663.70,
        "years": 5.0,
        "total_trades": 10,
        "mdd": 15.25,
        "sharpe": 1.69,
        "calmar": 3.29,
        "profit_factor": 27.0,
    })
    active = dict(held)
    active["total_trades"] = 40
    empty = dict(held)
    empty["total_trades"] = 0

    assert evaluate(held) == evaluate(active)
    assert evaluate(active) - evaluate(empty) == 12.0
