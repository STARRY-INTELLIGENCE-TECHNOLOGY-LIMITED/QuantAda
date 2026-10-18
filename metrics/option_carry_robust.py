"""现金担保期权策略评分，包含交易活跃度、回撤和可用资金利用率惩罚。"""

from __future__ import annotations

import math


def _finite(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _clip(value, lower, upper):
    return min(max(float(value), lower), upper)


def _strategy_summary(strat):
    """沿 Backtrader 包装层查找策略研究摘要。"""
    current = strat
    visited = set()
    for _ in range(4):
        if current is None or id(current) in visited:
            break
        visited.add(id(current))
        getter = getattr(current, "get_research_summary", None)
        if callable(getter):
            try:
                result = getter()
            except Exception:
                result = None
            if isinstance(result, dict):
                return result
        getter = getattr(current, "get_optimizer_stats", None)
        if callable(getter):
            try:
                result = getter()
            except Exception:
                result = None
            if isinstance(result, dict):
                summary = result.get("wheel_summary")
                if isinstance(summary, dict):
                    return summary
        current = getattr(current, "strategy", None)
    return {}


def _trading_days(stats, strat):
    days = max(0.0, _finite(stats.get("trading_days"), 0.0))
    current = strat
    visited = set()
    while days <= 0 and current is not None and id(current) not in visited:
        visited.add(id(current))
        data = getattr(current, "data", None)
        if data is not None:
            try:
                days = float(len(data))
            except Exception:
                days = 0.0
            if days > 0:
                break
        current = getattr(current, "strategy", None)
    if days > 0:
        return days
    return max(126.0, _finite(stats.get("years"), 1.0) * 252.0)


def _monthly_win_rate(stats, strat):
    value = stats.get("monthly_win_rate")
    if value is None and strat is not None:
        current = strat
        visited = set()
        for _ in range(4):
            if current is None or id(current) in visited:
                break
            visited.add(id(current))
            try:
                analysis = current.analyzers.getbyname("timereturn_monthly").get_analysis()
                active = [
                    float(item) for item in analysis.values()
                    if math.isfinite(float(item)) and abs(float(item)) > 1e-12
                ]
                if active:
                    value = sum(item > 0 for item in active) / len(active)
                    break
            except Exception:
                pass
            current = getattr(current, "strategy", None)
    rate = _finite(value, 0.5)
    if rate > 1.0:
        rate /= 100.0
    return _clip(rate, 0.0, 1.0)


def evaluate(stats, strat=None, args=None):
    """奖励有足够交易与资金使用的 CSP/Wheel 风险调整回报。"""
    if not isinstance(stats, dict):
        return -100.0

    option_trades = max(0.0, _finite(stats.get("option_trade_count"), 0.0))
    coverage = _finite(stats.get("option_risk_data_coverage"), 0.0)
    years = max(0.5, _trading_days(stats, strat) / 252.0)
    total_trades = max(0.0, _finite(stats.get("total_trades"), 0.0))
    trades_per_year = total_trades / years
    if trades_per_year < 6.0:
        return -100.0 + trades_per_year
    if coverage < 0.80:
        return -100.0

    total_return = _finite(stats.get("total_return_pct"), 0.0)
    growth = 1.0 + total_return / 100.0
    if growth <= 0:
        annual_return = -100.0
    else:
        try:
            annual_return = (growth ** (1.0 / years) - 1.0) * 100.0
        except (OverflowError, ValueError):
            annual_return = 100.0
    annual_return = _clip(annual_return, -100.0, 100.0)
    mdd = abs(_finite(stats.get("mdd"), 100.0))
    worst_trade = abs(min(0.0, _finite(stats.get("option_worst_trade_risk_pct"), 0.0)))
    cvar10 = abs(min(0.0, _finite(stats.get("option_cvar10_risk_pct"), 0.0)))
    mae = max(0.0, _finite(stats.get("option_worst_mae_pct"), 0.0))
    activity = min(1.0, trades_per_year / 18.0)
    risk_adjusted = annual_return - 0.5 * mdd
    # 对正收益按活跃度折扣；负收益随低活跃度加罚，避免折扣把坏分抬高。
    score = risk_adjusted * activity if risk_adjusted >= 0 else risk_adjusted * (2.0 - activity)
    monthly_component = 10.0 * (_monthly_win_rate(stats, strat) - 0.5)
    score += (
        monthly_component * activity
        if monthly_component >= 0
        else monthly_component * (2.0 - activity)
    )

    summary = stats.get("wheel_summary")
    if not isinstance(summary, dict) or not summary:
        summary = _strategy_summary(strat)
    utilization = stats.get("average_committed_fraction")
    if utilization is None:
        utilization = summary.get(
            "average_committed_fraction", summary.get("average_utilization"),
        )
    if utilization is not None:
        utilization = _clip(_finite(utilization, 0.0), 0.0, 1.0)
        if utilization < 0.20:
            discount = utilization / 0.20
            score = score * discount if score >= 0 else score * (2.0 - discount)

    score -= 0.75 * max(0.0, worst_trade - 10.0)
    score -= 0.50 * max(0.0, cvar10 - 8.0)
    score -= 0.10 * max(0.0, mae - 25.0)
    return float(score) if math.isfinite(score) else -100.0


__all__ = ["evaluate"]
