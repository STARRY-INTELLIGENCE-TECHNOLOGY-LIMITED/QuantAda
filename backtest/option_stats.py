"""期权交易归因统计，供优化器的期权专用评分使用。"""

from __future__ import annotations

import math


def _number(value, default=None):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if math.isfinite(value) else default


def _is_option_trade(trade):
    """空头期权，或带乘数/权利金的保护腿。"""
    if str(trade.get("side", "long")).lower() == "short":
        return True
    if _number(trade.get("contract_multiplier")):
        return True
    return _number(trade.get("premium_credit")) is not None


def summarize_option_trades(closed_trades, years=1.0):
    """按 Short Put/PCS 汇总风险资本、净权利金和尾部损失。

    风险收益使用平均担保金额，避免把顺序换手的担保金简单加总后压低收益率。
    保护腿的 PnL 与权利金借记会计入净结果。权利金收益率按年化，避免长窗口虚增评分。
    """
    years = max(_number(years, 1.0) or 1.0, 0.25)
    all_trades = [trade for trade in (closed_trades or []) if _is_option_trade(trade)]
    shorts = [
        trade for trade in all_trades
        if str(trade.get("side", "long")).lower() == "short"
    ]
    result = {
        "option_trade_count": len(shorts),
        "option_total_pnl": 0.0,
        "option_gross_credit": 0.0,
        "option_risk_capital_sum": 0.0,
        "option_annual_risk_return_pct": 0.0,
        "option_premium_yield_pct": 0.0,
        "option_worst_trade_pnl": 0.0,
        "option_worst_trade_risk_pct": 0.0,
        "option_cvar10_risk_pct": 0.0,
        "option_avg_mae_pct": 0.0,
        "option_worst_mae_pct": 0.0,
        "option_risk_data_coverage": 0.0,
    }
    if not shorts:
        return result

    pnl_values = []
    risk_returns = []
    maes = []
    risk_data_count = 0
    for trade in all_trades:
        pnl = _number(trade.get("pnl"), 0.0) or 0.0
        credit = _number(trade.get("premium_credit"), 0.0) or 0.0
        result["option_total_pnl"] += pnl
        result["option_gross_credit"] += credit
        pnl_values.append(pnl)

    for trade in shorts:
        pnl = _number(trade.get("pnl"), 0.0) or 0.0
        risk_capital = _number(trade.get("risk_capital"))
        entry = _number(trade.get("entry_price"))
        adverse = _number(trade.get("highest_price_during_trade"))
        if risk_capital is not None and risk_capital > 0:
            risk_data_count += 1
            result["option_risk_capital_sum"] += risk_capital
            risk_returns.append(pnl / risk_capital)
        if entry is not None and entry > 0 and adverse is not None:
            maes.append(adverse / entry - 1.0)

    result["option_risk_data_coverage"] = risk_data_count / len(shorts)
    avg_risk = (
        result["option_risk_capital_sum"] / risk_data_count
        if risk_data_count else 0.0
    )
    if avg_risk > 0:
        result["option_annual_risk_return_pct"] = (
            result["option_total_pnl"] / avg_risk * 100.0 / years
        )
        result["option_premium_yield_pct"] = (
            result["option_gross_credit"] / avg_risk * 100.0 / years
        )
        if risk_returns:
            result["option_worst_trade_risk_pct"] = min(risk_returns) * 100.0
            tail_count = max(1, int(math.ceil(len(risk_returns) * 0.10)))
            result["option_cvar10_risk_pct"] = (
                sum(sorted(risk_returns)[:tail_count]) / tail_count * 100.0
            )

    result["option_worst_trade_pnl"] = min(pnl_values, default=0.0)
    if maes:
        result["option_avg_mae_pct"] = sum(maes) / len(maes) * 100.0
        result["option_worst_mae_pct"] = max(maes) * 100.0
    return result


__all__ = ["summarize_option_trades"]
