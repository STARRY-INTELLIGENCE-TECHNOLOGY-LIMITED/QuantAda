"""实盘期权成交推送的到期损益附录收集器。

本模块只把券商持仓快照映射为通用损益腿，计算仍使用 common/options/payoff。
构建失败时返回空串，不得打断成交推送。
"""

from __future__ import annotations

import math

from common.options.analytics import parse_option_symbol, underlying_key
from common.options.contracts import (
    InvalidOptionOrderEffect,
    normalize_option_order_effect,
    signed_position_delta,
)
from common.options.payoff import (
    OptionLeg,
    UnderlyingLeg,
    analyze_payoff,
    format_payoff_fill_summary,
)


def collect_option_fill_payoff_summary(
    broker,
    *,
    fill_symbol,
    fill_price,
    fill_size,
    order_effect=None,
    is_buy=False,
    is_sell=False,
    is_combo=False,
    fill_data=None,
    combo_legs=None,
) -> str:
    """生成期权成交的短到期损益附录；股票或构建失败时返回空串。"""
    try:
        return _build_option_fill_payoff_summary(
            broker,
            fill_symbol=fill_symbol,
            fill_price=fill_price,
            fill_size=fill_size,
            order_effect=order_effect,
            is_buy=is_buy,
            is_sell=is_sell,
            is_combo=is_combo,
            fill_data=fill_data,
            combo_legs=combo_legs,
        )
    except Exception:
        return ""


def _build_option_fill_payoff_summary(
    broker,
    *,
    fill_symbol,
    fill_price,
    fill_size,
    order_effect=None,
    is_buy=False,
    is_sell=False,
    is_combo=False,
    fill_data=None,
    combo_legs=None,
) -> str:
    if broker is None:
        return ""
    fill_parsed = parse_option_symbol(fill_symbol)
    if not fill_parsed.get("option_type"):
        return ""
    fill_underlying = underlying_key(fill_parsed.get("underlying") or "")
    fill_expiry = _expiry_key(fill_parsed.get("expiry"))
    if not fill_underlying or fill_expiry is None:
        return ""

    fill_delta, is_close = _signed_fill_delta(
        fill_size,
        order_effect=order_effect,
        is_buy=is_buy,
        is_sell=is_sell,
    )
    feeds = list(getattr(broker, "datas", None) or ())
    if fill_data is not None and all(
        str(getattr(item, "_name", "") or "") != str(fill_symbol)
        for item in feeds
    ):
        feeds.append(fill_data)

    option_sizes = {}
    option_meta = {}
    underlying_legs = []
    spot = None
    for data in feeds:
        name = str(getattr(data, "_name", "") or "")
        if not name:
            continue
        parsed = parse_option_symbol(name)
        position = broker.get_position(data)
        size = _finite_number(getattr(position, "size", 0) or 0)
        if size is None:
            return ""
        if parsed.get("option_type"):
            if underlying_key(parsed.get("underlying") or "") != fill_underlying:
                continue
            if _expiry_key(parsed.get("expiry")) != fill_expiry:
                continue
            multiplier = _positive_multiplier(broker, data)
            if multiplier is None:
                return ""
            option_sizes[name] = size
            option_meta[name] = {
                "parsed": parsed,
                "premium": _premium(getattr(position, "price", None), None) if size != 0 else None,
                "multiplier": multiplier,
            }
            continue
        if underlying_key(name) != fill_underlying:
            continue
        current_price = _safe_current_price(broker, data)
        if spot is None:
            spot = current_price
        if size == 0:
            continue
        entry_price = _premium(getattr(position, "price", None), current_price)
        if entry_price is None:
            continue
        underlying_legs.append(UnderlyingLeg(name, size, entry_price))

    # 开仓且快照仍为空时叠加本次成交；平仓后已空仓则不再生成相反腿。
    fill_snap = option_sizes.get(str(fill_symbol), 0.0)
    if abs(fill_snap) <= 0 and not is_close and fill_delta != 0:
        option_sizes[str(fill_symbol)] = fill_delta
        if str(fill_symbol) not in option_meta:
            multiplier = _positive_multiplier(broker, fill_data)
            if multiplier is None:
                return ""
            option_meta[str(fill_symbol)] = {
                "parsed": fill_parsed,
                "premium": None,
                "multiplier": multiplier,
            }
    _overlay_combo_open_legs(
        option_sizes,
        option_meta,
        combo_legs,
        fill_underlying,
        fill_expiry,
        broker,
    )

    fill_premium = _premium(fill_price, None)
    option_legs = []
    for name, size in option_sizes.items():
        if size == 0:
            continue
        meta = option_meta.get(name)
        if meta is None:
            return ""
        # 组合成交价是净价/均价，不能覆盖单腿权利金。
        premium = meta.get("premium")
        if premium is None and not is_combo and name == str(fill_symbol):
            premium = fill_premium
        if premium is None:
            return ""
        parsed = meta["parsed"]
        strike = _finite_number(parsed.get("strike"), positive=True)
        if strike is None:
            return ""
        option_legs.append(
            OptionLeg(
                name,
                parsed.get("option_type"),
                size,
                strike,
                premium,
                meta["multiplier"],
                parsed.get("expiry"),
            )
        )
    if not option_legs:
        return ""
    if is_combo and len(option_legs) < 2:
        return ""
    analysis = analyze_payoff(
        option_legs,
        underlying_legs,
        spot=spot,
    )
    return format_payoff_fill_summary(analysis)



def _overlay_combo_open_legs(option_sizes, option_meta, combo_legs, fill_underlying, fill_expiry, broker):
    """快照滞后时，用组合开仓腿补齐同标的同到期日仓位。"""
    if not combo_legs:
        return
    for item in combo_legs:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        name = str(item.get("symbol") or getattr(data, "_name", "") or "")
        parsed = parse_option_symbol(name)
        if not parsed.get("option_type"):
            continue
        if underlying_key(parsed.get("underlying") or "") != fill_underlying:
            continue
        if _expiry_key(parsed.get("expiry")) != fill_expiry:
            continue
        effect = item.get("effect")
        try:
            normalized = normalize_option_order_effect(effect) if effect else ""
        except (InvalidOptionOrderEffect, TypeError, ValueError):
            continue
        if normalized not in {"BUY_TO_OPEN", "SELL_TO_OPEN"}:
            continue
        risk_leg = item.get("risk_leg")
        signed = _finite_number(getattr(risk_leg, "signed_quantity", None))
        if signed is None:
            signed, _is_close = _signed_fill_delta(item.get("volume"), order_effect=normalized)
        if not signed:
            continue
        if abs(option_sizes.get(name, 0.0)) <= 0:
            option_sizes[name] = signed
        multiplier = _finite_number(getattr(risk_leg, "contract_multiplier", None), positive=True)
        if multiplier is None:
            multiplier = _positive_multiplier(broker, data)
        premium = _premium(item.get("price"), getattr(risk_leg, "premium", None))
        current = option_meta.get(name) or {
            "parsed": parsed,
            "premium": None,
            "multiplier": None,
        }
        if current.get("premium") is None:
            current["premium"] = premium
        if current.get("multiplier") is None:
            current["multiplier"] = multiplier
        option_meta[name] = current


def _signed_fill_delta(fill_size, order_effect=None, is_buy=False, is_sell=False):
    """计算本次成交的 signed 增量；未知效果按开仓处理，便于快照滞后时补腿。"""
    quantity = _finite_number(fill_size, positive=True)
    if quantity is None:
        return 0.0, False
    if order_effect:
        try:
            effect = normalize_option_order_effect(order_effect)
            delta = float(signed_position_delta(effect, quantity))
            return delta, effect in {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}
        except (InvalidOptionOrderEffect, TypeError, ValueError, OverflowError):
            pass
    if is_buy and not is_sell:
        return quantity, False
    if is_sell and not is_buy:
        return -quantity, False
    return 0.0, False


def _expiry_key(value):
    if value is None:
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
        expiry = pd.Timestamp(value)
    except Exception:
        return None
    if getattr(expiry, "tzinfo", None) is not None:
        expiry = expiry.tz_convert("UTC")
    return expiry.normalize()


def _finite_number(value, *, positive=False):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    if positive and parsed <= 0:
        return None
    return parsed


def _positive_multiplier(broker, data):
    if data is None or broker is None:
        return None
    getter = getattr(broker, "get_contract_multiplier", None)
    if not callable(getter):
        return None
    return _finite_number(getter(data), positive=True)


def _safe_current_price(broker, data):
    getter = getattr(broker, "get_current_price", None)
    if not callable(getter):
        return None
    try:
        price = _finite_number(getter(data), positive=False)
    except Exception:
        return None
    if price is None or price < 0:
        return None
    return price


def _premium(value, fallback):
    premium = _finite_number(value)
    if premium is not None:
        premium = abs(premium)
        if math.isfinite(premium):
            return premium
    fallback_value = _finite_number(fallback)
    if fallback_value is None:
        return None
    fallback_value = abs(fallback_value)
    return fallback_value if math.isfinite(fallback_value) else None
