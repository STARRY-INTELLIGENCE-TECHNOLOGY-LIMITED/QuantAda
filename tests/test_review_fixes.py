from types import SimpleNamespace

import pandas as pd

from common.options.data_safety import sanitize_market_dataframe
from common.options.risk import OptionRiskLeg, compute_option_margin
from common.options.risk_watchdog import OptionRiskWatchdog
from common.options.settlement import reconcile_settlement_snapshot
from live_trader.adapters.base_broker import BaseLiveBroker


def test_optional_volume_nan_does_not_drop_ohlc_row():
    frame = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [float("nan")]},
        index=pd.date_range("2026-01-01", periods=1),
    )
    assert sanitize_market_dataframe(frame, require_ohlcv=False) is not None


def test_none_expiry_is_not_a_protective_pair():
    short = OptionRiskLeg("short", "AAPL", "PUT", -1, 100, 5, 100, 100, expiry=None, price_shock=0.2)
    long = OptionRiskLeg("long", "AAPL", "PUT", 1, 90, 2, 100, 100, expiry=None, price_shock=0.2)
    snapshot = compute_option_margin([short, long], cash=100_000, portfolio_margin=True)
    assert snapshot.max_loss_estimate > 700


def test_watchdog_reset_does_not_clear_other_block_source():
    broker = SimpleNamespace()
    broker._option_entry_blocks = {}
    broker._option_entry_kill_switch = False
    broker._option_entry_kill_reason = None
    BaseLiveBroker.set_option_entry_kill_switch(broker, True, source="settlement", reason="unknown")
    watchdog = OptionRiskWatchdog(broker, interval_seconds=1)
    watchdog.reset()
    assert BaseLiveBroker.option_entry_blocked(broker) is True


def test_futu_combo_callback_preserves_submitted_legs():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter, FutuComboOrderProxy

    broker = object.__new__(FutuBrokerAdapter)
    broker.is_live = True
    broker.datas = [SimpleNamespace(_name="US.AAPL260918P320000")]
    broker._combo_order_legs = {
        "combo-1": [{"symbol": "US.AAPL260918P320000", "data": broker.datas[0], "effect": "SELL_TO_OPEN"}]
    }
    broker._contract_multiplier = lambda _data: 100
    proxy = broker.convert_order_proxy({"order_id": "combo-1", "code": "US.AAPL260918P320000", "order_status": "SUBMITTED"})
    assert isinstance(proxy, FutuComboOrderProxy)
    assert proxy.combo_legs[0]["effect"] == "SELL_TO_OPEN"
    assert proxy.is_buy() is False
    assert proxy.is_sell() is False
    raw_proxy = broker.convert_order_proxy({
        "order_id": "combo-2", "code": "US.AAPL260918P320000",
        "order_type": "COMBO", "order_status": "SUBMITTED",
    })
    assert isinstance(raw_proxy, FutuComboOrderProxy)


def test_futu_combo_proxy_aggregates_leg_fills_when_order_total_is_missing():
    from live_trader.adapters.futu_broker import FutuComboOrderProxy

    proxy = FutuComboOrderProxy(
        {
            "order_id": "combo-filled",
            "order_status": "FILLED_ALL",
            "dealt_qty": 0,
        },
        legs=[
            {"symbol": "US.AAPL260918P320000", "dealt_qty": 2, "dealt_avg_price": 5.0},
            {"symbol": "US.AAPL260918P300000", "dealt_qty": 2, "dealt_avg_price": 2.0},
        ],
    )

    assert proxy.executed.size == 2
    assert proxy.executed.value == 14.0


def test_futu_combined_option_positions_are_aggregated():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    symbol = "US.AAPL260918P320000"
    broker = object.__new__(FutuBrokerAdapter)
    broker._query_position_rows = lambda _code: [
        {
            "code": symbol,
            "position_market": "US",
            "position_type": "COMBINED",
            "combo_id": "combo-1",
            "qty": 1,
            "can_sell_qty": 1,
            "average_cost": 5,
            "option_contract_multiplier": 100,
        }
    ]
    result = broker.get_position(SimpleNamespace(_name=symbol))
    assert result.size == 1
    assert result.combo_id == "combo-1"


def test_futu_option_position_without_side_or_sellable_fails_closed():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    symbol = "US.AAPL260918P320000"
    broker = object.__new__(FutuBrokerAdapter)
    broker._query_position_rows = lambda _code: [{
        "code": symbol,
        "position_market": "US",
        "qty": 1,
        "average_cost": 5,
        "option_contract_multiplier": 100,
    }]

    import pytest

    with pytest.raises(RuntimeError, match="unknown position side"):
        broker.get_position(SimpleNamespace(_name=symbol))


def test_futu_combined_non_option_position_is_not_silently_dropped():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    broker = object.__new__(FutuBrokerAdapter)
    broker._query_position_rows = lambda _code: [{
        "code": "US.AAPL", "position_market": "US", "position_type": "COMBINED",
        "combo_id": "combo-stock", "qty": 2, "can_sell_qty": 2, "average_cost": 100,
    }]
    result = broker.get_position(SimpleNamespace(_name="US.AAPL"))
    assert result.size == 2
    assert result.combo_id == "combo-stock"


def test_futu_pending_combo_open_uses_saved_leg_effect_after_reservation_rotation():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    short_symbol = "US.AAPL260918P320000"
    broker = object.__new__(FutuBrokerAdapter)
    broker.is_live = True
    broker._option_run_reservations = {}
    broker._combo_order_legs = {
        "combo-open": [
            {"symbol": short_symbol, "effect": "SELL_TO_OPEN"},
            {"symbol": "US.AAPL260918P300000", "effect": "BUY_TO_OPEN"},
        ]
    }
    broker._query_all_position_rows = lambda: []
    broker._query_order_rows = lambda: [{
        "order_id": "combo-open",
        "code": short_symbol,
        "trd_side": "SELL",
        "order_status": "SUBMITTED",
        "qty": 1,
        "dealt_qty": 0,
    }]
    broker._option_multiplier_for_symbol = lambda _symbol, row=None: 100

    obligations = broker.get_option_assignment_obligations()

    assert obligations["assignment_cash"] == 32000
    assert obligations["pending_orders"][0]["remaining"] == 1


def test_futu_pending_combo_orders_expand_into_each_leg_for_expected_position():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    broker = object.__new__(FutuBrokerAdapter)
    broker.is_live = True
    broker.datas = []
    broker._combo_order_legs = {
        "combo-1": [
            {"symbol": "US.AAPL260918P320000", "effect": "SELL_TO_OPEN"},
            {"symbol": "US.AAPL260918P300000", "effect": "BUY_TO_OPEN"},
        ]
    }
    broker._query_order_rows = lambda: [{
        "order_id": "combo-1",
        "code": "US.AAPL260918P320000",
        "trd_side": "SELL",
        "order_status": "SUBMITTED",
        "qty": 1,
        "dealt_qty": 0,
    }]

    pending = broker.get_pending_orders()

    assert [(item["symbol"], item["direction"]) for item in pending] == [
        ("US.AAPL260918P320000", "SELL"),
        ("US.AAPL260918P300000", "BUY"),
    ]


def test_futu_empty_option_snapshot_requires_account_fact():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    broker = object.__new__(FutuBrokerAdapter)
    broker._query_all_position_rows = lambda: []
    broker._query_account_info = lambda: (_ for _ in ()).throw(RuntimeError("account timeout"))

    snapshot = broker.get_option_risk_snapshot()

    assert snapshot["trusted"] is False
    assert "account snapshot unavailable" in snapshot["error"]


def test_portfolio_margin_never_exceeds_defined_put_spread_loss():
    from common.options.risk import OptionRiskLeg, compute_option_margin

    short = OptionRiskLeg(
        "short", "AAPL", "PUT", -1, 100, 5, 100, 100,
        expiry="2026-09-18", historical_volatility=2.0,
        volatility_shock=1.0, price_shock=0.2,
    )
    long = OptionRiskLeg(
        "long", "AAPL", "PUT", 1, 90, 2, 100, 100,
        expiry="2026-09-18", historical_volatility=2.0,
        volatility_shock=1.0, price_shock=0.2,
    )

    snapshot = compute_option_margin([short, long], cash=100_000, portfolio_margin=True)

    assert snapshot.margin_used == snapshot.max_loss_estimate == 700


def test_common_option_parser_handles_futu_compact_strike():
    from common.options.analytics import parse_option_symbol

    parsed = parse_option_symbol("US.MARA261016P9000")
    assert parsed["strike"] == 9.0


def test_backtest_uncommitted_cash_matches_dotted_underlying_key():
    from backtest.backtester import BacktraderStrategyWrapper

    index = pd.date_range("2026-01-01", periods=1)
    underlying = SimpleNamespace(
        _name="US.BRK.B",
        p=SimpleNamespace(dataname=pd.DataFrame(
            {"open": [300.0], "high": [300.0], "low": [300.0], "close": [300.0], "volume": [1]},
            index=index,
        )),
        datetime=SimpleNamespace(datetime=lambda _offset: index[0]),
    )
    option = SimpleNamespace(
        _name="US.BRK.B260201P300000",
        p=SimpleNamespace(dataname=pd.DataFrame(
            {
                "open": [5.0], "high": [5.0], "low": [5.0], "close": [5.0], "volume": [1],
                "strike": [300.0], "expiry": ["2026-02-01"], "last": [5.0],
                "contract_multiplier": [100.0], "historical_volatility": [0.2],
            },
            index=index,
        )),
        datetime=SimpleNamespace(datetime=lambda _offset: index[0]),
    )
    wrapper = object.__new__(BacktraderStrategyWrapper)
    wrapper.datas = [underlying, option]
    wrapper._option_cash_reservations = {}
    wrapper._last_order_target_skip_reason = None
    wrapper.get_current_price = lambda data: 300.0 if data is underlying else 5.0
    wrapper.getposition = lambda data: SimpleNamespace(size=0 if data is underlying else -1, price=5.0)
    wrapper.get_contract_multiplier = lambda _data: 100.0
    wrapper.getcash = lambda: 100_000.0

    available = wrapper.get_option_uncommitted_cash()

    assert available > 0
    assert wrapper._last_order_target_skip_reason is None


def test_backtest_risk_leg_matches_unparseable_datafeed_metadata():
    from backtest.backtester import _validate_backtest_risk_leg

    data = SimpleNamespace(
        _name="AAPL_OPTION",
        p=SimpleNamespace(dataname=pd.DataFrame({
            "option_contract_size": [100.0],
            "option_type": ["PUT"],
            "strike": [100.0],
            "expiry": ["2026-09-18"],
        })),
    )
    risk_leg = OptionRiskLeg(
        "AAPL_OPTION", "AAPL", "PUT", -1, 90, 2, 100, 100,
        expiry="2026-09-18",
    )

    import pytest

    with pytest.raises(ValueError, match="DataFeed metadata"):
        _validate_backtest_risk_leg(data, risk_leg)


def test_backtest_option_close_spread_rejects_cross_underlying_legs():
    from backtest.backtester import BacktraderStrategyWrapper

    index = pd.date_range("2026-01-01", periods=1)

    def make_data(symbol):
        return SimpleNamespace(
            _name=symbol,
            close=[1.0],
            p=SimpleNamespace(dataname=pd.DataFrame(
                {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                 "volume": [1], "contract_multiplier": [100.0]},
                index=index,
            )),
        )

    short = make_data("US.AAPL260918P320000")
    hedge = make_data("US.MSFT260918P300000")
    wrapper = object.__new__(BacktraderStrategyWrapper)
    wrapper.datas = [short, hedge]
    wrapper._option_cash_reservations = {}
    wrapper._last_order_target_skip_reason = None
    wrapper.slippage = 0.0
    wrapper.getposition = lambda data: SimpleNamespace(size=-1 if data is short else 1)
    wrapper.get_contract_multiplier = lambda _data: 100.0
    wrapper.get_option_uncommitted_cash = lambda: 100_000.0
    wrapper.broker = SimpleNamespace(
        getcommissioninfo=lambda _data: SimpleNamespace(p=SimpleNamespace(commission=0.0))
    )

    result = wrapper.submit_option_spread([
        {"data": short, "volume": 1, "effect": "BUY_TO_CLOSE", "price": 1.0},
        {"data": hedge, "volume": 1, "effect": "SELL_TO_CLOSE", "price": 1.0},
    ])

    assert result is None
    assert wrapper._last_order_target_skip_reason == "invalid_option_spread"


def test_backtest_option_close_spread_accepts_valid_put_legs():
    from backtest.backtester import BacktraderStrategyWrapper

    index = pd.date_range("2026-01-01", periods=1)

    def make_data(symbol):
        return SimpleNamespace(
            _name=symbol,
            close=[1.0],
            p=SimpleNamespace(dataname=pd.DataFrame(
                {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                 "volume": [1], "contract_multiplier": [100.0]},
                index=index,
            )),
        )

    short = make_data("US.AAPL260918P320000")
    hedge = make_data("US.AAPL260918P300000")
    wrapper = object.__new__(BacktraderStrategyWrapper)
    wrapper.datas = [short, hedge]
    wrapper._option_cash_reservations = {}
    wrapper._last_order_target_skip_reason = None
    wrapper.slippage = 0.0
    wrapper.getposition = lambda data: SimpleNamespace(size=-1 if data is short else 1)
    wrapper.get_contract_multiplier = lambda _data: 100.0
    wrapper.get_option_uncommitted_cash = lambda: 100_000.0
    wrapper.submit_option_order = lambda data, quantity, effect, **kwargs: SimpleNamespace(
        ref=f"{data._name}:{effect}:{quantity}"
    )

    result = wrapper.submit_option_spread([
        {"data": short, "volume": 1, "effect": "BUY_TO_CLOSE", "price": 1.0},
        {"data": hedge, "volume": 1, "effect": "SELL_TO_CLOSE", "price": 1.0},
    ])

    assert result is not None
    assert len(result) == 2


def test_command_center_accepts_external_strategy_root(tmp_path):
    package = tmp_path / "private_strategies"
    package.mkdir()
    source = package / "demo_strategy.py"
    source.write_text("class DemoStrategy:\n    params = {'window': 5}\n", encoding="utf-8")
    from command_center.web import CommandCenterService

    service = CommandCenterService()
    analyzed = service.analyze_training({"source_path": str(source), "source_root": str(tmp_path)})
    assert analyzed["strategy"] == "private_strategies.demo_strategy"
    assert analyzed["ranges"][0]["name"] == "window"


def test_settlement_snapshot_deduplicates_repeated_events():
    result = reconcile_settlement_snapshot({
        "trusted": True,
        "events": [
            {"symbol": "AAPL", "type": "ASSIGNED_PUT", "quantity": 1},
            {"symbol": "AAPL", "type": "ASSIGNED_PUT", "quantity": 1},
        ],
    })
    assert len(result.events) == 1
    assert len(result.position_adjustments) == 1


def test_settlement_snapshot_without_trusted_marker_fails_closed():
    result = reconcile_settlement_snapshot({})
    assert result.trusted is False


def test_futu_combo_callback_uses_matched_data_multiplier():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter, FutuComboOrderProxy

    symbol = "US.AAPL260918P320000"
    data = SimpleNamespace(_name=symbol)
    broker = object.__new__(FutuBrokerAdapter)
    broker.is_live = True
    broker.datas = [data]
    broker._combo_order_legs = {}
    broker._contract_multiplier = lambda _data: 100.0

    proxy = broker.convert_order_proxy({
        "order_id": "combo-filled",
        "code": symbol,
        "order_type": "COMBO",
        "order_status": "FILLED_ALL",
        "dealt_qty": 1,
        "dealt_avg_price": 4,
    })

    assert isinstance(proxy, FutuComboOrderProxy)
    assert proxy.contract_multiplier == 100.0
    assert proxy.executed.value == 400.0


def test_futu_combo_callback_does_not_report_partial_missing_leg_as_filled():
    from live_trader.adapters.futu_broker import FutuComboOrderProxy

    proxy = FutuComboOrderProxy(
        {"order_id": "combo-partial", "order_status": "FILLED_PART"},
        legs=[
            {"dealt_qty": 1, "dealt_avg_price": 4},
            {"dealt_qty": 0, "dealt_avg_price": 2},
        ],
        contract_multiplier=100,
    )

    assert proxy.executed.size == 0


def test_futu_option_lot_size_cannot_be_used_as_contract_multiplier():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    option = "US.AAPL260918P050000"
    broker = object.__new__(FutuBrokerAdapter)
    broker.is_live = True
    broker._contract_multipliers = {}
    broker.datas = []
    broker._query_all_position_rows = lambda: [{
        "code": option,
        "position_market": "US",
        "position_side": "SHORT",
        "qty": 1,
        "average_cost": 5,
        "lot_size": 1,
    }]
    broker._query_order_rows = lambda: []

    import pytest

    with pytest.raises(RuntimeError, match="multiplier unavailable"):
        broker.get_option_assignment_obligations()


def test_futu_combo_order_proxy_uses_leg_multiplier_for_aggregate_fill():
    from live_trader.adapters.futu_broker import FutuComboOrderProxy

    proxy = FutuComboOrderProxy(
        {
            "order_id": "combo-net",
            "code": "US.AAPL260918P320000",
            "order_status": "FILLED_ALL",
            "dealt_qty": 1,
            "dealt_avg_price": 4,
        },
        legs=[{
            "symbol": "US.AAPL260918P320000",
            "effect": "SELL_TO_OPEN",
            "risk_leg": OptionRiskLeg(
                "US.AAPL260918P320000", "US.AAPL", "PUT", -1,
                320, 5, 300, 100,
            ),
        }],
    )

    assert proxy.executed.value == 400


def test_futu_combo_order_proxy_scales_leg_fill_value_by_multiplier():
    from live_trader.adapters.futu_broker import FutuComboOrderProxy

    proxy = FutuComboOrderProxy(
        {
            "order_id": "combo-leg-value",
            "code": "US.AAPL260918P320000",
            "order_status": "FILLED_ALL",
            "dealt_qty": 0,
        },
        legs=[{
            "symbol": "US.AAPL260918P320000",
            "dealt_qty": 1,
            "dealt_avg_price": 4,
            "risk_leg": OptionRiskLeg(
                "US.AAPL260918P320000", "US.AAPL", "PUT", -1,
                320, 5, 300, 100,
            ),
        }],
    )

    assert proxy.executed.value == 400


def test_futu_short_position_valuation_fails_closed_when_fx_is_missing():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    broker = object.__new__(FutuBrokerAdapter)
    broker.is_live = True
    broker._market_currency = lambda _symbol: "HKD"
    broker._account_currency_name = lambda: "USD"
    broker._get_fx_rate = lambda *_args: None
    broker._contract_multiplier = lambda _data: 100.0

    import pytest

    with pytest.raises(RuntimeError, match="position valuation unavailable"):
        broker.get_position_market_value(SimpleNamespace(_name="HK.00700"), -1, price=10.0)


def test_portfolio_margin_rejects_volatility_shock_without_historical_volatility():
    import pytest

    leg = OptionRiskLeg("p", "AAPL", "PUT", -1, 100, 2, 100, 100)
    with pytest.raises(ValueError, match="historical_volatility"):
        compute_option_margin(
            [leg], cash=100_000, portfolio_margin=True, volatility_shock=0.8
        )


def test_portfolio_stress_counts_underlying_once_for_multiple_legs():
    first = OptionRiskLeg("p1", "AAPL", "PUT", 1, 50, 2, 100, 100)
    second = OptionRiskLeg("p2", "AAPL", "PUT", 1, 50, 2, 100, 100)
    one = compute_option_margin(
        [first], cash=1_000_000, underlying_positions={"AAPL": 100}
    )
    two = compute_option_margin(
        [first, second], cash=1_000_000, underlying_positions={"AAPL": 100}
    )
    assert one.stress_loss_down == 2_000
    assert two.stress_loss_down == 2_000


def test_portfolio_stress_includes_underlying_with_protected_spread():
    short = OptionRiskLeg(
        "short", "AAPL", "PUT", -1, 100, 5, 100, 100,
        expiry="2026-09-18", price_shock=0.2,
    )
    hedge = OptionRiskLeg(
        "hedge", "AAPL", "PUT", 1, 90, 2, 100, 100,
        expiry="2026-09-18", price_shock=0.2,
    )
    snapshot = compute_option_margin(
        [short, hedge], cash=1_000_000,
        underlying_positions={"AAPL": 100}, portfolio_margin=True,
    )
    assert snapshot.stress_loss_down == 2_700


def test_futu_provider_rejects_mismatched_current_snapshot_row():
    from data_providers.futu_provider import FutuDataProvider

    class Quote:
        def get_market_snapshot(self, _codes):
            return 0, pd.DataFrame([{
                "code": "US.MSFT",
                "update_time": "2026-09-12 10:00:00",
                "open_price": 200,
                "high_price": 201,
                "low_price": 199,
                "last_price": 200,
                "volume": 1,
            }])

    assert FutuDataProvider(quote_ctx=Quote())._current_snapshot_kline("US.AAPL") is None


def test_futu_provider_accepts_current_snapshot_without_volume():
    from data_providers.futu_provider import FutuDataProvider

    class Quote:
        def get_market_snapshot(self, _codes):
            return 0, pd.DataFrame([{
                "code": "US.AAPL",
                "update_time": "2026-09-12 10:00:00",
                "open_price": 200,
                "high_price": 201,
                "low_price": 199,
                "last_price": 200,
            }])

    result = FutuDataProvider(quote_ctx=Quote())._current_snapshot_kline("US.AAPL")
    assert result is not None
    assert result.iloc[0]["volume"] == 0.0


def test_backtest_option_cash_fails_closed_before_first_visible_row():
    from backtest.backtester import BacktraderStrategyWrapper

    data = SimpleNamespace(
        _name="US.AAPL260918P320000",
        p=SimpleNamespace(dataname=pd.DataFrame(
            {"open": [5], "high": [5], "low": [5], "close": [5], "volume": [1],
             "option_type": ["PUT"], "strike": [320], "contract_multiplier": [100]},
            index=pd.date_range("2026-01-02", periods=1),
        )),
        datetime=SimpleNamespace(datetime=lambda _offset: pd.Timestamp("2026-01-01")),
    )
    wrapper = object.__new__(BacktraderStrategyWrapper)
    wrapper.datas = [data]
    wrapper._option_cash_reservations = {}
    wrapper.getposition = lambda _data: SimpleNamespace(size=-1)
    wrapper.getcash = lambda: 100_000
    wrapper.get_contract_multiplier = lambda _data: 100

    assert wrapper.get_option_uncommitted_cash() == 0.0
    assert wrapper._last_order_target_skip_reason == "option_margin_unavailable"
