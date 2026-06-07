from types import SimpleNamespace

import pytest

from metrics.mix_score_us_turbo import evaluate


def _base_stats():
    return {
        "mdd": 10.0,
        "total_return_pct": 60.0,
        "years": 2.0,
        "total_trades": 40,
        "safe_mdd": 10.0,
        "win_rate": 0.60,
        "profit_factor": 2.5,
    }


def test_evaluate_applies_consistency_penalty_from_stats():
    stats = _base_stats()
    stats["monthly_win_rate"] = 0.20

    score = evaluate(stats)

    # Base: 30*2 + 60/10 + 10 bonus = 76
    # Penalty: -10 * ((0.40 - 0.20) / 0.40) = -5
    assert score == pytest.approx(71.0)


def test_evaluate_normalizes_percentage_monthly_win_rate():
    stats = _base_stats()
    stats["monthly_win_rate"] = 20.0

    score = evaluate(stats)

    assert score == pytest.approx(71.0)


def test_evaluate_treats_non_finite_monthly_win_rate_as_zero():
    stats = _base_stats()
    stats["monthly_win_rate"] = float("nan")

    score = evaluate(stats)

    assert score == pytest.approx(66.0)


def test_evaluate_falls_back_to_monthly_analyzer_when_stat_missing():
    stats = _base_stats()
    strat = SimpleNamespace(
        analyzers=SimpleNamespace(
            getbyname=lambda _name: SimpleNamespace(
                get_analysis=lambda: {
                    "2024-01": 0.10,
                    "2024-02": -0.05,
                    "2024-03": 0.0,
                    "2024-04": 0.08,
                }
            )
        )
    )

    score = evaluate(stats, strat=strat)

    assert score == pytest.approx(76.0)
