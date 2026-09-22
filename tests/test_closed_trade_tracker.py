from types import SimpleNamespace

from backtest.closed_trade_tracker import ClosedTradeTracker


class _Data:
    _name = "US.SPY260918P00600000"

    def __init__(self):
        self.low = [4.0]
        self.high = [8.0]


class _Owner:
    def __init__(self, data):
        self.datas = [data]
        self.position = SimpleNamespace(size=-1)

    def getposition(self, _data):
        return self.position


def _order(effect, price, is_buy):
    return SimpleNamespace(
        status=1,
        Completed=1,
        executed=SimpleNamespace(size=1.0, price=price),
        data=_Data(),
        info=SimpleNamespace(
            option_order_effect=effect,
            option_assignment_cash=1000.0 if effect == "SELL_TO_OPEN" else None,
            option_contract_multiplier=100.0 if effect == "SELL_TO_OPEN" else None,
        ),
        isbuy=lambda: is_buy,
        issell=lambda: not is_buy,
    )


def test_closed_trade_tracker_records_short_option_adverse_high():
    data = _Data()
    owner = _Owner(data)
    tracker = ClosedTradeTracker(owner)

    opening = _order("SELL_TO_OPEN", 5.0, False)
    opening.data = data
    tracker.notify_order(opening)

    data.high[0] = 8.0
    tracker.update_active_lows()

    closing = _order("BUY_TO_CLOSE", 3.0, True)
    closing.data = data
    tracker.notify_order(closing)
    tracker.notify_trade(SimpleNamespace(
        isclosed=True,
        data=data,
        pnlcomm=20.0,
    ))

    assert tracker.closed_trades[0]["side"] == "short"
    assert tracker.closed_trades[0]["highest_price_during_trade"] == 8.0
    assert tracker.closed_trades[0]["risk_capital"] == 1000.0
    assert tracker.closed_trades[0]["premium_credit"] == 500.0


def test_closed_trade_tracker_ignores_zero_padded_long_low():
    data = _Data()
    data._name = "US.SPY260918C00600000"
    owner = _Owner(data)
    owner.position = SimpleNamespace(size=1)
    tracker = ClosedTradeTracker(owner)
    opening = _order("BUY_TO_OPEN", 5.0, True)
    opening.data = data
    opening.info = SimpleNamespace(
        option_order_effect="BUY_TO_OPEN",
        option_contract_multiplier=100.0,
    )
    tracker.notify_order(opening)
    data.low[0] = 0.0
    tracker.update_active_lows()
    assert tracker._active_trade_states[data._name]["lowest_price_during_trade"] == 5.0
