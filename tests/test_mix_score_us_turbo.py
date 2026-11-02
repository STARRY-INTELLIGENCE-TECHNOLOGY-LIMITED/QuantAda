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

    compound = ((1.6 ** 0.5) - 1.0) * 100.0
    # 基础分：复合年化 * 2 + 60/10 + 10 bonus；月度胜率 20% 再扣 5 分。
    assert score == pytest.approx(compound * 2.0 + 6.0 + 10.0 - 5.0)


def test_evaluate_normalizes_percentage_monthly_win_rate():
    stats = _base_stats()
    stats["monthly_win_rate"] = 20.0

    score = evaluate(stats)

    compound = ((1.6 ** 0.5) - 1.0) * 100.0
    assert score == pytest.approx(compound * 2.0 + 6.0 + 10.0 - 5.0)


def test_evaluate_treats_non_finite_monthly_win_rate_as_zero():
    stats = _base_stats()
    stats["monthly_win_rate"] = float("nan")

    score = evaluate(stats)

    compound = ((1.6 ** 0.5) - 1.0) * 100.0
    assert score == pytest.approx(compound * 2.0 + 6.0 + 10.0 - 10.0)


def test_evaluate_keeps_account_rank_when_drawdown_or_trades_are_weak():
    strong = _base_stats()
    strong["total_return_pct"] = 120.0
    strong["mdd"] = 30.0
    strong["safe_mdd"] = 30.0
    strong["total_trades"] = 8
    strong["monthly_win_rate"] = 0.60

    weak = _base_stats()
    weak["total_return_pct"] = 8.0
    weak["mdd"] = 5.0
    weak["safe_mdd"] = 5.0
    weak["monthly_win_rate"] = 0.60

    strong_score = evaluate(strong)
    weak_score = evaluate(weak)

    assert strong_score > weak_score
    assert strong_score != -20.0
    assert weak_score != -20.0


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

    compound = ((1.6 ** 0.5) - 1.0) * 100.0
    assert score == pytest.approx(compound * 2.0 + 6.0 + 10.0)
