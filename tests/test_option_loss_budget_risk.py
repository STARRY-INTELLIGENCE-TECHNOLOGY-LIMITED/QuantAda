from common.options.loss_budget import OptionLegSnapshot, assess_symbol_loss
from risk_controls.sample_option_loss_budget import SampleOptionLossBudget


def _leg(symbol, option_type, strike, signed_quantity, entry, mark, expiry="20240119", multiplier=100.0):
    return OptionLegSnapshot(
        symbol=symbol,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        underlying="AAPL",
        signed_quantity=signed_quantity,
        entry_premium=entry,
        mark_premium=mark,
        multiplier=multiplier,
    )


def test_long_option_loss_uses_premium_not_price_direction():
    decision = assess_symbol_loss(
        "US.AAPL240119P00100000",
        [_leg("US.AAPL240119P00100000", "PUT", 100, 1, 4, 1.5)],
        max_loss_pct=0.5,
    )

    assert decision.breached is True
    assert decision.defined_max_loss == 400
    assert decision.loss == 250


def test_short_put_price_drop_is_profit():
    decision = assess_symbol_loss(
        "US.AAPL240119P00100000",
        [_leg("US.AAPL240119P00100000", "PUT", 100, -1, 4, 1)],
        max_loss_pct=0.5,
    )

    assert decision.breached is False
    assert decision.loss == 0


def test_credit_spread_budget_is_width_minus_credit():
    short = _leg("US.AAPL240119P00100000", "PUT", 100, -1, 5, 12)
    long = _leg("US.AAPL240119P00090000", "PUT", 90, 1, 2, 3)
    decision = assess_symbol_loss("US.AAPL240119P00100000", [short, long], max_loss_pct=0.5)

    assert decision.defined_max_loss == 700
    assert decision.loss == 600
    assert decision.breached is True
    assert set(decision.symbols) == {short.symbol, long.symbol}


def test_naked_call_percent_does_not_pretend_premium_is_max_loss():
    naked = _leg("US.AAPL240119C00110000", "CALL", 110, -1, 3, 10)
    percent_only = assess_symbol_loss(naked.symbol, [naked], max_loss_pct=0.5)
    cash_capped = assess_symbol_loss(naked.symbol, [naked], max_loss_pct=0.5, max_loss_cash=500)

    assert percent_only.breached is False
    assert percent_only.reason == "undefined_risk"
    assert cash_capped.breached is True
    assert cash_capped.budget == 500


def test_incomplete_hedge_ignores_percent_and_uses_cash_cap():
    short = _leg("US.AAPL240119P00100000", "PUT", 100, -2, 5, 8)
    long = _leg("US.AAPL240119P00090000", "PUT", 90, 1, 2, 2.5)
    ignored = assess_symbol_loss(short.symbol, [short, long], max_loss_pct=0.01)
    capped = assess_symbol_loss(short.symbol, [short, long], max_loss_pct=0.01, max_loss_cash=400)

    assert ignored.breached is False
    assert ignored.reason == "incomplete_hedge"
    assert capped.breached is True
    assert set(capped.symbols) == {short.symbol, long.symbol}


class _Line:
    def __init__(self, value):
        self.value = value

    def __getitem__(self, index):
        if index != 0:
            raise IndexError(index)
        return self.value


class _Data:
    def __init__(self, name, close):
        self._name = name
        self.close = _Line(close)


class _Position:
    def __init__(self, size, price):
        self.size = size
        self.price = price


class _Broker:
    def __init__(self, positions, multipliers, datas, shares=None):
        self.positions = positions
        self.multipliers = multipliers
        self.datas = datas
        self.shares = shares or {}
        self.logs = []

    def getposition(self, data):
        if data._name in self.shares:
            return _Position(self.shares[data._name], 100)
        return self.positions.get(data._name, _Position(0, 0))

    def get_contract_multiplier(self, data):
        return self.multipliers.get(data._name, 0.0)

    def log(self, message):
        self.logs.append(message)


def _risk(positions, marks, multipliers=None, params=None, extra=None):
    datas = [_Data(symbol, marks[symbol]) for symbol in marks]
    datas.extend(extra or [])
    broker = _Broker(positions, multipliers or {symbol: 100.0 for symbol in marks}, datas)
    return SampleOptionLossBudget(broker, params=params), datas


def test_sample_exits_long_put_and_ignores_stock_feed():
    short_name = "US.AAPL240119P00100000"
    positions = {short_name: _Position(1, 4)}
    risk, datas = _risk(positions, {short_name: 1.5, "US.AAPL": 180})

    assert risk.check(datas[1]) is None
    assert risk.check(datas[0]) == "SELL"


def test_sample_does_not_exit_profitable_short_put():
    name = "US.AAPL240119P00100000"
    risk, datas = _risk({name: _Position(-1, 4)}, {name: 1.0})

    assert risk.check(datas[0]) is None


def test_sample_exits_short_put_on_cash_budget():
    name = "US.AAPL240119P00100000"
    risk, datas = _risk(
        {name: _Position(-1, 4)},
        {name: 20},
        params={"max_loss_pct": 0, "max_loss_cash": 1000},
    )

    assert risk.check(datas[0]) == "SELL"


def test_sample_exits_both_credit_spread_legs():
    short_name = "US.AAPL240119P00100000"
    long_name = "US.AAPL240119P00090000"
    risk, datas = _risk(
        {short_name: _Position(-1, 5), long_name: _Position(1, 2)},
        {short_name: 12, long_name: 3},
    )

    assert risk.check(datas[0]) == "SELL"
    assert risk.check(datas[1]) == "SELL"


def test_sample_keeps_hedge_when_structure_is_inside_budget():
    short_name = "US.AAPL240119P00100000"
    long_name = "US.AAPL240119P00090000"
    risk, datas = _risk(
        {short_name: _Position(-1, 5), long_name: _Position(1, 2)},
        {short_name: 6, long_name: 0.2},
    )

    assert risk.check(datas[1]) is None
    assert risk.check(datas[0]) is None


def test_sample_skips_when_multiplier_is_missing():
    name = "US.AAPL240119P00100000"
    risk, datas = _risk({name: _Position(1, 4)}, {name: 1}, multipliers={name: 0.0})

    assert risk.check(datas[0]) is None


def test_sample_does_not_stop_covered_call_on_option_mark():
    call = "US.AAPL240119C00110000"
    stock = _Data("US.AAPL", 180)
    risk, datas = _risk(
        {call: _Position(-1, 3)},
        {call: 12},
        params={"max_loss_cash": 100},
        extra=[stock],
    )
    risk.broker.shares = {"US.AAPL": 100}

    assert risk.check(datas[0]) is None


def test_sample_skips_when_protective_leg_cannot_be_priced():
    short_name = "US.AAPL240119P00100000"
    long_name = "US.AAPL240119P00090000"
    risk, datas = _risk(
        {short_name: _Position(-1, 5), long_name: _Position(1, 0)},
        {short_name: 20, long_name: 3},
        params={"max_loss_cash": 100},
    )

    assert risk.check(datas[0]) is None
