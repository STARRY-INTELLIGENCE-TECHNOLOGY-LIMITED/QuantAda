import math


class ClosedTradeTracker:
    """
    跟踪已完成的 Backtrader 往返交易，用于归因诊断。
    """

    def __init__(self, owner):
        self.owner = owner
        self.closed_trades = []
        self._active_trade_states = {}
        self._spread_expected = {}
        self._spread_closed = {}
        self._processed_settlement_event_ids = set()

    def update_active_lows(self):
        for data in getattr(self.owner, "datas", []) or []:
            symbol = getattr(data, "_name", None)
            state = self._active_trade_states.get(symbol)
            if not state:
                continue

            try:
                position_size = float(self.owner.getposition(data).size)
                side = str(state.get("side", "long")).lower()
                if side == "short":
                    if position_size >= 0:
                        continue
                    adverse_price = float(data.high[0])
                    field_name = "highest_price_during_trade"
                else:
                    if position_size <= 0:
                        continue
                    adverse_price = float(data.low[0])
                    field_name = "lowest_price_during_trade"
            except Exception:
                continue

            if not math.isfinite(adverse_price) or adverse_price <= 0:
                continue
            current_value = state.get(field_name)
            if side == "short":
                state[field_name] = (
                    adverse_price
                    if current_value is None
                    else max(current_value, adverse_price)
                )
            else:
                state[field_name] = (
                    adverse_price
                    if current_value is None
                    else min(current_value, adverse_price)
                )

    @staticmethod
    def _order_effect(order):
        """读取显式期权效果；普通股票订单返回空字符串。"""
        info = getattr(order, "info", None)
        effect = getattr(info, "option_order_effect", None)
        if effect is None and isinstance(info, dict):
            effect = info.get("option_order_effect")
        if effect is None:
            effect = getattr(order, "order_effect", None)
        return str(effect or "").strip().upper()

    @staticmethod
    def _order_info_number(order, name):
        """读取订单信息中的有限数值。"""
        info = getattr(order, "info", None)
        value = getattr(info, name, None)
        if value is None and isinstance(info, dict):
            value = info.get(name)
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    @staticmethod
    def _order_info_text(order, name):
        info = getattr(order, "info", None)
        value = getattr(info, name, None)
        if value is None and isinstance(info, dict):
            value = info.get(name)
        text = str(value or "").strip()
        return text or None

    def notify_order(self, order):
        if order.status != order.Completed:
            return

        executed = getattr(order, "executed", None)
        if executed is None:
            return

        try:
            size = float(executed.size)
            price = float(executed.price)
        except (TypeError, ValueError):
            return
        if size == 0 or not math.isfinite(price):
            return

        symbol = getattr(getattr(order, "data", None), "_name", None)
        if not symbol:
            return

        effect = self._order_effect(order)
        spread_id = self._order_info_text(order, "option_spread_id")
        spread_leg = self._order_info_text(order, "option_spread_leg")
        if effect == "SELL_TO_OPEN":
            self._track_sell(
                symbol,
                price,
                abs(size),
                risk_capital=self._order_info_number(order, "option_assignment_cash"),
                contract_multiplier=self._order_info_number(order, "option_contract_multiplier"),
                spread_id=spread_id,
                spread_leg=spread_leg,
            )
        elif effect in {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}:
            state = self._active_trade_states.get(symbol)
            if state:
                state["last_exit_price"] = price
        elif effect == "BUY_TO_OPEN" or (not effect and order.isbuy() and size > 0):
            self._track_buy(
                symbol,
                price,
                abs(size),
                contract_multiplier=self._order_info_number(order, "option_contract_multiplier"),
                spread_id=spread_id,
                spread_leg=spread_leg,
            )
        elif not effect and order.issell():
            state = self._active_trade_states.get(symbol)
            if state:
                state["last_exit_price"] = price

    def notify_trade(self, trade):
        if not getattr(trade, "isclosed", False):
            return

        symbol = getattr(getattr(trade, "data", None), "_name", None) or "UNKNOWN"
        state = self._active_trade_states.pop(symbol, None)

        pnl = getattr(trade, "pnlcomm", None)
        if pnl is None:
            pnl = getattr(trade, "pnl", None)
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            pnl = 0.0

        entry_price = None
        entry_size = None
        exit_price = None
        lowest_price = None
        highest_price = None
        side = "long"
        risk_capital = None
        premium_credit = None
        contract_multiplier = None
        if state:
            entry_price = state.get("entry_price")
            entry_size = state.get("entry_size")
            exit_price = state.get("last_exit_price")
            lowest_price = state.get("lowest_price_during_trade")
            highest_price = state.get("highest_price_during_trade")
            side = str(state.get("side", "long")).lower()
            risk_capital = state.get("risk_capital")
            premium_credit = state.get("premium_credit")
            contract_multiplier = state.get("contract_multiplier")
        else:
            try:
                entry_price = float(getattr(trade, "price", None))
            except (TypeError, ValueError):
                entry_price = None

        pnl_pct = None
        try:
            entry_value = abs(float(entry_price) * float(entry_size))
            if entry_value > 0:
                pnl_pct = pnl / entry_value
        except (TypeError, ValueError):
            pass

        leg_record = {
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "lowest_price_during_trade": lowest_price,
            "highest_price_during_trade": highest_price,
            "side": side,
            "risk_capital": risk_capital,
            "premium_credit": premium_credit,
            "contract_multiplier": contract_multiplier,
        }
        spread_id = state.get("spread_id") if state else None
        if spread_id:
            bucket = self._spread_closed.setdefault(spread_id, {})
            bucket[symbol] = leg_record
            expected = self._spread_expected.get(spread_id, set())
            if expected and expected.issubset(bucket):
                legs = [bucket[item] for item in sorted(expected)]
                short_leg = next(
                    (item for item in legs if item.get("side") == "short"),
                    legs[0],
                )
                combined = dict(short_leg)
                combined["symbol"] = short_leg.get("symbol", symbol)
                combined["pnl"] = sum(float(item.get("pnl") or 0.0) for item in legs)
                combined["premium_credit"] = sum(
                    float(item.get("premium_credit") or 0.0) for item in legs
                )
                combined["spread_id"] = spread_id
                combined["spread_legs"] = legs
                self.closed_trades.append(combined)
                self._spread_closed.pop(spread_id, None)
                self._spread_expected.pop(spread_id, None)
            return
        self.closed_trades.append(leg_record)

    def reconcile_external_settlement(self, events):
        """按已确认的外部结算事实关闭本地归因状态，不生成新交易意图。"""
        for event in events or ():
            if not isinstance(event, dict):
                continue
            symbol = str(event.get("symbol") or "").strip()
            if not symbol:
                continue
            event_id = str(event.get("event_id") or "|".join(
                str(value)
                for value in (
                    symbol,
                    str(event.get("type") or event.get("event") or "").upper(),
                    event.get("quantity", event.get("shares", 0)),
                    event.get("settlement_price", ""),
                    event.get("pnl", ""),
                )
            ))
            if event_id in self._processed_settlement_event_ids:
                continue
            self._processed_settlement_event_ids.add(event_id)
            state = self._active_trade_states.pop(symbol, None)
            if state is None:
                continue
            settlement_price = event.get("settlement_price")
            try:
                settlement_price = float(settlement_price)
                if not math.isfinite(settlement_price):
                    settlement_price = None
            except (TypeError, ValueError, OverflowError):
                settlement_price = None
            try:
                pnl = float(event.get("pnl", 0.0) or 0.0)
                if not math.isfinite(pnl):
                    pnl = 0.0
            except (TypeError, ValueError, OverflowError):
                pnl = 0.0
            pnl_pct = event.get("pnl_pct")
            try:
                pnl_pct = float(pnl_pct) if pnl_pct is not None else None
                if pnl_pct is not None and not math.isfinite(pnl_pct):
                    pnl_pct = None
            except (TypeError, ValueError, OverflowError):
                pnl_pct = None
            self.closed_trades.append({
                "symbol": symbol,
                "entry_price": state.get("entry_price"),
                "exit_price": settlement_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "lowest_price_during_trade": state.get("lowest_price_during_trade"),
                "highest_price_during_trade": state.get("highest_price_during_trade"),
                "side": state.get("side", "long"),
                "risk_capital": state.get("risk_capital"),
                "premium_credit": state.get("premium_credit"),
                "contract_multiplier": state.get("contract_multiplier"),
                "settlement_event": str(event.get("type", "EXTERNAL_SETTLEMENT")),
            })

    def _track_buy(
        self,
        symbol,
        price,
        size,
        contract_multiplier=None,
        spread_id=None,
        spread_leg=None,
    ):
        debit = None
        if contract_multiplier is not None:
            debit = -price * size * contract_multiplier
        state = self._active_trade_states.get(symbol)
        if not state:
            self._active_trade_states[symbol] = {
                "symbol": symbol,
                "side": "long",
                "entry_price": price,
                "entry_size": size,
                "lowest_price_during_trade": price,
                "highest_price_during_trade": None,
                "risk_capital": None,
                "premium_credit": debit,
                "contract_multiplier": contract_multiplier,
                "last_exit_price": None,
                "spread_id": spread_id,
                "spread_leg": spread_leg,
            }
            if spread_id:
                self._spread_expected.setdefault(spread_id, set()).add(symbol)
            return

        old_size = float(state.get("entry_size") or 0.0)
        new_size = old_size + size
        if new_size > 0:
            state["entry_price"] = (
                (float(state.get("entry_price") or 0.0) * old_size) + (price * size)
            ) / new_size
            state["entry_size"] = new_size
        if debit is not None:
            state["premium_credit"] = (state.get("premium_credit") or 0.0) + debit
        if state.get("contract_multiplier") is None:
            state["contract_multiplier"] = contract_multiplier
        current_low = state.get("lowest_price_during_trade")
        state["lowest_price_during_trade"] = price if current_low is None else min(current_low, price)

    def _track_sell(
        self,
        symbol,
        price,
        size,
        risk_capital=None,
        contract_multiplier=None,
        spread_id=None,
        spread_leg=None,
    ):
        """记录显式 SELL_TO_OPEN 空头交易，空头不把买平误记为新多头。"""
        state = self._active_trade_states.get(symbol)
        if not state:
            self._active_trade_states[symbol] = {
                "symbol": symbol,
                "side": "short",
                "entry_price": price,
                "entry_size": size,
                "lowest_price_during_trade": None,
                "highest_price_during_trade": price,
                "risk_capital": risk_capital,
                "premium_credit": price * size * (contract_multiplier or 1.0),
                "contract_multiplier": contract_multiplier,
                "last_exit_price": None,
                "spread_id": spread_id,
                "spread_leg": spread_leg,
            }
            if spread_id:
                self._spread_expected.setdefault(spread_id, set()).add(symbol)
            return

        old_size = float(state.get("entry_size") or 0.0)
        new_size = old_size + size
        if new_size > 0:
            state["entry_price"] = (
                (float(state.get("entry_price") or 0.0) * old_size) + (price * size)
            ) / new_size
            state["entry_size"] = new_size
        if risk_capital is not None:
            state["risk_capital"] = (state.get("risk_capital") or 0.0) + risk_capital
        state["premium_credit"] = (state.get("premium_credit") or 0.0) + (
            price * size * (contract_multiplier or state.get("contract_multiplier") or 1.0)
        )
        if state.get("contract_multiplier") is None:
            state["contract_multiplier"] = contract_multiplier
        current_high = state.get("highest_price_during_trade")
        state["highest_price_during_trade"] = (
            price if current_high is None else max(current_high, price)
        )
