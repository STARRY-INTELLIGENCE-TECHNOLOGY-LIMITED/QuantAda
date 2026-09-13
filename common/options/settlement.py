"""基于券商清算事实的期权指派对账工具。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SettlementReconciliation:
    """一次清算快照对账结果。"""

    trusted: bool
    assigned_symbols: tuple
    position_adjustments: tuple
    sizing_reset: bool
    error: str = ""
    events: tuple = ()


def reconcile_settlement_snapshot(snapshot):
    """校验清算快照并提取指派事件，不把缺失快照解释为空仓。

    ``snapshot`` 由 Broker 适配器生成；这里只接受明确的 ``trusted`` 标记和
    事件列表，策略层不保存或重放任何历史交易意图。
    """
    if not isinstance(snapshot, dict):
        return SettlementReconciliation(False, (), (), True, "invalid settlement snapshot")
    if snapshot.get("trusted") is not True:
        return SettlementReconciliation(
            False, (), (), True, str(snapshot.get("error") or "untrusted settlement snapshot")
        )
    events = tuple(snapshot.get("events") or ())
    assigned = []
    adjustments = []
    normalized_events = []
    seen_event_ids = set()
    for event in events:
        if not isinstance(event, dict):
            return SettlementReconciliation(False, (), (), True, "invalid settlement event")
        symbol = str(event.get("symbol") or "").strip()
        if not symbol:
            return SettlementReconciliation(False, (), (), True, "settlement event missing symbol")
        event_type = str(event.get("type") or event.get("event") or "").upper()
        if not event_type:
            return SettlementReconciliation(False, (), (), True, "settlement event missing type")
        quantity = event.get("quantity", event.get("shares", 0))
        try:
            quantity_value = float(quantity)
            if not math.isfinite(quantity_value):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return SettlementReconciliation(False, (), (), True, "settlement event quantity is invalid")
        if ("ASSIGN" in event_type or "EXERCI" in event_type) and quantity_value <= 0:
            return SettlementReconciliation(False, (), (), True, "settlement assignment quantity must be positive")
        normalized = dict(event)
        explicit_id = event.get('event_id', event.get('id', event.get('order_id')))
        if explicit_id not in (None, ""):
            event_id = str(explicit_id)
        else:
            # 无事件 ID 的 OpenD 记录用稳定字段组成当前事件身份，保证重复
            # 回调不会再次触发策略清算与仓位 sizing 重置。
            event_id = "|".join(
                str(value)
                for value in (symbol, event_type, quantity_value,
                              event.get("settlement_price", ""), event.get("pnl", ""))
            )
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        if "ASSIGN" in event_type or "EXERCI" in event_type:
            assigned.append(symbol)
        adjustments.append((symbol, event_type, quantity_value))
        normalized.update({"symbol": symbol, "type": event_type, "quantity": quantity_value, "event_id": event_id})
        normalized_events.append(normalized)
    return SettlementReconciliation(
        True,
        tuple(assigned),
        tuple(adjustments),
        bool(assigned),
        "",
        tuple(normalized_events),
    )


__all__ = ["SettlementReconciliation", "reconcile_settlement_snapshot"]
