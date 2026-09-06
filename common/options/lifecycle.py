"""确定性的期权到期、结算和最小换月计算。

该模块只服务回测/研究，不进入实盘 Broker。它使用输入的 signed position、
现货、现金和合约元数据计算结果，不保存跨 K 的交易意图。
"""

from __future__ import annotations

from dataclasses import dataclass


class ExpiredOptionError(ValueError):
    """尝试在合约到期后交易。"""


class InvalidOptionLifecycle(ValueError):
    """期权生命周期输入不合法。"""


@dataclass(frozen=True)
class OptionContract:
    """最小期权合约描述。"""

    symbol: str
    option_type: str
    strike: float
    expiry: object
    contract_multiplier: float
    settlement: str = "physical"
    currency: str = ""


@dataclass(frozen=True)
class OptionExpiryResult:
    """一次到期或提前平仓处理的确定性结果。"""

    symbol: str
    event: str
    option_position_before: float
    option_position_after: float
    underlying_delta: float
    cash_delta: float
    cash_after: float
    underlying_after: float
    intrinsic_value: float
    contract_multiplier: float
    expired: bool = True


@dataclass(frozen=True)
class OptionRollResult:
    """一次旧合约结算并建立新合约的结果。"""

    expiry: OptionExpiryResult
    new_symbol: str
    new_option_position: float
    new_cash: float
    opening_cash_delta: float


def _number(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise InvalidOptionLifecycle(f"invalid {name}: {value!r}") from None
    if result != result or result in (float("inf"), float("-inf")):
        raise InvalidOptionLifecycle(f"invalid {name}: {value!r}")
    return result


def _timestamp(value):
    try:
        import pandas as pd

        result = pd.Timestamp(value)
        if pd.isna(result):
            raise ValueError
        return result
    except Exception:
        raise InvalidOptionLifecycle(f"invalid timestamp: {value!r}") from None


def _day(value):
    """将日期统一为 UTC 日界，避免时区混用导致比较异常。"""
    timestamp = _timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.normalize()


def _normalise_type(value):
    text = str(value or "").strip().upper()
    if text in {"C", "CALL"}:
        return "CALL"
    if text in {"P", "PUT"}:
        return "PUT"
    raise InvalidOptionLifecycle(f"unsupported option type: {value!r}")


def assert_option_tradeable(contract: OptionContract, at) -> None:
    """到期日之后禁止继续交易；到期日当天仍可由调用方决定是否退出。"""
    if _day(at) > _day(contract.expiry):
        raise ExpiredOptionError(
            f"option {contract.symbol} expired at {contract.expiry}"
        )


def settle_option_expiry(
    contract: OptionContract,
    signed_quantity,
    spot,
    *,
    cash=0.0,
    underlying_position=0.0,
    dividend_per_share=0.0,
    dividend_events=None,
    dividend_entitled=False,
    at=None,
) -> OptionExpiryResult:
    """结算到期合约，支持 OTM 归零、ITM 实物或现金结算。

    期权结算本身不会自动产生股息。若回测需要在同一账本内结算标的股息，
    调用方必须传入已按除息日和持有资格筛选的 ``dividend_events``。旧的
    ``dividend_per_share`` 参数仅在显式设置 ``dividend_entitled=True`` 时生效，
    用于兼容单次、已确认资格的股息数据，避免重复或提前记账。
    """
    quantity = _number(signed_quantity, "signed_quantity")
    spot_value = _number(spot, "spot")
    strike = _number(contract.strike, "strike")
    multiplier = _number(contract.contract_multiplier, "contract_multiplier")
    cash_before = _number(cash, "cash")
    underlying_before = _number(underlying_position, "underlying_position")
    if strike <= 0 or multiplier <= 0 or quantity == 0 or spot_value < 0:
        raise InvalidOptionLifecycle("invalid expiry position, spot, strike or multiplier")
    expiry = _timestamp(contract.expiry)
    if at is not None and _day(at) < _day(expiry):
        raise InvalidOptionLifecycle("cannot settle an option before expiry")

    option_type = _normalise_type(contract.option_type)
    settlement = str(contract.settlement or "physical").strip().lower()
    if settlement not in {"physical", "cash"}:
        raise InvalidOptionLifecycle(f"unsupported settlement: {contract.settlement!r}")
    intrinsic = max(spot_value - strike, 0.0) if option_type == "CALL" else max(strike - spot_value, 0.0)

    # 股息属于标的账本现金流，不是期权到期结算现金流。只有调用方明确
    # 提供除息日已发生且尚未入账的事件时，才将其加入本次结果。
    dividend_cash = 0.0
    if dividend_events is not None:
        if not isinstance(dividend_events, (list, tuple)):
            raise InvalidOptionLifecycle("dividend_events must be a list or tuple")
        expiry_day = _day(expiry)
        for event in dividend_events:
            if isinstance(event, dict):
                ex_date = event.get("ex_date", event.get("date"))
                per_share = event.get("per_share", event.get("amount"))
            elif isinstance(event, (list, tuple)) and len(event) == 2:
                ex_date, per_share = event
            else:
                raise InvalidOptionLifecycle(f"invalid dividend event: {event!r}")
            event_day = _day(ex_date)
            if event_day > expiry_day:
                continue
            dividend_cash += underlying_before * _number(per_share, "dividend_per_share")
    elif _number(dividend_per_share, "dividend_per_share") != 0:
        if not dividend_entitled:
            raise InvalidOptionLifecycle(
                "dividend_per_share requires explicit dividend_entitled=True"
            )
        dividend_cash = underlying_before * _number(
            dividend_per_share, "dividend_per_share"
        )

    if intrinsic <= 0:
        return OptionExpiryResult(
            symbol=contract.symbol,
            event="EXPIRED_OTM",
            option_position_before=quantity,
            option_position_after=0.0,
            underlying_delta=0.0,
            cash_delta=dividend_cash,
            cash_after=cash_before + dividend_cash,
            underlying_after=underlying_before,
            intrinsic_value=0.0,
            contract_multiplier=multiplier,
        )

    if settlement == "cash":
        cash_delta = quantity * intrinsic * multiplier
        underlying_delta = 0.0
        event = "EXERCISED_CASH" if quantity > 0 else "ASSIGNED_CASH"
    elif option_type == "CALL":
        underlying_delta = quantity * multiplier
        cash_delta = -quantity * strike * multiplier
        event = "EXERCISED_CALL" if quantity > 0 else "ASSIGNED_CALL"
    else:
        underlying_delta = -quantity * multiplier
        cash_delta = quantity * strike * multiplier
        event = "EXERCISED_PUT" if quantity > 0 else "ASSIGNED_PUT"

    cash_delta += dividend_cash
    return OptionExpiryResult(
        symbol=contract.symbol,
        event=event,
        option_position_before=quantity,
        option_position_after=0.0,
        underlying_delta=underlying_delta,
        cash_delta=cash_delta,
        cash_after=cash_before + cash_delta,
        underlying_after=underlying_before + underlying_delta,
        intrinsic_value=intrinsic,
        contract_multiplier=multiplier,
    )


def roll_option_position(
    old_contract: OptionContract,
    new_contract: OptionContract,
    signed_quantity,
    old_spot,
    new_price,
    *,
    cash=0.0,
    underlying_position=0.0,
    dividend_per_share=0.0,
    dividend_events=None,
    dividend_entitled=False,
    old_price=None,
    at=None,
) -> OptionRollResult:
    """关闭旧合约并按新合约价格建立相同 signed position。

    到期前换月是普通的买平/卖平加新开仓，不应伪装成到期结算；因此必须
    提供旧合约的实际平仓价格 ``old_price``。到期日或到期后才使用到期结算。
    """
    quantity = _number(signed_quantity, "signed_quantity")
    new_price_value = _number(new_price, "new_price")
    new_multiplier = _number(new_contract.contract_multiplier, "contract_multiplier")
    if new_price_value <= 0 or quantity == 0:
        raise InvalidOptionLifecycle("invalid roll quantity or new option price")
    at_timestamp = _timestamp(at) if at is not None else _timestamp(old_contract.expiry)
    old_expiry = _timestamp(old_contract.expiry)
    if _day(at_timestamp) < _day(old_expiry):
        if old_price is None:
            raise InvalidOptionLifecycle(
                "pre-expiry roll requires old option close price"
            )
        close_price = _number(old_price, "old_price")
        if close_price < 0:
            raise InvalidOptionLifecycle("old_price must be non-negative")
        old_multiplier = _number(
            old_contract.contract_multiplier, "contract_multiplier"
        )
        close_cash_delta = quantity * close_price * old_multiplier
        expiry_result = OptionExpiryResult(
            symbol=old_contract.symbol,
            event="ROLLED_PRE_EXPIRY",
            option_position_before=quantity,
            option_position_after=0.0,
            underlying_delta=0.0,
            cash_delta=close_cash_delta,
            cash_after=_number(cash, "cash") + close_cash_delta,
            underlying_after=_number(underlying_position, "underlying_position"),
            intrinsic_value=0.0,
            contract_multiplier=old_multiplier,
            expired=False,
        )
    else:
        expiry_result = settle_option_expiry(
            old_contract,
            quantity,
            old_spot,
            cash=cash,
            underlying_position=underlying_position,
            dividend_per_share=dividend_per_share,
            dividend_events=dividend_events,
            dividend_entitled=dividend_entitled,
            at=at,
        )
    # 新合约必须在换月时仍可交易，避免把换月结果写入已过期合约。
    if _day(at_timestamp) >= _day(new_contract.expiry):
        raise ExpiredOptionError(
            f"option {new_contract.symbol} is expired at {at_timestamp}"
        )
    opening_cash_delta = -quantity * new_price_value * new_multiplier
    return OptionRollResult(
        expiry=expiry_result,
        new_symbol=new_contract.symbol,
        new_option_position=quantity,
        new_cash=expiry_result.cash_after + opening_cash_delta,
        opening_cash_delta=opening_cash_delta,
    )


__all__ = [
    "ExpiredOptionError",
    "InvalidOptionLifecycle",
    "OptionContract",
    "OptionExpiryResult",
    "OptionRollResult",
    "assert_option_tradeable",
    "settle_option_expiry",
    "roll_option_position",
]
