import math

from metrics.mix_score_turbo import evaluate as _turbo_evaluate


# 评分公式或量纲变化时递增；Study 记录该版本，版本不同的旧 Study 不续传，避免新旧分数混在一起。
METRIC_VERSION = 2


def evaluate(stats, strat=None, args=None):
    """
    混合评分 美股/国际市场冲锋版 (US Turbo)
    基于 A 股 Turbo 评分公式（同一份实现），增加月度胜率一致性软惩罚。
    目标：收益主导，同时压制单年度暴利但月度持续性不足的过拟合参数。
    月度正收益占比低于 40% 时线性扣分，最多 10 分；其余项与 A 股 Turbo 完全一致，均为连续有界项。
    """

    monthly_win_rate = stats.get('monthly_win_rate')
    if monthly_win_rate is None and strat is not None:
        try:
            monthly_returns = strat.analyzers.getbyname('timereturn_monthly').get_analysis()
            active_monthly_returns = []
            for monthly_return in monthly_returns.values():
                try:
                    monthly_return = float(monthly_return)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(monthly_return) and abs(monthly_return) > 1e-12:
                    active_monthly_returns.append(monthly_return)
            monthly_win_rate = (
                sum(1 for monthly_return in active_monthly_returns if monthly_return > 0) / len(active_monthly_returns)
                if active_monthly_returns else 0.0
            )
        except Exception:
            monthly_win_rate = 0.0
    try:
        monthly_win_rate = 0.0 if monthly_win_rate is None else float(monthly_win_rate)
    except (TypeError, ValueError):
        monthly_win_rate = 0.0
    if not math.isfinite(monthly_win_rate):
        monthly_win_rate = 0.0
    if monthly_win_rate > 1.0:
        monthly_win_rate = monthly_win_rate / 100.0
    monthly_win_rate = max(0.0, min(monthly_win_rate, 1.0))

    # 月度一致性惩罚：低于 40% 正收益月份占比时线性扣分，0% 时扣满 10 分。
    consistency_penalty = 10.0 * max(0.0, 0.40 - monthly_win_rate) / 0.40

    return _turbo_evaluate(stats, strat, args) - consistency_penalty
