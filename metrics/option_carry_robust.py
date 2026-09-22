"""CSP/PCS 期权专用稳健评分。

该评分同时看账户收益和风险资本收益，避免把大量闲置现金的平滑曲线
误判为高质量期权策略。风险资本由回测 Broker 的 CSP 担保金额或 PCS
最大风险资本提供。
"""

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


def evaluate(stats, strat=None, args=None):
    """计算面向现金担保 CSP 与 PCS 的风险资本收益评分。"""
    if not isinstance(stats, dict):
        return -100.0

    option_trades = max(0.0, _finite(stats.get("option_trade_count"), 0.0))
    coverage = _finite(stats.get("option_risk_data_coverage"), 0.0)
    if option_trades < 10 or coverage < 0.80:
        return -100.0 - max(0.0, 10.0 - option_trades)

    years = max(_finite(stats.get("years"), 1.0), 0.25)
    account_annual = _finite(stats.get("total_return_pct"), 0.0) / years
    risk_annual = _finite(stats.get("option_annual_risk_return_pct"), -100.0)
    premium_yield = _finite(stats.get("option_premium_yield_pct"), 0.0)
    sharpe = _finite(stats.get("sharpe"), 0.0)
    mdd = abs(_finite(stats.get("mdd"), 20.0))
    worst_trade = abs(min(0.0, _finite(stats.get("option_worst_trade_risk_pct"), 0.0)))
    cvar10 = abs(min(0.0, _finite(stats.get("option_cvar10_risk_pct"), 0.0)))
    mae = max(0.0, _finite(stats.get("option_worst_mae_pct"), 0.0))

    score = (
        0.35 * _clip(account_annual, -100.0, 100.0)
        + 0.35 * _clip(risk_annual, -100.0, 100.0)
        + 0.15 * _clip(premium_yield, -100.0, 100.0)
        + 0.15 * _clip(sharpe * 10.0, -40.0, 60.0)
    )
    score -= 0.50 * max(0.0, mdd - 8.0)
    score -= 0.75 * max(0.0, worst_trade - 10.0)
    score -= 0.50 * max(0.0, cvar10 - 8.0)
    score -= 0.10 * max(0.0, mae - 25.0)
    return float(score) if math.isfinite(score) else -100.0


__all__ = ["evaluate"]
