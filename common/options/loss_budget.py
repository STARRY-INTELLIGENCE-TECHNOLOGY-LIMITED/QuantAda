"""期权持仓亏损预算的纯计算。

调用方提供已确认的腿快照。本模块不读取账户、不保存状态，也不提交订单。
百分比预算只作用于已定义最大亏损；裸短 Call 没有定义上限。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .analytics import underlying_key


@dataclass(frozen=True)
class OptionLegSnapshot:
    """一笔已确认期权腿的权利金与合约事实。"""

    symbol: str
    option_type: str
    strike: float
    expiry: str
    underlying: str
    signed_quantity: float
    entry_premium: float
    mark_premium: float
    multiplier: float


@dataclass(frozen=True)
class OptionLossDecision:
    """一次亏损预算判断。breached 为真时 symbols 都应打回零。"""

    breached: bool
    reason: str
    loss: float | None
    defined_max_loss: float | None
    budget: float | None
    symbols: tuple[str, ...]


def _finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _positive_limit(value):
    parsed = _finite(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _priced(leg):
    strike = _finite(leg.strike)
    entry = _finite(leg.entry_premium)
    mark = _finite(leg.mark_premium)
    multiplier = _finite(leg.multiplier)
    quantity = _finite(leg.signed_quantity)
    if None in {strike, entry, mark, multiplier, quantity}:
        return False
    if strike <= 0 or multiplier <= 0 or entry < 0 or mark < 0 or quantity == 0:
        return False
    return str(leg.option_type or "").upper() in {"PUT", "CALL"}


def _cash_pnl(leg):
    if not _priced(leg):
        return None
    return (leg.mark_premium - leg.entry_premium) * leg.signed_quantity * leg.multiplier


def _same_contract_family(left, right):
    if str(left.option_type).upper() != str(right.option_type).upper():
        return False
    if underlying_key(left.underlying) != underlying_key(right.underlying):
        return False
    if not left.expiry or left.expiry != right.expiry:
        return False
    left_multiplier = _finite(left.multiplier)
    right_multiplier = _finite(right.multiplier)
    if left_multiplier is None or right_multiplier is None:
        return False
    return abs(left_multiplier - right_multiplier) <= max(1e-9, abs(left_multiplier) * 1e-9)


def _is_equal_vertical(left, right):
    if left.symbol == right.symbol or not _same_contract_family(left, right):
        return False
    left_qty = _finite(left.signed_quantity)
    right_qty = _finite(right.signed_quantity)
    left_strike = _finite(left.strike)
    right_strike = _finite(right.strike)
    if None in {left_qty, right_qty, left_strike, right_strike}:
        return False
    if left_qty * right_qty >= 0:
        return False
    if abs(abs(left_qty) - abs(right_qty)) > 1e-9:
        return False
    return abs(left_strike - right_strike) > 1e-9


def _vertical_max_loss(left, right):
    short, long = (left, right) if left.signed_quantity < 0 else (right, left)
    quantity = abs(short.signed_quantity)
    width = abs(short.strike - long.strike)
    net_debit = (long.entry_premium - short.entry_premium) * quantity * short.multiplier
    if net_debit >= 0:
        return net_debit
    return max(0.0, width * quantity * short.multiplier + net_debit)


def _single_defined_loss(leg):
    quantity = abs(leg.signed_quantity)
    notional = quantity * leg.multiplier
    if leg.signed_quantity > 0:
        return leg.entry_premium * notional
    if str(leg.option_type).upper() == "PUT":
        return max(0.0, leg.strike - leg.entry_premium) * notional
    return None


def _incomplete_hedges(leg, legs):
    related = []
    for other in legs:
        if other.symbol == leg.symbol or not _same_contract_family(leg, other):
            continue
        other_qty = _finite(other.signed_quantity)
        if other_qty is None or leg.signed_quantity * other_qty >= 0:
            continue
        if not _is_equal_vertical(leg, other):
            related.append(other)
    return related


def _decision(breached, reason, loss, defined, budget, symbols):
    return OptionLossDecision(
        breached=bool(breached),
        reason=reason,
        loss=loss,
        defined_max_loss=defined,
        budget=budget,
        symbols=tuple(symbols),
    )


def _assess_group(group, max_loss_pct, max_loss_cash, defined, incomplete=False):
    symbols = tuple(leg.symbol for leg in group)
    pnl = 0.0
    for leg in group:
        part = _cash_pnl(leg)
        if part is None:
            return _decision(False, "insufficient_inputs", None, defined, None, symbols)
        pnl += part
    loss = max(0.0, -pnl)
    budgets = []
    percent = _positive_limit(max_loss_pct)
    cash = _positive_limit(max_loss_cash)
    if defined is not None and percent is not None and not incomplete:
        budgets.append(defined * percent)
    if cash is not None:
        budgets.append(cash)
    if not budgets:
        if incomplete:
            reason = "incomplete_hedge"
        elif defined is None:
            reason = "undefined_risk"
        else:
            reason = "no_budget"
        return _decision(False, reason, loss, defined, None, symbols)
    budget = min(budgets)
    breached = loss > 0 and loss + 1e-8 >= budget
    return _decision(
        breached,
        "loss_budget" if breached else "within_budget",
        loss,
        defined,
        budget,
        symbols,
    )


def assess_symbol_loss(
    symbol,
    legs,
    *,
    max_loss_pct=None,
    max_loss_cash=None,
    covered=False,
) -> OptionLossDecision:
    """判断 symbol 所在结构是否已用完可承受亏损。"""
    matched = [leg for leg in legs if leg.symbol == symbol and _finite(leg.signed_quantity) not in {None, 0.0}]
    if not matched:
        return _decision(False, "no_position", None, None, None, ())
    if len(matched) > 1:
        return _decision(False, "ambiguous_position", None, None, None, (symbol,))
    leg = matched[0]
    if not _priced(leg):
        return _decision(False, "insufficient_inputs", None, None, None, (symbol,))

    partners = [other for other in legs if _is_equal_vertical(leg, other) and _priced(other)]
    if partners:
        partner = min(partners, key=lambda item: abs(item.strike - leg.strike))
        return _assess_group(
            (leg, partner),
            max_loss_pct,
            max_loss_cash,
            _vertical_max_loss(leg, partner),
        )

    incomplete = _incomplete_hedges(leg, legs)
    if incomplete:
        return _assess_group(
            (leg, *incomplete),
            max_loss_pct,
            max_loss_cash,
            None,
            incomplete=True,
        )

    if leg.signed_quantity < 0 and str(leg.option_type).upper() == "CALL" and covered:
        return _decision(False, "covered_call", max(0.0, -(_cash_pnl(leg) or 0.0)), None, None, (symbol,))

    return _assess_group((leg,), max_loss_pct, max_loss_cash, _single_defined_loss(leg))


__all__ = ["OptionLegSnapshot", "OptionLossDecision", "assess_symbol_loss"]
