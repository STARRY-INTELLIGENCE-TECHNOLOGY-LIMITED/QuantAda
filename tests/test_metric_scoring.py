"""评分插件的性格、连续性、鲁棒性与单调性测试。"""
import math

import pytest

from metrics import mix_score_origin, mix_score_turbo, mix_score_us_turbo, option_carry_robust, us_robust

TURBO = {"mix_score_turbo": mix_score_turbo.evaluate, "mix_score_us_turbo": mix_score_us_turbo.evaluate}
ALL = {
    **TURBO,
    "mix_score_origin": mix_score_origin.evaluate,
    "us_robust": us_robust.evaluate,
    "option_carry_robust": option_carry_robust.evaluate,
}


def _stats(annual_pct, mdd, trades, years=1.0, **extra):
    """按目标复合年化反推总收益，其余字段取中性合理值。"""
    total = ((1.0 + annual_pct / 100.0) ** years - 1.0) * 100.0
    base = {
        "total_return_pct": total, "years": years, "mdd": mdd, "safe_mdd": max(mdd, 1.0),
        "total_trades": trades, "win_rate": 0.55, "profit_factor": 1.8, "monthly_win_rate": 0.6,
        "sharpe": annual_pct / 20.0, "calmar": annual_pct / max(mdd, 1.0),
    }
    base.update(extra)
    return base


# A 高收益高回撤少样本，B 中等收益低回撤多样本；用 1 年窗口，使年化与总收益一致。
# 注意：mix_score_origin 使用未年化的总收益，窗口拉长到 2 年时它会反过来偏向 A，这是其既有特性。
PROFILE_A = _stats(60.0, 35.0, 25)
PROFILE_B = _stats(25.0, 10.0, 60)


@pytest.mark.parametrize("name", list(TURBO))
def test_turbo_prefers_aggressive_profile(name):
    assert TURBO[name](PROFILE_A) > TURBO[name](PROFILE_B)


@pytest.mark.parametrize("name", ["mix_score_origin", "us_robust"])
def test_defensive_metrics_prefer_robust_profile(name):
    assert ALL[name](PROFILE_B) > ALL[name](PROFILE_A)


def test_turbo_return_dominates_drawdown_and_sample_discounts():
    """收益翻倍时，即使回撤很高、样本很少，turbo 仍应更高。"""
    for evaluate in TURBO.values():
        assert evaluate(_stats(80.0, 45.0, 10)) > evaluate(_stats(40.0, 5.0, 80))


@pytest.mark.parametrize("name", list(TURBO))
def test_turbo_penalties_are_bounded(name):
    evaluate = TURBO[name]
    mild = evaluate(_stats(30.0, 75.0, 0, monthly_win_rate=0.0))
    extreme = evaluate(_stats(30.0, 1e6, 0, monthly_win_rate=0.0))
    assert mild == pytest.approx(extreme)


@pytest.mark.parametrize("name", list(ALL))
def test_no_jump_when_win_rate_or_profit_factor_cross_old_thresholds(name):
    evaluate = ALL[name]
    wr_low = evaluate(_stats(40.0, 8.0, 40, win_rate=0.549))
    wr_high = evaluate(_stats(40.0, 8.0, 40, win_rate=0.551))
    pf_low = evaluate(_stats(40.0, 8.0, 40, profit_factor=1.99))
    pf_high = evaluate(_stats(40.0, 8.0, 40, profit_factor=2.01))
    assert abs(wr_high - wr_low) < 0.2
    assert abs(pf_high - pf_low) < 0.2


@pytest.mark.parametrize("name", list(ALL))
def test_no_jump_across_trade_count_and_drawdown_limits(name):
    evaluate = ALL[name]
    pairs = (
        ("total_trades", (9.999, 10.001)), ("total_trades", (19.999, 20.001)),
        ("mdd", (19.999, 20.001)), ("mdd", (24.999, 25.001)), ("mdd", (14.999, 15.001)),
    )
    for key, points in pairs:
        scores = []
        for value in points:
            stats = _stats(40.0, 8.0, 40)
            stats[key] = value
            if key == "mdd":
                stats["safe_mdd"] = max(value, 1.0)
            scores.append(evaluate(stats))
        assert abs(scores[0] - scores[1]) < 0.5, (key, points)


@pytest.mark.parametrize("name", list(ALL))
def test_extreme_and_non_finite_inputs_stay_finite(name):
    evaluate = ALL[name]
    nan, inf = float("nan"), float("inf")
    for field in ("total_return_pct", "years", "mdd", "safe_mdd", "total_trades", "win_rate", "profit_factor",
                  "sharpe", "calmar", "monthly_win_rate"):
        for bad in (nan, inf, -inf, 0.0, 1e308, -1e308, None):
            stats = _stats(30.0, 10.0, 30)
            stats[field] = bad
            assert math.isfinite(evaluate(stats)), (name, field, bad)
    zero = _stats(0.0, 0.0, 0)
    assert math.isfinite(evaluate(zero))


@pytest.mark.parametrize("name", list(ALL))
def test_monotone_in_return_and_drawdown(name):
    evaluate = ALL[name]
    returns = [_stats(a, 12.0, 30) for a in (-50, -10, 0, 10, 30, 60, 120)]
    scores = [evaluate(s) for s in returns]
    assert all(a <= b + 1e-9 for a, b in zip(scores, scores[1:]))
    # 固定其余字段，只改回撤，分数不应上升。
    fixed = _stats(30.0, 12.0, 30)
    last = None
    for mdd in (0.0, 1.0, 5.0, 10.0, 15.0, 20.0, 25.0, 40.0, 80.0):
        score = evaluate({**fixed, "mdd": mdd, "safe_mdd": max(mdd, 1.0)})
        assert last is None or score <= last + 1e-9
        last = score


def test_us_robust_drawdown_gate_is_continuous_but_steep():
    below = us_robust.evaluate(_stats(40.0, 20.0, 40))
    above = us_robust.evaluate(_stats(40.0, 20.5, 40))
    assert 0.0 < below - above < 5.0
    assert us_robust.evaluate(_stats(40.0, 30.0, 40)) < below - 50.0
