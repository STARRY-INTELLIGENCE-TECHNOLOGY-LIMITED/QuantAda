import pytest

from metrics.mix_score_turbo import evaluate


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


def test_turbo_keeps_account_rank_when_drawdown_or_trades_are_weak():
    strong = _base_stats()
    strong["total_return_pct"] = 120.0
    strong["mdd"] = 30.0
    strong["safe_mdd"] = 30.0
    strong["total_trades"] = 8

    weak = _base_stats()
    weak["total_return_pct"] = 8.0
    weak["mdd"] = 5.0
    weak["safe_mdd"] = 5.0

    assert evaluate(strong) > evaluate(weak)
    assert evaluate(strong) != -20.0


def test_turbo_below_thresholds_matches_unpenalized_formula():
    stats = _base_stats()
    # 复合年化 * 2 + 胜率 0.60 平滑奖励 + 盈亏比 2.5 平滑奖励；回撤 10% 与 40 笔均无降权
    compound = ((1.6 ** 0.5) - 1.0) * 100.0
    assert evaluate(stats) == pytest.approx(compound * 2.0 + 5.0 * (0.20 / 0.30) + 5.0 * 0.75)


def test_turbo_uses_compound_annual_return():
    one_year = _base_stats()
    one_year["years"] = 1.0
    two_year = _base_stats()
    two_year["years"] = 2.0

    assert evaluate(one_year) > evaluate(two_year)
