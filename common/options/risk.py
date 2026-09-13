"""期权现金义务、担保条件与压力风险的纯计算工具。"""

from __future__ import annotations

from dataclasses import dataclass

from .analytics import parse_expiry, underlying_key


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
    # 可选压力元数据，保留旧版位置参数兼容性。
    expiry: object = None
    historical_volatility: float | None = None
    volatility_shock: float | None = None
    price_shock: float | None = None


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


def _finite(value, name):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise OptionMarginError(f"invalid {name}: {value!r}") from None
    if not parsed == parsed or parsed in (float("inf"), float("-inf")):
        raise OptionMarginError(f"invalid {name}: {value!r}")
    return parsed


def _positive(value, name):
    parsed = _finite(value, name)
    if parsed <= 0:
        raise OptionMarginError(f"invalid {name}: {value!r}")
    return parsed


def _nonnegative(value, name):
    parsed = _finite(value, name)
    if parsed < 0:
        raise OptionMarginError(f"invalid {name}: {value!r}")
    return parsed


def _type(value):
    text = str(value or "").strip().upper()
    if text in {"C", "CALL"}:
        return "CALL"
    if text in {"P", "PUT"}:
        return "PUT"
    raise OptionMarginError(f"unsupported option type: {value!r}")


def _expiry_key(value):
    """返回可用于匹配的到期日；空值和 NaT 不能作为保护依据。"""
    if value is None:
        return ""
    text = str(value).strip()
    if text.upper() in {"", "NONE", "NAN", "NAT"}:
        return ""
    # 风险腿可能来自 DataFrame（Timestamp）、Futu 六位日期或普通字符串；
    # 统一成 YYYYMMDD，避免同一到期日因表示形式不同而失去保护配对。
    try:
        parsed = parse_expiry(value)
        if parsed is not None and str(parsed).upper() not in {"NAT", "NAN"}:
            return parsed.strftime("%Y%m%d")
    except Exception:
        pass
    return ""


def _intrinsic(option_type, spot, strike):
    return max(spot - strike, 0.0) if option_type == "CALL" else max(strike - spot, 0.0)


def _underlying_position(positions, underlying):
    """按原始键或归一化底层键读取持仓数量。"""
    if underlying in positions:
        return positions[underlying]
    identity = underlying_key(underlying)
    for key, value in positions.items():
        if underlying_key(key) == identity:
            return value
    return 0.0


def _validated_legs(legs, positions):
    records = []
    for leg in legs:
        if not isinstance(leg, OptionRiskLeg):
            raise OptionMarginError("legs must contain OptionRiskLeg values")
        records.append({
            "leg": leg,
            "type": _type(leg.option_type),
            "quantity": _finite(leg.signed_quantity, "signed_quantity"),
            "strike": _positive(leg.strike, "strike"),
            "premium": _nonnegative(leg.premium, "premium"),
            "spot": _positive(leg.spot, "spot"),
            "multiplier": _positive(leg.contract_multiplier, "contract_multiplier"),
            "underlying_size": _finite(
                _underlying_position(positions, leg.underlying),
                "underlying_position",
            ),
        })
    return records


def _find_spread_pairs(records):
    """匹配数量、到期日和乘数均一致的 Put 保护腿。"""
    pairs = {}
    used_hedges = set()
    for short_index, short in enumerate(records):
        if short["quantity"] >= 0 or short["type"] != "PUT":
            continue
        short_leg = short["leg"]
        for hedge_index, hedge in enumerate(records):
            if hedge_index == short_index or hedge_index in used_hedges:
                continue
            if hedge["quantity"] <= 0 or hedge["type"] != "PUT":
                continue
            hedge_leg = hedge["leg"]
            if underlying_key(hedge_leg.underlying) != underlying_key(short_leg.underlying):
                continue
            if hedge["strike"] >= short["strike"]:
                continue
            # 未知到期日不能证明是同一合约；None == None 不得触发价差保护。
            short_expiry = _expiry_key(short_leg.expiry)
            hedge_expiry = _expiry_key(hedge_leg.expiry)
            if not short_expiry or not hedge_expiry or short_expiry != hedge_expiry:
                continue
            if abs(hedge["multiplier"] - short["multiplier"]) > 1e-12:
                continue
            if abs(hedge["spot"] - short["spot"]) > max(1e-9, abs(short["spot"]) * 1e-6):
                continue
            if abs(hedge["quantity"] - abs(short["quantity"])) > 1e-12:
                continue
            pairs[short_index] = hedge_index
            used_hedges.add(hedge_index)
            break
    return pairs, used_hedges


def _stress_leg(record, underlying_size, stress_down, stress_up, result, *, include_underlying=True):
    """把单腿现货压力损失累加到结果字典。"""
    quantity = record["quantity"]
    spot = record["spot"]
    strike = record["strike"]
    multiplier = record["multiplier"]
    current_value = quantity * _intrinsic(record["type"], spot, strike) * multiplier
    down = stress_down
    leg = record["leg"]
    if leg.price_shock is not None:
        down = max(down, _nonnegative(leg.price_shock, "price_shock"))
    for key, factor in (("down", 1.0 - down), ("up", 1.0 + stress_up)):
        stress_spot = max(0.0, spot * factor)
        stress_value = quantity * _intrinsic(record["type"], stress_spot, strike) * multiplier
        underlying_change = (
            underlying_size * (stress_spot - spot)
            if include_underlying
            else 0.0
        )
        result[key] += max(0.0, -(stress_value - current_value + underlying_change))


def compute_option_margin(
    legs,
    *,
    cash,
    underlying_positions=None,
    stress_down=0.20,
    stress_up=0.20,
    portfolio_margin=False,
    volatility_shock=0.0,
    price_shock=None,
) -> OptionMarginSnapshot:
    """计算现金担保 Put、Covered Call 与定义风险 Put Spread。"""
    cash_value = _finite(cash, "cash")
    if cash_value < 0:
        raise OptionMarginError("cash must not be negative")
    if not isinstance(legs, (list, tuple)):
        raise OptionMarginError("legs must be a list or tuple")

    positions = dict(underlying_positions or {})
    records = _validated_legs(legs, positions)
    portfolio_mode = bool(portfolio_margin)
    if len(records) > 1 and not portfolio_mode and any(item["quantity"] < 0 for item in records):
        raise OptionMarginError("multi-leg combinations with short legs require a defined-risk model")

    stress_down_value = _nonnegative(stress_down, "stress_down")
    stress_up_value = _nonnegative(stress_up, "stress_up")
    configured_vol_shock = _nonnegative(volatility_shock, "volatility_shock")
    configured_drop = stress_down_value if price_shock is None else _nonnegative(price_shock, "price_shock")
    pairs, paired_hedges = _find_spread_pairs(records) if portfolio_mode else ({}, set())

    margin_used = 0.0
    max_loss = 0.0
    stress_losses = {"down": 0.0, "up": 0.0}
    obligations = []
    stress_underlyings = set()

    for index, record in enumerate(records):
        quantity = record["quantity"]
        if quantity == 0:
            continue
        strike = record["strike"]
        premium = record["premium"]
        multiplier = record["multiplier"]
        spot = record["spot"]
        leg = record["leg"]

        if quantity < 0 and record["type"] == "PUT":
            required = abs(quantity) * strike * multiplier
            if portfolio_mode:
                hv_raw = leg.historical_volatility
                leg_vol_shock = _nonnegative(leg.volatility_shock, "volatility_shock") if leg.volatility_shock is not None else 0.0
                leg_drop = _nonnegative(leg.price_shock, "price_shock") if leg.price_shock is not None else 0.0
                if hv_raw is None:
                    # 只有显式价格压力而没有波动率压力时，才允许仅按价格情景估算；
                    # 不能把缺失 HV 当成零波动率来吞掉已配置的波动率压力。
                    if (
                        (leg.price_shock is None and price_shock is None)
                        or leg.volatility_shock is not None
                        or configured_vol_shock > 0
                    ):
                        raise OptionMarginError(
                            "historical_volatility is required for volatility stress"
                        )
                    hv = 0.0
                else:
                    hv = _nonnegative(hv_raw, "historical_volatility")
                drop = max(configured_drop, leg_drop)
                stress_factor = min(max(1.0 + hv * max(leg_vol_shock, configured_vol_shock, drop), 1.0), 10.0)
                stress_spot = max(0.0, spot * (1.0 - drop))
                stress_loss = max(0.0, _intrinsic("PUT", stress_spot, strike) - premium) * abs(quantity) * multiplier
                required = max(required * stress_factor, stress_loss)

                hedge_index = pairs.get(index)
                if hedge_index is not None:
                    hedge = records[hedge_index]
                    spread_loss = max(0.0, (strike - hedge["strike"]) - premium + hedge["premium"]) * abs(quantity) * multiplier
                    combined_now = -_intrinsic("PUT", spot, strike) + _intrinsic("PUT", spot, hedge["strike"])
                    combined_stress = -_intrinsic("PUT", stress_spot, strike) + _intrinsic("PUT", stress_spot, hedge["strike"])
                    stress_loss = max(0.0, -(combined_stress - combined_now + premium - hedge["premium"])) * abs(quantity) * multiplier
                    # 纵向价差的最坏损失被执行价宽度减权利金严格封顶；
                    # 压力因子只能提高保守估计，不能超过已定义的风险上限。
                    required = min(spread_loss, max(spread_loss * stress_factor, stress_loss))
                    max_loss += spread_loss
                else:
                    max_loss += max(0.0, abs(quantity) * strike * multiplier - abs(quantity) * premium * multiplier)
            else:
                max_loss += abs(quantity) * strike * multiplier - abs(quantity) * premium * multiplier
            margin_used += required
            if margin_used > cash_value + 1e-12:
                raise OptionMarginError("cash-secured put collateral is insufficient")
            obligations.append({
                "symbol": leg.symbol,
                "type": "PUT_ASSIGNMENT_BUY",
                "shares": abs(quantity) * multiplier,
                "cash": abs(quantity) * strike * multiplier,
            })
        elif quantity < 0 and record["type"] == "CALL":
            required_shares = abs(quantity) * multiplier
            if record["underlying_size"] < required_shares:
                raise OptionMarginError("covered call underlying collateral is insufficient")
            max_loss += max(spot - strike, 0.0) * required_shares
            obligations.append({
                "symbol": leg.symbol,
                "type": "CALL_ASSIGNMENT_DELIVER",
                "shares": required_shares,
                "cash": strike * required_shares,
            })
        elif quantity > 0:
            if not (portfolio_mode and index in paired_hedges):
                max_loss += quantity * premium * multiplier
        else:
            raise OptionMarginError("unsupported naked short option")

        if not (portfolio_mode and (index in pairs or index in paired_hedges)):
            underlying_identity = underlying_key(leg.underlying)
            include_underlying = underlying_identity not in stress_underlyings
            if include_underlying:
                stress_underlyings.add(underlying_identity)
            _stress_leg(
                record,
                record["underlying_size"],
                stress_down_value,
                stress_up_value,
                stress_losses,
                include_underlying=include_underlying,
            )

    if portfolio_mode:
        for short_index, hedge_index in pairs.items():
            short = records[short_index]
            hedge = records[hedge_index]
            net_credit = short["leg"].premium - hedge["leg"].premium
            down = max(
                stress_down_value,
                _nonnegative(short["leg"].price_shock, "price_shock") if short["leg"].price_shock is not None else 0.0,
                _nonnegative(hedge["leg"].price_shock, "price_shock") if hedge["leg"].price_shock is not None else 0.0,
            )
            spread_loss_cap = max(0.0, (short["strike"] - hedge["strike"] - net_credit) * abs(short["quantity"]) * short["multiplier"])
            for key, factor in (("down", 1.0 - down), ("up", 1.0 + stress_up_value)):
                stress_spot = max(0.0, short["spot"] * factor)
                current_value = -_intrinsic("PUT", short["spot"], short["strike"]) + _intrinsic("PUT", short["spot"], hedge["strike"])
                stress_value = -_intrinsic("PUT", stress_spot, short["strike"]) + _intrinsic("PUT", stress_spot, hedge["strike"])
                loss = max(0.0, -(stress_value - current_value + net_credit)) * abs(short["quantity"]) * short["multiplier"]
                option_loss = min(spread_loss_cap, loss)
                underlying_identity = underlying_key(short["leg"].underlying)
                include_underlying = underlying_identity not in stress_underlyings
                if include_underlying:
                    stress_underlyings.add(underlying_identity)
                underlying_change = short["underlying_size"] * (stress_spot - short["spot"])
                stress_losses[key] += option_loss + (
                    max(0.0, -underlying_change) if include_underlying else 0.0
                )

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
