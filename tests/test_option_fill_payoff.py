from types import SimpleNamespace

from live_trader.option_fill_payoff import collect_option_fill_payoff_summary


class _Broker:
    def __init__(self, positions, prices=None, multipliers=None):
        self._positions = positions
        self._prices = prices or {}
        self._multipliers = multipliers or {}
        self.datas = [SimpleNamespace(_name=name) for name in positions]

    def get_position(self, data):
        position = self._positions[data._name]
        if isinstance(position, Exception):
            raise position
        return position

    def get_current_price(self, data):
        return self._prices[data._name]

    def get_contract_multiplier(self, data):
        return self._multipliers[data._name]


def _pos(size, price=0.0):
    return SimpleNamespace(size=size, price=price)


def test_stock_fill_has_no_payoff_summary():
    broker = _Broker({"US.MARA": _pos(100, 17.5)}, {"US.MARA": 17.5}, {"US.MARA": 1.0})
    assert collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA",
        fill_price=17.5,
        fill_size=10,
        is_buy=True,
    ) == ""


def test_combo_fill_without_second_leg_omits_payoff_summary():
    broker = _Broker(
        {"US.MARA261016P9000": _pos(-1, 0.55)},
        {"US.MARA261016P9000": 0.55},
        {"US.MARA261016P9000": 100.0},
    )
    assert collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA261016P9000",
        fill_price=0.55,
        fill_size=1,
        order_effect="SELL_TO_OPEN",
        is_sell=True,
        is_combo=True,
        fill_data=SimpleNamespace(_name="US.MARA261016P9000"),
    ) == ""


def test_combo_fill_overlays_open_legs_when_snapshot_lags():
    short = SimpleNamespace(_name="US.MARA261016P9000")
    long = SimpleNamespace(_name="US.MARA261016P8000")
    broker = _Broker(
        {
            "US.MARA": _pos(0, 0.0),
            "US.MARA261016P9000": _pos(0, 0.0),
            "US.MARA261016P8000": _pos(0, 0.0),
        },
        {"US.MARA": 17.5, "US.MARA261016P9000": 0.55, "US.MARA261016P8000": 0.20},
        {"US.MARA": 1.0, "US.MARA261016P9000": 100.0, "US.MARA261016P8000": 100.0},
    )
    text = collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA261016P9000",
        fill_price=0.35,
        fill_size=1,
        is_combo=True,
        fill_data=short,
        combo_legs=[
            {"data": short, "symbol": short._name, "effect": "SELL_TO_OPEN", "volume": 1, "price": 0.55},
            {"data": long, "symbol": long._name, "effect": "BUY_TO_OPEN", "volume": 1, "price": 0.20},
        ],
    )
    assert "现货参考价：17.50" in text
    assert "最大盈利：35.00" in text
    assert "最大亏损：65.00" in text


def test_csp_open_overlays_lagging_empty_snapshot():
    broker = _Broker(
        {
            "US.MARA": _pos(0, 0.0),
            "US.MARA261016P9000": _pos(0, 0.0),
        },
        {"US.MARA": 17.5, "US.MARA261016P9000": 0.55},
        {"US.MARA": 1.0, "US.MARA261016P9000": 100.0},
    )
    text = collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA261016P9000",
        fill_price=0.55,
        fill_size=1,
        order_effect="SELL_TO_OPEN",
        is_sell=True,
        fill_data=SimpleNamespace(_name="US.MARA261016P9000"),
    )
    assert "现货参考价：17.50" in text
    assert "最大盈利：55.00" in text
    assert "最大亏损：845.00" in text
    assert "盈亏平衡点：8.45" in text


def test_close_to_flat_omits_payoff_summary():
    broker = _Broker(
        {
            "US.MARA": _pos(0, 0.0),
            "US.MARA261016P9000": _pos(0, 0.0),
        },
        {"US.MARA": 17.5, "US.MARA261016P9000": 0.55},
        {"US.MARA": 1.0, "US.MARA261016P9000": 100.0},
    )
    assert collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA261016P9000",
        fill_price=0.55,
        fill_size=1,
        order_effect="BUY_TO_CLOSE",
        is_buy=True,
        fill_data=SimpleNamespace(_name="US.MARA261016P9000"),
    ) == ""


def test_put_credit_spread_uses_same_expiry_legs_only():
    broker = _Broker(
        {
            "US.MARA": _pos(0, 0.0),
            "US.MARA261016P9000": _pos(-1, 0.55),
            "US.MARA261016P8000": _pos(1, 0.20),
            "US.MARA261023P9000": _pos(-1, 0.60),
        },
        {
            "US.MARA": 17.5,
            "US.MARA261016P9000": 0.55,
            "US.MARA261016P8000": 0.20,
            "US.MARA261023P9000": 0.60,
        },
        {
            "US.MARA": 1.0,
            "US.MARA261016P9000": 100.0,
            "US.MARA261016P8000": 100.0,
            "US.MARA261023P9000": 100.0,
        },
    )
    text = collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA261016P9000",
        fill_price=0.55,
        fill_size=1,
        order_effect="SELL_TO_OPEN",
        is_sell=True,
        fill_data=SimpleNamespace(_name="US.MARA261016P9000"),
    )
    # 宽 1 美元的 Put Credit Spread：最大亏损 (1 - 0.35) * 100 = 65，不含另一到期日。
    assert "最大亏损：65.00" in text
    assert "最大盈利：35.00" in text


def test_covered_call_includes_underlying_leg():
    broker = _Broker(
        {
            "US.MARA": _pos(100, 17.0),
            "US.MARA261016C20000": _pos(-1, 0.40),
        },
        {"US.MARA": 17.5, "US.MARA261016C20000": 0.40},
        {"US.MARA": 1.0, "US.MARA261016C20000": 100.0},
    )
    text = collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA261016C20000",
        fill_price=0.40,
        fill_size=1,
        order_effect="SELL_TO_OPEN",
        is_sell=True,
        fill_data=SimpleNamespace(_name="US.MARA261016C20000"),
    )
    assert "最大亏损：无限" not in text
    assert "最大盈利：" in text
    assert "现货参考价：17.50" in text


def test_missing_multiplier_omits_summary():
    broker = _Broker(
        {
            "US.MARA": _pos(0, 0.0),
            "US.MARA261016P9000": _pos(-1, 0.55),
        },
        {"US.MARA": 17.5, "US.MARA261016P9000": 0.55},
        {"US.MARA": 1.0, "US.MARA261016P9000": 0.0},
    )
    assert collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA261016P9000",
        fill_price=0.55,
        fill_size=1,
        order_effect="SELL_TO_OPEN",
        is_sell=True,
        fill_data=SimpleNamespace(_name="US.MARA261016P9000"),
    ) == ""


def test_position_query_failure_omits_summary():
    broker = _Broker(
        {
            "US.MARA261016P9000": RuntimeError("position snapshot failed"),
        },
        {"US.MARA261016P9000": 0.55},
        {"US.MARA261016P9000": 100.0},
    )
    assert collect_option_fill_payoff_summary(
        broker,
        fill_symbol="US.MARA261016P9000",
        fill_price=0.55,
        fill_size=1,
        order_effect="SELL_TO_OPEN",
        is_sell=True,
        fill_data=SimpleNamespace(_name="US.MARA261016P9000"),
    ) == ""
