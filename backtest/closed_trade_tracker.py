import math


class ClosedTradeTracker:
    """
    跟踪已完成的 Backtrader 往返交易，用于归因诊断。
    """

    def __init__(self, owner):
        self.owner = owner
        self.closed_trades = []
        self._active_trade_states = {}

    def update_active_lows(self):
        for data in getattr(self.owner, "datas", []) or []:
            symbol = getattr(data, "_name", None)
            state = self._active_trade_states.get(symbol)
            if not state:
                continue

            try:
                if self.owner.getposition(data).size <= 0:
                    continue
                low_price = float(data.low[0])
            except Exception:
                continue

            if not math.isfinite(low_price):
                continue
            current_low = state.get("lowest_price_during_trade")
            state["lowest_price_during_trade"] = (
                low_price
                if current_low is None
                else min(current_low, low_price)
            )

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

        if order.isbuy() and size > 0:
            self._track_buy(symbol, price, abs(size))
        elif order.issell():
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
        if state:
            entry_price = state.get("entry_price")
            entry_size = state.get("entry_size")
            exit_price = state.get("last_exit_price")
            lowest_price = state.get("lowest_price_during_trade")
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

        self.closed_trades.append({
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "lowest_price_during_trade": lowest_price,
        })

    def _track_buy(self, symbol, price, size):
        state = self._active_trade_states.get(symbol)
        if not state:
            self._active_trade_states[symbol] = {
                "symbol": symbol,
                "entry_price": price,
                "entry_size": size,
                "lowest_price_during_trade": price,
                "last_exit_price": None,
            }
            return

        old_size = float(state.get("entry_size") or 0.0)
        new_size = old_size + size
        if new_size > 0:
            state["entry_price"] = (
                (float(state.get("entry_price") or 0.0) * old_size) + (price * size)
            ) / new_size
            state["entry_size"] = new_size
        current_low = state.get("lowest_price_during_trade")
        state["lowest_price_during_trade"] = price if current_low is None else min(current_low, price)
