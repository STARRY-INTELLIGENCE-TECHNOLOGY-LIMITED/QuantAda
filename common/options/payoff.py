"""通用期权策略到期损益分析与计划文本生成工具。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


class PayoffInputError(ValueError):
    """损益腿输入不合法。"""


@dataclass(frozen=True)
class OptionLeg:
    """期权腿；contracts 为正数多仓、负数空仓，premium 为每份报价。"""

    symbol: str
    option_type: str
    contracts: float
    strike: float
    premium: float
    # 不提供默认乘数；不同市场的期权名义乘数差异很大，缺失时必须失败关闭。
    contract_multiplier: float | None = None
    expiry: object = None


@dataclass(frozen=True)
class UnderlyingLeg:
    """现货/ETF 腿；shares 为正数多仓、负数空仓。"""

    symbol: str
    shares: float
    entry_price: float


@dataclass(frozen=True)
class PayoffAnalysis:
    """到期损益曲线及其可审计摘要。"""

    prices: tuple
    pnl: tuple
    breakevens: tuple
    profit_intervals: tuple
    loss_intervals: tuple
    max_profit: float | None
    max_loss: float | None
    unbounded_profit: bool
    unbounded_loss: bool
    spot: float | None
    currency: str = ""


def _finite(value, name, *, positive=False, nonnegative=False):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PayoffInputError(f"invalid {name}: {value!r}") from None
    if not math.isfinite(parsed):
        raise PayoffInputError(f"invalid {name}: {value!r}")
    if positive and parsed <= 0:
        raise PayoffInputError(f"{name} must be positive: {value!r}")
    if nonnegative and parsed < 0:
        raise PayoffInputError(f"{name} must be non-negative: {value!r}")
    return parsed


def _option_type(value):
    normalized = str(value or "").strip().upper()
    if normalized in {"C", "CALL"}:
        return "CALL"
    if normalized in {"P", "PUT"}:
        return "PUT"
    raise PayoffInputError(f"unsupported option type: {value!r}")


def payoff_at_expiry(price, option_legs=(), underlying_legs=(), fixed_cash=0.0):
    """计算指定标的价格下的到期净损益。"""
    spot = _finite(price, "price", nonnegative=True)
    result = _finite(fixed_cash, "fixed_cash")
    for leg in option_legs or ():
        option_type = _option_type(leg.option_type)
        contracts = _finite(leg.contracts, "contracts")
        strike = _finite(leg.strike, "strike", positive=True)
        premium = _finite(leg.premium, "premium", nonnegative=True)
        multiplier = _finite(
            leg.contract_multiplier,
            "contract_multiplier",
            positive=True,
        )
        intrinsic = (
            max(spot - strike, 0.0)
            if option_type == "CALL"
            else max(strike - spot, 0.0)
        )
        result += contracts * (intrinsic - premium) * multiplier
    for leg in underlying_legs or ():
        shares = _finite(leg.shares, "shares")
        entry_price = _finite(leg.entry_price, "entry_price", nonnegative=True)
        result += shares * (spot - entry_price)
    return float(result)


def _intervals(prices, pnl, profitable):
    """把采样点上的盈亏布尔值压缩成价格区间。"""
    mask = np.asarray(pnl) >= 0 if profitable else np.asarray(pnl) < 0
    if not mask.any():
        return ()
    intervals = []
    start = None
    for index, enabled in enumerate(mask):
        if enabled and start is None:
            start = index
        if start is not None and (not enabled or index == len(mask) - 1):
            end = index if enabled and index == len(mask) - 1 else index - 1
            intervals.append((float(prices[start]), float(prices[end])))
            start = None
    return tuple(intervals)


def _breakevens(prices, pnl, tolerance=1e-10):
    """通过相邻采样点线性插值计算盈亏平衡点。"""
    result = []
    for left, right, left_pnl, right_pnl in zip(
        prices[:-1], prices[1:], pnl[:-1], pnl[1:]
    ):
        if abs(left_pnl) <= tolerance:
            result.append(float(left))
        if left_pnl * right_pnl < 0 and right_pnl != left_pnl:
            result.append(float(left + (right - left) * (-left_pnl) / (right_pnl - left_pnl)))
    if len(pnl) > 0 and abs(pnl[-1]) <= tolerance:
        result.append(float(prices[-1]))
    return tuple(sorted({round(value, 10) for value in result}))


def analyze_payoff(
    option_legs=(),
    underlying_legs=(),
    *,
    fixed_cash=0.0,
    spot=None,
    price_min=0.0,
    price_max=None,
    grid_points=1001,
    currency="",
):
    """分析单腿或同一到期日多腿策略的到期盈亏、区间和最大收益/损失。

    不同到期日的路径损益需要逐日重估，若混用则直接拒绝，避免把多到期
    组合错误压成一个静态到期图。
    """
    option_legs = tuple(option_legs or ())
    underlying_legs = tuple(underlying_legs or ())
    # 使用时间戳归一化比较到期日，避免同一到期日的字符串/时区差异被误判。
    expiries = set()
    for leg in option_legs:
        if leg.expiry is None:
            continue
        try:
            import pandas as pd

            expiry = pd.Timestamp(leg.expiry)
            if expiry.tzinfo is not None:
                expiry = expiry.tz_convert("UTC")
            expiries.add(expiry.normalize())
        except Exception as exc:
            raise PayoffInputError(f"invalid expiry: {leg.expiry!r}") from exc
    if len(expiries) > 1:
        raise PayoffInputError(
            "mixed expiries require a path-aware payoff model"
        )
    strikes = [
        _finite(leg.strike, "strike", positive=True)
        for leg in option_legs
    ]
    spot_value = None if spot is None else _finite(spot, "spot", nonnegative=True)
    lower = _finite(price_min, "price_min", nonnegative=True)
    reference = max(strikes + ([spot_value] if spot_value is not None else [0.0]) + [1.0])
    upper = (
        _finite(price_max, "price_max", positive=True)
        if price_max is not None
        else max(reference * 2.0, reference + 100.0)
    )
    if upper <= lower:
        raise PayoffInputError("price_max must be greater than price_min")
    try:
        points = int(grid_points)
    except (TypeError, ValueError, OverflowError):
        points = 1001
    points = max(101, min(points, 10001))
    # 把执行价加入网格，避免采样点恰好错过折点而低估最大盈亏或漏报平衡点。
    prices = np.unique(np.concatenate([
        np.linspace(lower, upper, points),
        np.asarray([value for value in strikes if lower <= value <= upper], dtype=float),
        np.asarray([lower, upper], dtype=float),
    ]))
    pnl = np.array([
        payoff_at_expiry(price, option_legs, underlying_legs, fixed_cash)
        for price in prices
    ])

    # 价格只能趋近于零而不能低于零；显式评估零点以覆盖 Put 的最大损失/收益。
    zero_pnl = payoff_at_expiry(lower, option_legs, underlying_legs, fixed_cash)
    high_probe = max(upper * 2.0, upper + 1.0)
    high_pnl = payoff_at_expiry(high_probe, option_legs, underlying_legs, fixed_cash)
    high_slope = (high_pnl - pnl[-1]) / max(high_probe - upper, 1e-12)
    unbounded_profit = bool(high_slope > 1e-8)
    unbounded_loss = bool(high_slope < -1e-8)
    if unbounded_profit:
        max_profit = None
    else:
        max_profit = float(max(np.max(pnl), zero_pnl))
    if unbounded_loss:
        max_loss = None
    else:
        max_loss = float(max(0.0, -min(np.min(pnl), zero_pnl)))

    return PayoffAnalysis(
        prices=tuple(float(value) for value in prices),
        pnl=tuple(float(value) for value in pnl),
        breakevens=_breakevens(prices, pnl),
        profit_intervals=_intervals(prices, pnl, True),
        loss_intervals=_intervals(prices, pnl, False),
        max_profit=max_profit,
        max_loss=max_loss,
        unbounded_profit=unbounded_profit,
        unbounded_loss=unbounded_loss,
        spot=spot_value,
        currency=str(currency or ""),
    )


def _format_number(value, currency=""):
    if value is None:
        return "无限"
    return f"{currency}{value:,.2f}"


def format_payoff_plan(title, analysis: PayoffAnalysis, legs=()):
    """把损益分析转换为适合 IM Plan 的 Markdown 文本。"""
    def ranges(values, unbounded=False):
        if not values:
            return "无"
        formatted = []
        for index, (left, right) in enumerate(values):
            right_text = "∞" if unbounded and index == len(values) - 1 else f"{right:,.2f}"
            formatted.append(f"{left:,.2f} ~ {right_text}")
        return "；".join(formatted)

    lines = [
        f"### {title}",
        "- 分析类型：到期损益图（不代表路径内保证金或实时清算值）",
        f"- 现货参考价：{analysis.spot:,.2f}" if analysis.spot is not None else "- 现货参考价：未提供",
        f"- 最大盈利：{_format_number(analysis.max_profit, analysis.currency)}",
        f"- 最大亏损：{_format_number(analysis.max_loss, analysis.currency)}",
        f"- 盈亏平衡点：{', '.join(f'{value:,.2f}' for value in analysis.breakevens) or '无'}",
        f"- 盈利区间：{ranges(analysis.profit_intervals, analysis.unbounded_profit)}",
        f"- 亏损区间：{ranges(analysis.loss_intervals, analysis.unbounded_loss)}",
        "",
        "| 腿 | 方向 | 数量 | 执行价 | 权利金 | 乘数 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for leg in legs or ():
        if isinstance(leg, OptionLeg):
            multiplier = _finite(
                leg.contract_multiplier,
                "contract_multiplier",
                positive=True,
            )
            lines.append(
                f"| `{leg.symbol}` | {str(leg.option_type).upper()} | "
                f"{leg.contracts:g} | {leg.strike:,.2f} | {leg.premium:,.2f} | "
                f"{multiplier:g} |"
            )
        elif isinstance(leg, UnderlyingLeg):
            lines.append(
                f"| `{leg.symbol}` | UNDERLYING | {leg.shares:g} | - | "
                f"{leg.entry_price:,.2f} | 1 |"
            )
    return "\n".join(lines)


__all__ = [
    "PayoffInputError",
    "OptionLeg",
    "UnderlyingLeg",
    "PayoffAnalysis",
    "payoff_at_expiry",
    "analyze_payoff",
    "format_payoff_plan",
]
