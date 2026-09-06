"""Cash-Secured Put 与 Covered Call 的账户级风险计算。"""

from __future__ import annotations

from dataclasses import dataclass


class OptionMarginError(ValueError):
    """期权保证金输入或担保条件不合法。"""


@dataclass(frozen=True)
class OptionRiskLeg:
    """单个期权腿；正数为多仓，负数为短仓。"""

    symbol: str
    underlying: str
    option_type: str
    signed_quantity: float
    strike: float
    premium: float
    spot: float
    contract_multiplier: float


@dataclass(frozen=True)
class OptionMarginSnapshot:
    """账户级保证金与压力结果。"""

    cash: float
    margin_used: float
    available_margin: float
    margin_utilization: float
    max_loss_estimate: float
    stress_loss_down: float
    stress_loss_up: float
    assignment_obligations: tuple


def _positive(value, name):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise OptionMarginError(f"invalid {name}: {value!r}") from None
    if parsed != parsed or parsed in (float("inf"), float("-inf")) or parsed <= 0:
        raise OptionMarginError(f"invalid {name}: {value!r}")
    return parsed


def _nonnegative(value, name):
    parsed = _finite(value, name)
    if parsed < 0:
        raise OptionMarginError(f"invalid {name}: {value!r}")
    return parsed


def _finite(value, name):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise OptionMarginError(f"invalid {name}: {value!r}") from None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise OptionMarginError(f"invalid {name}: {value!r}")
    return parsed


def _type(value):
    text = str(value or "").strip().upper()
    if text in {"C", "CALL"}:
        return "CALL"
    if text in {"P", "PUT"}:
        return "PUT"
    raise OptionMarginError(f"unsupported option type: {value!r}")


def _intrinsic(option_type, spot, strike):
    return max(spot - strike, 0.0) if option_type == "CALL" else max(strike - spot, 0.0)


def compute_option_margin(
    legs,
    *,
    cash,
    underlying_positions=None,
    stress_down=0.20,
    stress_up=0.20,
) -> OptionMarginSnapshot:
    """计算当前仅允许的现金担保 Put 与 Covered Call 风险。

    不支持裸卖和多腿组合；任何担保不足直接抛出，调用方必须 fail-closed。
    """
    cash_value = _finite(cash, "cash")
    if cash_value < 0:
        raise OptionMarginError("cash must not be negative")
    if not isinstance(legs, (list, tuple)):
        raise OptionMarginError("legs must be a list or tuple")
    if len(legs) > 1:
        raise OptionMarginError("multi-leg option combinations are unsupported")
    positions = dict(underlying_positions or {})
    margin_used = 0.0
    max_loss = 0.0
    stress_losses = {"down": 0.0, "up": 0.0}
    obligations = []

    for leg in legs:
        if not isinstance(leg, OptionRiskLeg):
            raise OptionMarginError("legs must contain OptionRiskLeg values")
        option_type = _type(leg.option_type)
        quantity = _finite(leg.signed_quantity, "signed_quantity")
        strike = _positive(leg.strike, "strike")
        premium = _nonnegative(leg.premium, "premium")
        spot = _positive(leg.spot, "spot")
        multiplier = _positive(leg.contract_multiplier, "contract_multiplier")
        if quantity == 0:
            continue
        underlying_size = _finite(positions.get(leg.underlying, 0.0), "underlying_position")
        if quantity < 0 and option_type == "PUT":
            margin_used += abs(quantity) * strike * multiplier
            if margin_used > cash_value + 1e-12:
                raise OptionMarginError("cash-secured put collateral is insufficient")
            max_loss += abs(quantity) * strike * multiplier - abs(quantity) * premium * multiplier
            obligations.append({
                "symbol": leg.symbol,
                "type": "PUT_ASSIGNMENT_BUY",
                "shares": abs(quantity) * multiplier,
                "cash": abs(quantity) * strike * multiplier,
            })
        elif quantity < 0 and option_type == "CALL":
            required_shares = abs(quantity) * multiplier
            if underlying_size < required_shares:
                raise OptionMarginError("covered call underlying collateral is insufficient")
            max_loss += max(spot - strike, 0.0) * required_shares
            obligations.append({
                "symbol": leg.symbol,
                "type": "CALL_ASSIGNMENT_DELIVER",
                "shares": required_shares,
                "cash": strike * required_shares,
            })
        elif quantity > 0:
            max_loss += quantity * premium * multiplier
        else:
            raise OptionMarginError("unsupported naked short option")

        current_value = quantity * _intrinsic(option_type, spot, strike) * multiplier
        for key, factor in (("down", 1.0 - _finite(stress_down, "stress_down")),
                            ("up", 1.0 + _finite(stress_up, "stress_up"))):
            stress_spot = max(0.0, spot * factor)
            stress_value = quantity * _intrinsic(option_type, stress_spot, strike) * multiplier
            underlying_change = underlying_size * (stress_spot - spot)
            stress_losses[key] += max(0.0, -(stress_value - current_value + underlying_change))

    available = cash_value - margin_used
    utilization = margin_used / cash_value if cash_value > 0 else (0.0 if margin_used == 0 else float("inf"))
    return OptionMarginSnapshot(
        cash=cash_value,
        margin_used=margin_used,
        available_margin=available,
        margin_utilization=utilization,
        max_loss_estimate=max(0.0, max_loss),
        stress_loss_down=stress_losses["down"],
        stress_loss_up=stress_losses["up"],
        assignment_obligations=tuple(obligations),
    )


__all__ = ["OptionMarginError", "OptionRiskLeg", "OptionMarginSnapshot", "compute_option_margin"]
