"""通用期权评分：账户结果为主，担保资金与生命周期信息为有界次级项。

主项是账户年化收益减去账户回撤。担保资金收益率、单笔尾部、样本量和覆盖率，
以及策略实际提供的资金占用、Wheel 周期和压力损失，只作为次级调整。
次级项合计限制在固定带宽内，避免干净尾部或更少交易推翻明显更好的账户结果。
交易过少或风险资本覆盖不足时降权，不使用笔数悬崖。未提供 Wheel 字段的策略不受罚。
"""

from __future__ import annotations

import math


_SECONDARY_BAND = 2.5
_MDD_WEIGHT = 0.20
_MDD_STRESS_START = 15.0
_MDD_STRESS_WEIGHT = 0.50
_MIN_YEARS = 0.25
_SAMPLE_TRADES_PER_YEAR = 8.0
_SAMPLE_WEIGHT = 0.15
_COVERAGE_FLOOR = 0.80
_CARRY_WEIGHT = 0.05
_CARRY_CAP = 40.0
_TAIL_CAP_START = (25.0, 15.0, 40.0)
_STRESS_NAV_START = 0.25
_STRESS_WEIGHT = 25.0
_STRESS_CAP = 30.0


def _finite(value, default=0.0):
    """把输入转成有限浮点数，异常或非有限值使用默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _clip(value, lower, upper):
    """把数值限制在闭区间内。"""
    return min(max(float(value), lower), upper)


def _lookup_summary(stats, strat):
    """读取策略自愿提供的研究摘要；没有摘要时返回空字典。"""
    summary = stats.get("wheel_summary") if isinstance(stats, dict) else None
    if isinstance(summary, dict) and summary:
        return summary
    current = strat
    visited = set()
    for _ in range(4):
        if current is None or id(current) in visited:
            break
        visited.add(id(current))
        for name in ("get_research_summary", "get_optimizer_stats"):
            getter = getattr(current, name, None)
            if not callable(getter):
                continue
            try:
                result = getter()
            except Exception:
                result = None
            if not isinstance(result, dict) or not result:
                continue
            nested = result.get("wheel_summary")
            if isinstance(nested, dict) and nested:
                return nested
            return result
        current = getattr(current, "strategy", None)
    return {}


def _years(stats, strat):
    """优先使用优化器给出的窗口长度，避免多标的 data0 长度歪曲年化。"""
    years = _finite(stats.get("years"), 0.0)
    if years > 0.0:
        return max(years, _MIN_YEARS)
    current = strat
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        data = getattr(current, "data", None)
        if data is not None:
            try:
                bars = float(len(data))
            except Exception:
                bars = 0.0
            if bars > 0.0:
                return max(bars / 252.0, _MIN_YEARS)
        current = getattr(current, "strategy", None)
    return 1.0


def _annual_return(total_return_pct, years):
    """由区间总收益率计算复合年化，单位为百分点。"""
    growth = 1.0 + _finite(total_return_pct, 0.0) / 100.0
    if growth <= 0.0:
        return -100.0
    try:
        annual = (growth ** (1.0 / years) - 1.0) * 100.0
    except (OverflowError, ValueError):
        annual = 100.0 if growth > 1.0 else -100.0
    return _clip(annual, -100.0, 100.0)


def _first_fraction(sources, keys):
    """按顺序读取第一个存在的比例字段。"""
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source and source.get(key) is not None:
                return _clip(_finite(source.get(key), 0.0), 0.0, 1.0)
    return None


def _sample_penalty(stats, years):
    """交易不足时线性降权，保留账户收益对低样本组合的排序。"""
    option_trades = max(0.0, _finite(stats.get("option_trade_count"), 0.0))
    total_trades = max(0.0, _finite(stats.get("total_trades"), 0.0))
    activity_trades = option_trades if option_trades > 0.0 else total_trades
    shortage = max(0.0, _SAMPLE_TRADES_PER_YEAR - activity_trades / years)
    return _SAMPLE_WEIGHT * shortage


def _coverage_and_carry(stats):
    """按风险数据覆盖率平滑纳入担保资金收益率，并处罚覆盖不足。"""
    option_trades = max(0.0, _finite(stats.get("option_trade_count"), 0.0))
    if option_trades <= 0.0 or "option_risk_data_coverage" not in stats:
        return 0.0, 0.0
    coverage = _clip(_finite(stats.get("option_risk_data_coverage"), 0.0), 0.0, 1.0)
    penalty = 1.5 * max(0.0, _COVERAGE_FLOOR - coverage) / _COVERAGE_FLOOR
    if "option_annual_risk_return_pct" not in stats:
        return penalty, 0.0
    carry = _clip(_finite(stats.get("option_annual_risk_return_pct"), 0.0), -_CARRY_CAP, _CARRY_CAP)
    coverage_weight = min(1.0, coverage / _COVERAGE_FLOOR)
    return penalty, _CARRY_WEIGHT * carry * coverage_weight


def _tail_penalty(stats):
    """单笔担保尾部和 MAE 只做有界风险调整，不按担保亏损直接扣几十分。"""
    if max(0.0, _finite(stats.get("option_trade_count"), 0.0)) <= 0.0:
        return 0.0
    worst = abs(min(0.0, _finite(stats.get("option_worst_trade_risk_pct"), 0.0)))
    cvar = abs(min(0.0, _finite(stats.get("option_cvar10_risk_pct"), 0.0)))
    mae = max(0.0, _finite(stats.get("option_worst_mae_pct"), 0.0))
    worst_start, cvar_start, mae_start = _TAIL_CAP_START
    return (
        0.04 * max(0.0, worst - worst_start)
        + 0.03 * max(0.0, cvar - cvar_start)
        + 0.01 * max(0.0, mae - mae_start)
    )


def _lifecycle_points(stats, summary):
    """资金占用和完整周期只在策略提供字段时生效，且不能单独主导排序。"""
    points = 0.0
    utilization = _first_fraction(
        (stats, summary),
        ("average_committed_fraction", "average_utilization"),
    )
    if utilization is not None:
        points += 1.5 * min(utilization, 0.50)
        points -= 2.0 * max(0.0, utilization - 0.80)
    peak = _first_fraction(
        (summary,),
        ("max_utilization", "peak_committed_fraction"),
    )
    if peak is not None:
        points -= 1.0 * max(0.0, peak - 0.90)
    if "completed_wheel_cycles" in summary or "completed_stock_exits" in summary:
        cycles = max(0.0, _finite(summary.get("completed_wheel_cycles"), 0.0))
        cycles += max(0.0, _finite(summary.get("completed_stock_exits"), 0.0))
        points += 0.5 * min(1.0, cycles / _years(stats, None))
    if summary.get("observations") and summary.get("risk_known") is False:
        points -= 1.0
    return points


def _stress_penalty(summary):
    """仅在策略给出净值压力损失时惩罚，避免把缺失字段当成零风险或无限风险。"""
    if "risk_stress_loss_down" not in summary:
        return 0.0
    nav = _finite(summary.get("nav"), 0.0)
    if nav <= 0.0:
        return 0.0
    stress_fraction = max(0.0, _finite(summary.get("risk_stress_loss_down"), 0.0) / nav)
    penalty = _STRESS_WEIGHT * max(0.0, stress_fraction - _STRESS_NAV_START)
    return min(_STRESS_CAP, penalty)


def evaluate(stats, strat=None, args=None):
    """计算通用期权分数。账户风险调整收益决定大排序，其余信息只在带宽内调序。"""
    del args
    if not isinstance(stats, dict):
        return -100.0
    years = _years(stats, strat)
    annual_return = _annual_return(stats.get("total_return_pct"), years)
    drawdown = abs(_finite(stats.get("mdd"), 100.0))
    account = annual_return - _MDD_WEIGHT * drawdown
    account -= _MDD_STRESS_WEIGHT * max(0.0, drawdown - _MDD_STRESS_START)
    summary = _lookup_summary(stats, strat)
    coverage_penalty, carry_points = _coverage_and_carry(stats)
    secondary = carry_points + _lifecycle_points(stats, summary)
    secondary -= (
        _tail_penalty(stats)
        + coverage_penalty
        + _stress_penalty(summary)
        + _sample_penalty(stats, years)
    )
    secondary = _clip(secondary, -_SECONDARY_BAND, _SECONDARY_BAND)
    score = account + secondary
    return float(score) if math.isfinite(score) else -100.0


__all__ = ["evaluate"]
