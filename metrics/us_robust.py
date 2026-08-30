"""美股及国际 ETF 的稳健型参数优化评分函数。

该指标保留收益驱动，但同时考虑风险调整收益、回撤、月度一致性和可选的
基准超额收益、换手率、成本与滚动窗口结果。所有输入均来自优化器的 stats
字典或可选的策略分析器，不执行网络和文件操作。
"""

import math
from statistics import median


_HARD_MDD_LIMIT = 20.0
_SOFT_MDD_START = 8.0
_MDD_ALERT_LEVEL = 12.0
_MIN_SAMPLE_TRADES = 8


def _finite(value, default=0.0):
    """将输入转换为有限浮点数，异常或非有限值使用默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _clip(value, lower, upper):
    """将数值限制在指定区间内。"""
    return min(max(float(value), lower), upper)


def _optional_number(stats, *keys):
    """从 stats 中按顺序读取第一个有效的可选数值。"""
    for key in keys:
        if key not in stats or stats.get(key) is None:
            continue
        value = _finite(stats.get(key), float("nan"))
        if math.isfinite(value):
            return value
    return None


def _normalize_monthly_win_rate(value):
    """兼容 0～1 小数和 0～100 百分比两种月度胜率表示。"""
    if value is None:
        return None
    normalized = _finite(value, 0.5)
    if normalized > 1.0:
        normalized /= 100.0
    return _clip(normalized, 0.0, 1.0)


def _monthly_win_rate_from_strategy(strat):
    """从策略的月度收益分析器推导正收益月份比例。"""
    try:
        analyzer = strat.analyzers.getbyname("timereturn_monthly")
        monthly_returns = analyzer.get_analysis().values()
    except Exception:
        return None

    active_returns = []
    for value in monthly_returns:
        number = _finite(value, float("nan"))
        if math.isfinite(number) and abs(number) > 1e-12:
            active_returns.append(number)
    if not active_returns:
        return None
    return sum(value > 0 for value in active_returns) / len(active_returns)


def _fold_annual_returns(stats):
    """读取可选的滚动窗口年化收益列表。"""
    raw_values = stats.get("fold_annual_returns_pct")
    if raw_values is None:
        raw_values = stats.get("fold_annual_returns")
    if not isinstance(raw_values, (list, tuple)):
        return []

    values = []
    for value in raw_values:
        number = _finite(value, float("nan"))
        if math.isfinite(number):
            values.append(number)
    return values


def evaluate(stats, strat=None, args=None) -> float:
    """计算面向美股及国际 ETF 的稳健型优化分数。

    基础输入使用当前优化器已有字段：
    ``total_return_pct``、``years``、``mdd``、``sharpe``、``calmar``、
    ``total_trades``、``profit_factor`` 和 ``monthly_win_rate``。

    可选字段用于后续增强：``sortino``、``benchmark_annual_return_pct``、
    ``annual_cost_pct``、``turnover_ratio``、``fold_annual_returns_pct``、
    ``worst_fold_annual_return_pct`` 和 ``max_drawdown_duration_days``。
    ``turnover_ratio`` 约定为年换手倍数，滚动收益约定为百分比年化值。
    """
    if not isinstance(stats, dict):
        stats = {}

    total_return_pct = _finite(stats.get("total_return_pct"), 0.0)
    years = max(_finite(stats.get("years"), 1.0), 0.25)
    annual_return_pct = total_return_pct / years

    mdd = abs(_finite(stats.get("mdd"), 20.0))
    sharpe = _finite(stats.get("sharpe"), 0.0)
    sortino = _optional_number(stats, "sortino", "sortino_ratio")
    risk_ratio = sharpe if sortino is None else sortino

    calmar_default = annual_return_pct / max(mdd, 1.0) if mdd > 0 else 0.0
    calmar = _finite(stats.get("calmar"), calmar_default)
    total_trades = max(0.0, _finite(stats.get("total_trades"), 0.0))
    profit_factor = max(0.0, _finite(stats.get("profit_factor"), 1.0))

    annual_cost_pct = max(0.0, _optional_number(stats, "annual_cost_pct", "cost_pct") or 0.0)
    net_annual_return_pct = annual_return_pct - annual_cost_pct

    benchmark_annual_return_pct = _optional_number(stats, "benchmark_annual_return_pct")
    excess_return_pct = (
        net_annual_return_pct - benchmark_annual_return_pct
        if benchmark_annual_return_pct is not None
        else 0.0
    )

    fold_returns = _fold_annual_returns(stats)
    worst_fold = _optional_number(stats, "worst_fold_annual_return_pct")
    if fold_returns:
        median_fold = median(fold_returns)
        worst_fold = min(fold_returns) if worst_fold is None else min(worst_fold, min(fold_returns))
        robust_annual_return_pct = (
            0.50 * net_annual_return_pct + 0.30 * median_fold + 0.20 * worst_fold
        )
    else:
        robust_annual_return_pct = net_annual_return_pct

    risk_component = 10.0 * _clip(risk_ratio, -4.0, 6.0)
    calmar_component = _clip(calmar, -10.0, 20.0)
    score = (
        0.55 * _clip(robust_annual_return_pct, -100.0, 100.0)
        + 0.20 * risk_component
        + 0.15 * calmar_component
        + 0.10 * _clip(excess_return_pct, -100.0, 100.0)
    )

    monthly_win_rate = _normalize_monthly_win_rate(stats.get("monthly_win_rate"))
    if monthly_win_rate is None and strat is not None:
        monthly_win_rate = _monthly_win_rate_from_strategy(strat)
    if monthly_win_rate is None:
        monthly_win_rate = 0.5
    score += 4.0 * (monthly_win_rate - 0.5)

    if profit_factor > 0.0:
        score += 1.5 * _clip(math.log(max(profit_factor, 0.25)), -2.0, 2.0)

    # 8% 以上开始温和惩罚，12% 以上加重惩罚，避免收益单独主导。
    score -= 0.50 * max(0.0, mdd - _SOFT_MDD_START)
    score -= 1.25 * max(0.0, mdd - _MDD_ALERT_LEVEL)

    turnover_ratio = _optional_number(stats, "turnover_ratio", "annual_turnover")
    if turnover_ratio is not None:
        score -= 0.20 * max(0.0, turnover_ratio - 4.0)

    drawdown_duration_days = _optional_number(stats, "max_drawdown_duration_days")
    if drawdown_duration_days is not None:
        score -= 0.02 * max(0.0, drawdown_duration_days - 60.0)

    if worst_fold is not None and worst_fold < 0.0:
        score -= 0.10 * min(abs(worst_fold), 100.0)

    if total_trades < _MIN_SAMPLE_TRADES:
        score -= 1.5 * (_MIN_SAMPLE_TRADES - total_trades)

    # 一年以下的样本年化收益容易被短期行情放大，使用轻度期限可靠性扣分。
    if years < 1.0:
        score -= 8.0 * (1.0 - years)

    if mdd > _HARD_MDD_LIMIT:
        score = -100.0 - 2.0 * (mdd - _HARD_MDD_LIMIT)

    return float(score) if math.isfinite(score) else -100.0
