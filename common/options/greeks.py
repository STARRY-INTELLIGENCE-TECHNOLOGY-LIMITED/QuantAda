"""稳定、无副作用的 Black-Scholes 单合约与组合 Greeks 计算。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OptionGreeks:
    """单合约 Greeks；值已乘以合约乘数，不含持仓数量。"""

    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float

    def __iter__(self):
        return iter((self.delta, self.gamma, self.theta, self.vega, self.rho))


def _finite(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _normalise_units(units):
    text = str(units or "decimal").strip().lower()
    if text in {"decimal", "fraction", "frac"}:
        return "decimal"
    if text in {"percent", "%", "percentage"}:
        return "percent"
    if text == "auto":
        return "auto"
    raise ValueError(f"unsupported units: {units!r}")


def _to_decimal(value, units, *, nonnegative=True):
    parsed = _finite(value, 0.0)
    if nonnegative and parsed < 0:
        return 0.0
    normalized = _normalise_units(units)
    if normalized == "percent":
        return parsed / 100.0
    if normalized == "auto":
        # auto 仅用于兼容外部链数据；明确输入建议使用 decimal/percent。
        return parsed / 100.0 if parsed > 1.0 else parsed
    return parsed


def effective_volatility(iv, hv, fallback=0.0, *, units="decimal") -> float:
    """优先使用当前 IV，缺失时才回退 HV，并统一返回小数形式。

    ``units='decimal'`` 表示 0.25=25%，``units='percent'`` 表示 25=25%。
    ``auto`` 只作兼容用途，生产调用应明确指定单位。
    """
    normalized = _normalise_units(units)
    for candidate in (iv, hv, fallback):
        parsed = _to_decimal(candidate, normalized, nonnegative=True)
        if parsed > 0:
            return parsed
    return 0.0


def compute_option_greeks(
    option_type,
    spot,
    strike,
    time_to_expiry,
    volatility,
    risk_free_rate=0.0,
    dividend_yield=0.0,
    contract_multiplier=1.0,
    *,
    volatility_units="decimal",
    rate_units="decimal",
    dividend_units="decimal",
) -> OptionGreeks:
    """计算单合约 Greeks；到期或无效输入返回有限边界值。

    波动率、利率和股息率默认使用小数形式；若行情源返回百分数，必须
    通过对应的 ``*_units='percent'`` 显式转换，避免 25 被误解为 2500%。
    """
    kind = str(option_type or "").strip().upper()
    if kind in {"C", "CALL"}:
        kind = "CALL"
    elif kind in {"P", "PUT"}:
        kind = "PUT"
    else:
        return OptionGreeks(0.0, 0.0, 0.0, 0.0, 0.0)
    s = _finite(spot)
    k = _finite(strike)
    t = max(0.0, _finite(time_to_expiry))
    try:
        sigma = _to_decimal(volatility, volatility_units, nonnegative=True)
        rate = _to_decimal(risk_free_rate, rate_units, nonnegative=False)
        dividend = _to_decimal(dividend_yield, dividend_units, nonnegative=False)
    except ValueError:
        return OptionGreeks(0.0, 0.0, 0.0, 0.0, 0.0)
    multiplier = max(0.0, _finite(contract_multiplier, 1.0))
    if s <= 0 or k <= 0 or multiplier <= 0:
        return OptionGreeks(0.0, 0.0, 0.0, 0.0, 0.0)
    if t <= 0 or sigma <= 1e-12:
        if kind == "CALL":
            delta = 1.0 if s > k else 0.0
        else:
            delta = -1.0 if s < k else 0.0
        return OptionGreeks(delta * multiplier, 0.0, 0.0, 0.0, 0.0)

    sqrt_t = math.sqrt(t)
    try:
        d1 = (math.log(s / k) + (rate - dividend + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
        normal_pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
        normal_cdf_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        normal_cdf_d2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        discount = math.exp(-rate * t)
        dividend_discount = math.exp(-dividend * t)
        common = -(s * dividend_discount * normal_pdf * sigma) / (2.0 * sqrt_t)
        if kind == "CALL":
            delta = dividend_discount * normal_cdf_d1
            theta = common - rate * k * discount * normal_cdf_d2 + dividend * s * dividend_discount * normal_cdf_d1
            rho = k * t * discount * normal_cdf_d2
        else:
            delta = dividend_discount * (normal_cdf_d1 - 1.0)
            theta = common + rate * k * discount * (1.0 - normal_cdf_d2) - dividend * s * dividend_discount * (1.0 - normal_cdf_d1)
            rho = -k * t * discount * (1.0 - normal_cdf_d2)
        gamma = dividend_discount * normal_pdf / (s * sigma * sqrt_t)
        vega = s * dividend_discount * normal_pdf * sqrt_t
        result = OptionGreeks(
            delta * multiplier,
            gamma * multiplier,
            theta * multiplier,
            vega * multiplier,
            rho * multiplier,
        )
    except (ArithmeticError, OverflowError, ValueError):
        return OptionGreeks(0.0, 0.0, 0.0, 0.0, 0.0)
    values = tuple(_finite(value) for value in result)
    return OptionGreeks(*values)


def aggregate_option_greeks(legs) -> OptionGreeks:
    """按 signed quantity 聚合多个期权腿；异常值按零处理。"""
    totals = [0.0] * 5
    for item in legs or []:
        if isinstance(item, tuple) and len(item) == 2:
            greeks, quantity = item
        else:
            greeks = getattr(item, "greeks", None)
            quantity = getattr(item, "signed_quantity", 0.0)
        if not isinstance(greeks, OptionGreeks):
            continue
        qty = _finite(quantity)
        for index, value in enumerate(greeks):
            totals[index] += _finite(value) * qty
    return OptionGreeks(*(value if math.isfinite(value) else 0.0 for value in totals))


__all__ = ["OptionGreeks", "effective_volatility", "compute_option_greeks", "aggregate_option_greeks"]
