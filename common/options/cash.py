"""Cash-Secured Put 的无状态资金义务计算工具。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


class CSPCashError(ValueError):
    """Cash-Secured Put 资金约束不满足。"""


def _decimal(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise CSPCashError(f"invalid {name}: {value!r}") from None
    if not result.is_finite():
        raise CSPCashError(f"invalid {name}: {value!r}")
    return result


def assignment_cash(strike, contracts, contract_multiplier=100):
    """返回完整指派义务金额，不扣除权利金。"""
    strike_value = _decimal(strike, "strike")
    quantity = _decimal(contracts, "contracts")
    multiplier = _decimal(contract_multiplier, "contract_multiplier")
    if strike_value <= 0 or quantity < 0 or multiplier <= 0:
        raise CSPCashError("strike, contracts or contract_multiplier is invalid")
    return strike_value * quantity * multiplier


def aggregate_assignment_cash(short_puts=(), pending_short_puts=()):
    """汇总真实短 Put 与可信 pending 卖开 Put 的指派义务。"""
    total = Decimal("0")
    for item in tuple(short_puts or ()) + tuple(pending_short_puts or ()):
        total += assignment_cash(
            item["strike"],
            abs(item["contracts"] if "contracts" in item else item["remaining"]),
            item.get("contract_multiplier", 100),
        )
    return total


def uncommitted_cash(settled_cash, short_puts=(), pending_short_puts=()):
    """按账户已结算现金减去全部指派义务计算可用 CSP 现金。"""
    cash = _decimal(settled_cash, "settled_cash")
    if cash < 0:
        raise CSPCashError("settled_cash must not be negative")
    return cash - aggregate_assignment_cash(short_puts, pending_short_puts)


def assert_csp_capacity(
    settled_cash,
    short_puts,
    pending_short_puts,
    new_strike,
    new_contracts,
    new_contract_multiplier=100,
):
    """检查新增 Short Put 是否满足严格全额担保约束。"""
    available = uncommitted_cash(
        settled_cash,
        short_puts=short_puts,
        pending_short_puts=pending_short_puts,
    )
    required = assignment_cash(
        new_strike,
        new_contracts,
        new_contract_multiplier,
    )
    if available < required:
        raise CSPCashError(
            f"CSP assignment cash insufficient: available={available}, required={required}"
        )
    return available - required


__all__ = [
    "CSPCashError",
    "assignment_cash",
    "aggregate_assignment_cash",
    "uncommitted_cash",
    "assert_csp_capacity",
]
