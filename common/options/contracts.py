"""通用期权订单效果的纯计算与校验工具。

本模块不查询账户、不保存仓位，也不调用任何券商 SDK。它只把期权订单效果
转换为买卖方向，并根据已确认的 signed position 计算成交后的仓位变化。
卖开是否可以提交由 Broker 依据保证金能力另外决定。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


OPTION_ORDER_EFFECTS = frozenset(
    {
        "BUY_TO_OPEN",
        "SELL_TO_CLOSE",
        "SELL_TO_OPEN",
        "BUY_TO_CLOSE",
    }
)

_EFFECT_SIDE = {
    "BUY_TO_OPEN": "BUY",
    "SELL_TO_CLOSE": "SELL",
    "SELL_TO_OPEN": "SELL",
    "BUY_TO_CLOSE": "BUY",
}


class InvalidOptionOrderEffect(ValueError):
    """期权订单效果或其仓位边界不合法。"""


def normalize_option_order_effect(effect) -> str:
    """返回严格的大写订单效果；未知效果直接拒绝。"""
    value = str(effect or "").strip().upper().replace("-", "_")
    if value not in OPTION_ORDER_EFFECTS:
        raise InvalidOptionOrderEffect(
            f"unsupported option order effect: {effect!r}"
        )
    return value


def order_effect_side(effect) -> str:
    """返回订单效果对应的券商买卖方向。"""
    return _EFFECT_SIDE[normalize_option_order_effect(effect)]


def _decimal(value, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise InvalidOptionOrderEffect(
            f"invalid {field_name}: {value!r}"
        ) from None
    if not parsed.is_finite():
        raise InvalidOptionOrderEffect(
            f"invalid {field_name}: {value!r}"
        )
    return parsed


def signed_position_delta(effect, filled_size) -> Decimal:
    """计算一个已成交期权订单对 signed position 的增量。"""
    normalized = normalize_option_order_effect(effect)
    quantity = _decimal(filled_size, "filled_size")
    if quantity <= 0:
        raise InvalidOptionOrderEffect(
            f"filled_size must be positive: {filled_size!r}"
        )
    return quantity if normalized in {"BUY_TO_OPEN", "BUY_TO_CLOSE"} else -quantity


def validate_option_order_effect(
    effect,
    current_position,
    quantity,
    *,
    allow_sell_to_open: bool = False,
) -> str:
    """校验效果与已确认仓位的方向关系。

    ``allow_sell_to_open`` 默认关闭，避免在未实现保证金前把信号送到柜台。
    BUY_TO_CLOSE 可以用于已有负仓的平仓，不会创建新的卖方风险。
    """
    normalized = normalize_option_order_effect(effect)
    position = _decimal(current_position, "current_position")
    amount = _decimal(quantity, "quantity")
    if amount <= 0:
        raise InvalidOptionOrderEffect(f"quantity must be positive: {quantity!r}")

    if normalized == "SELL_TO_OPEN":
        if not allow_sell_to_open:
            raise InvalidOptionOrderEffect(
                "SELL_TO_OPEN is disabled until margin support is implemented"
            )
        if position > 0:
            raise InvalidOptionOrderEffect(
                "SELL_TO_OPEN cannot be submitted while a long position exists"
            )
        return normalized

    if normalized == "BUY_TO_OPEN" and position < 0:
        raise InvalidOptionOrderEffect(
            "BUY_TO_OPEN cannot be submitted while a short position exists"
        )
    if normalized == "SELL_TO_CLOSE":
        if position <= 0 or amount > position:
            raise InvalidOptionOrderEffect(
                "SELL_TO_CLOSE exceeds the confirmed long position"
            )
    if normalized == "BUY_TO_CLOSE":
        if position >= 0 or amount > abs(position):
            raise InvalidOptionOrderEffect(
                "BUY_TO_CLOSE exceeds the confirmed short position"
            )
    return normalized


def apply_signed_position(current_position, effect, filled_size):
    """返回成交后的 signed position，不修改传入对象。"""
    position = _decimal(current_position, "current_position")
    return position + signed_position_delta(effect, filled_size)


__all__ = [
    "OPTION_ORDER_EFFECTS",
    "InvalidOptionOrderEffect",
    "normalize_option_order_effect",
    "order_effect_side",
    "signed_position_delta",
    "validate_option_order_effect",
    "apply_signed_position",
]
