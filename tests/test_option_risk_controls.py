import math
from types import SimpleNamespace

import pandas as pd

from common.indicator_cache import get_cached_indicator_series
from common.options.data_safety import sanitize_market_dataframe
from common.options.risk import OptionRiskLeg, compute_option_margin
from common.options.risk_watchdog import OptionRiskWatchdog
from common.options.settlement import reconcile_settlement_snapshot
from live_trader.adapters.base_broker import BaseLiveBroker


def test_futu_option_risk_snapshot_uses_live_quote_and_account_fact_boundaries():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    option = "US.AAPL260918P320000"

    class Trade:
        def set_sync_query_connect_timeout(self, _timeout):
            pass

        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{"cash": 10_000, "total_assets": 10_000, "initial_margin": "N/A"}])

        def position_list_query(self, **_kwargs):
            return 0, pd.DataFrame([{
                "code": option, "position_market": "US", "position_side": "SHORT",
                "qty": 1, "average_cost": 5.0, "option_contract_multiplier": 100,
            }])

        def order_list_query(self, **_kwargs):
            return 0, pd.DataFrame()

    class Quote:
        def get_market_snapshot(self, codes):
            rows = []
            for code in codes:
                if code == option:
                    rows.append({
                        "code": code, "bid_price": 4.9, "ask_price": 5.1,
                        "last_price": 5.0, "option_gamma": 0.02,
                        "option_contract_multiplier": 100,
                        "option_implied_volatility": 0.3,
                        "update_time": "2026-09-09 10:00:00",
                    })
                elif code == "US.AAPL":
                    rows.append({"code": code, "last_price": 300.0})
            return 0, pd.DataFrame(rows)

    context = SimpleNamespace(
        futu_trade_context=Trade(), futu_quote_context=Quote(),
        _futu_runtime_config={"FUTU_ACCOUNT_CURRENCY": "USD"},
    )
    broker = FutuBrokerAdapter(context)
    broker.set_datas([SimpleNamespace(_name=option), SimpleNamespace(_name="US.AAPL")])
    snapshot = broker.get_option_risk_snapshot()
    broker.close()
    assert snapshot["supported"] is True
    assert snapshot["trusted"] is False
    assert "margin" in snapshot["error"]


def test_portfolio_margin_expands_for_volatility_and_price_shock():
    base = OptionRiskLeg("P", "AAPL", "PUT", -1, 100, 2, 100, 100)
    stressed = OptionRiskLeg(
        "P", "AAPL", "PUT", -1, 100, 2, 100, 100,
        historical_volatility=1.5,
        volatility_shock=0.8,
        price_shock=0.5,
    )
    static = compute_option_margin([base], cash=100_000)
    dynamic = compute_option_margin(
        [stressed], cash=100_000, portfolio_margin=True,
        volatility_shock=0.8, price_shock=0.5,
    )
    assert dynamic.margin_used > static.margin_used
    assert math.isfinite(dynamic.margin_used)


def test_portfolio_margin_rejects_missing_stress_facts():
    leg = OptionRiskLeg("P", "AAPL", "PUT", -1, 100, 2, 100, 100)
    import pytest

    with pytest.raises(ValueError, match="historical_volatility"):
        compute_option_margin([leg], cash=100_000, portfolio_margin=True, stress_down=0.0)


def test_portfolio_margin_accepts_explicit_global_price_shock_without_hv():
    leg = OptionRiskLeg("P", "AAPL", "PUT", -1, 100, 2, 100, 100)
    snapshot = compute_option_margin(
        [leg], cash=100_000, portfolio_margin=True, price_shock=0.5,
    )
    assert snapshot.margin_used > 0


def test_protected_put_spread_margin_is_defined_risk():
    short = OptionRiskLeg("short", "AAPL", "PUT", -1, 100, 5, 100, 100, expiry="2026-09-18", price_shock=0.2)
    long = OptionRiskLeg("long", "AAPL", "PUT", 1, 90, 2, 100, 100, expiry="2026-09-18", price_shock=0.2)
    snapshot = compute_option_margin(
        [short, long], cash=1_000, portfolio_margin=True,
    )
    assert snapshot.margin_used <= 700
    assert snapshot.max_loss_estimate == 700


def test_protected_put_spread_normalizes_expiry_representations():
    short = OptionRiskLeg(
        "short", "AAPL", "PUT", -1, 100, 5, 100, 100,
        expiry="20260918", price_shock=0.2,
    )
    long = OptionRiskLeg(
        "long", "AAPL", "PUT", 1, 90, 2, 100, 100,
        expiry=pd.Timestamp("2026-09-18"), price_shock=0.2,
    )
    snapshot = compute_option_margin([short, long], cash=1_000, portfolio_margin=True)
    assert snapshot.margin_used == snapshot.max_loss_estimate == 700


def test_protected_put_spread_normalizes_underlying_representations():
    short = OptionRiskLeg(
        "short", "US.AAPL", "PUT", -1, 100, 5, 100, 100,
        expiry="2026-09-18", price_shock=0.2,
    )
    long = OptionRiskLeg(
        "long", "AAPL", "PUT", 1, 90, 2, 100, 100,
        expiry="2026-09-18", price_shock=0.2,
    )
    snapshot = compute_option_margin([short, long], cash=1_000, portfolio_margin=True)
    assert snapshot.margin_used == snapshot.max_loss_estimate == 700


def test_protected_put_spread_rejects_unparseable_expiry_as_hedge_fact():
    short = OptionRiskLeg(
        "short", "AAPL", "PUT", -1, 100, 5, 100, 100,
        expiry="not-a-date", price_shock=0.2,
    )
    long = OptionRiskLeg(
        "long", "AAPL", "PUT", 1, 90, 2, 100, 100,
        expiry="not-a-date", price_shock=0.2,
    )
    snapshot = compute_option_margin([short, long], cash=100_000, portfolio_margin=True)
    assert snapshot.max_loss_estimate > 700


def test_spread_matching_requires_same_expiry_and_multiplier():
    short = OptionRiskLeg("short", "AAPL", "PUT", -1, 100, 5, 100, 100, expiry="2026-10-16", price_shock=0.2)
    long = OptionRiskLeg("long", "AAPL", "PUT", 1, 90, 2, 100, 10, expiry="2026-10-16", price_shock=0.2)
    snapshot = compute_option_margin([short, long], cash=100_000, portfolio_margin=True)
    assert snapshot.max_loss_estimate > 700


def test_one_protective_leg_cannot_secure_multiple_short_puts():
    short_a = OptionRiskLeg("short-a", "AAPL", "PUT", -1, 100, 5, 100, 100, price_shock=0.2)
    short_b = OptionRiskLeg("short-b", "AAPL", "PUT", -1, 95, 4, 100, 100, price_shock=0.2)
    hedge = OptionRiskLeg("hedge", "AAPL", "PUT", 1, 90, 2, 100, 100, price_shock=0.2)
    snapshot = compute_option_margin(
        [short_a, short_b, hedge], cash=100_000, portfolio_margin=True,
    )
    # 未配对的第二个 Short Put 仍按现金担保义务计量。
    assert snapshot.margin_used >= 9_500


def test_data_safety_drops_invalid_ohlcv_rows():
    frame = pd.DataFrame(
        {"open": [1.0, float("inf")], "high": [1.0, 1.0],
         "low": [1.0, 1.0], "close": [1.0, 1.0], "volume": [1, 1]},
        index=pd.date_range("2026-01-01", periods=2),
    )
    clean = sanitize_market_dataframe(frame)
    assert len(clean) == 1
    assert clean.iloc[0]["close"] == 1.0
    assert sanitize_market_dataframe(pd.DataFrame({"close": [1.0]}), require_ohlcv=False) is None


def test_indicator_cache_never_returns_infinity():
    class Broker:
        is_live = False
        indicator_cache = {}

    class Strategy:
        broker = Broker()

    data = SimpleNamespace(_name="AAPL", p=SimpleNamespace(
        dataname=pd.DataFrame({"close": [1.0]}, index=pd.date_range("2026-01-01", periods=1))
    ))
    result = get_cached_indicator_series(
        Strategy(), data, "x", (),
        lambda: pd.Series([float("inf"), 2.0], index=pd.date_range("2026-01-01", periods=2)),
    )
    assert result.tolist() == [2.0]
    assert all(math.isfinite(float(value)) for value in result)


def test_watchdog_blocks_untrusted_or_over_limit_snapshot():
    class Broker:
        def __init__(self):
            self.blocked = False
            self.reason = ""

        def set_option_entry_kill_switch(self, blocked, *, reason=""):
            self.blocked = blocked
            self.reason = reason

    broker = Broker()
    watchdog = OptionRiskWatchdog(
        broker, max_gamma=10, max_margin_utilization=0.8, max_spread_pct=0.2
    )
    result = watchdog.evaluate({"portfolio_gamma": 11, "margin_utilization": 0.5})
    assert result.blocked is True
    assert broker.blocked is True
    assert "gamma_limit" in broker.reason
    recovered = watchdog.evaluate({"portfolio_gamma": 0, "margin_utilization": 0.1})
    assert recovered.blocked is False
    assert broker.blocked is False
    watchdog.reset()
    assert broker.blocked is False


def test_watchdog_recovery_clears_only_its_source():
    broker = SimpleNamespace()
    broker._option_entry_blocks = {}
    broker._option_entry_kill_switch = False
    broker._option_entry_kill_reason = None
    BaseLiveBroker.set_option_entry_kill_switch(
        broker, True, source="settlement", reason="unknown clearing state"
    )
    watchdog = OptionRiskWatchdog(broker)

    result = watchdog.evaluate({"portfolio_gamma": 0, "margin_utilization": 0.1})

    assert result.blocked is False
    assert BaseLiveBroker.option_entry_blocked(broker) is True
    assert "settlement" in broker._option_entry_blocks
    assert "watchdog" not in broker._option_entry_blocks


def test_watchdog_blocks_explicitly_unsupported_snapshot():
    class Broker:
        def __init__(self):
            self.blocked = False

        def set_option_entry_kill_switch(self, blocked, *, reason=""):
            self.blocked = blocked

    watchdog = OptionRiskWatchdog(Broker())
    result = watchdog.evaluate({"trusted": True, "supported": False})
    assert result.blocked is True


def test_base_settlement_wrapper_preserves_broker_support_flag():
    broker = type(
        "BrokerStub",
        (),
        {"get_clearing_state": lambda self: {
            "trusted": True, "supported": False, "events": ()
        }},
    )()
    result = BaseLiveBroker.reconcile_option_settlement(broker)
    assert result["supported"] is False


def test_settlement_snapshot_fails_closed_when_untrusted():
    result = reconcile_settlement_snapshot({"trusted": False, "error": "timeout"})
    assert result.trusted is False
    assert result.sizing_reset is True


def test_settlement_reconciliation_preserves_broker_event_details():
    result = reconcile_settlement_snapshot({
        "trusted": True,
        "events": [{"symbol": "AAPL-P", "type": "ASSIGNED_PUT", "quantity": 1, "pnl": -12.5}],
    })
    assert result.events[0]["pnl"] == -12.5
    assert result.assigned_symbols == ("AAPL-P",)


def test_engine_does_not_treat_option_data_feeds_as_existing_option_risk():
    from live_trader.engine import LiveTrader

    class Broker:
        is_live = True
        datas = [SimpleNamespace(_name="US.AAPL260918P320000")]

        def reconcile_option_settlement(self):
            return {"trusted": True, "supported": False, "events": ()}

        def get_option_risk_snapshot(self):
            return {"trusted": True, "supported": True, "has_options": False}

    trader = object.__new__(LiveTrader)
    trader.broker = Broker()
    trader.alarm_manager = None
    trader._processed_settlement_event_ids = set()

    assert trader._reconcile_option_settlement() is True


def test_engine_blocks_option_entries_when_settlement_snapshot_is_untrusted_without_presence_flag():
    from live_trader.engine import LiveTrader

    class Broker:
        is_live = True

        def __init__(self):
            self.blocks = {}

        def reconcile_option_settlement(self):
            return {"trusted": False, "supported": True, "error": "timeout"}

        def set_option_entry_kill_switch(self, blocked, *, reason="", source="manual"):
            if blocked:
                self.blocks[source] = reason
            else:
                self.blocks.pop(source, None)

    broker = Broker()
    trader = object.__new__(LiveTrader)
    trader.broker = broker
    trader.alarm_manager = None
    trader._processed_settlement_event_ids = set()

    assert trader._reconcile_option_settlement() is True
    assert "settlement" in broker.blocks


def test_engine_treats_malformed_settlement_result_as_untrusted():
    from live_trader.engine import LiveTrader

    class Broker:
        is_live = True

        def __init__(self):
            self.blocks = {}

        def reconcile_option_settlement(self):
            return {}

        def set_option_entry_kill_switch(self, blocked, *, reason="", source="manual"):
            if blocked:
                self.blocks[source] = reason
            else:
                self.blocks.pop(source, None)

    broker = Broker()
    trader = object.__new__(LiveTrader)
    trader.broker = broker
    trader.alarm_manager = None
    trader._processed_settlement_event_ids = set()

    assert trader._reconcile_option_settlement() is True
    assert "settlement" in broker.blocks


def test_futu_risk_snapshot_localizes_naive_market_timestamp():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    option = "US.AAPL260918P320000"
    naive_us_now = pd.Timestamp.now(tz="America/New_York").replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    class Trade:
        def set_sync_query_connect_timeout(self, _timeout):
            pass

        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{
                "cash": 10_000, "total_assets": 10_000, "initial_margin": 1_000,
            }])

        def position_list_query(self, **_kwargs):
            return 0, pd.DataFrame([{
                "code": option, "position_market": "US", "position_side": "SHORT",
                "qty": 1, "average_cost": 5.0, "option_contract_multiplier": 100,
            }])

        def order_list_query(self, **_kwargs):
            return 0, pd.DataFrame()

    class Quote:
        def get_market_snapshot(self, codes):
            rows = []
            for code in codes:
                if code == option:
                    rows.append({
                        "code": code, "bid_price": 4.9, "ask_price": 5.1,
                        "last_price": 5.0, "option_gamma": 0.02,
                        "option_contract_multiplier": 100, "update_time": naive_us_now,
                    })
                elif code == "US.AAPL":
                    rows.append({"code": code, "last_price": 300.0, "update_time": naive_us_now})
            return 0, pd.DataFrame(rows)

    broker = FutuBrokerAdapter(SimpleNamespace(
        futu_trade_context=Trade(), futu_quote_context=Quote(),
        _futu_runtime_config={"FUTU_ACCOUNT_CURRENCY": "USD"},
    ))
    snapshot = broker.get_option_risk_snapshot()
    broker.close()

    assert snapshot["trusted"] is True


def test_futu_risk_snapshot_aggregates_duplicate_position_rows_by_contract():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    option = "US.AAPL260918P320000"
    now = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S")

    class Trade:
        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{"cash": 10_000, "total_assets": 10_000, "initial_margin": 1_000}])

        def position_list_query(self, **_kwargs):
            return 0, pd.DataFrame([
                {"code": option, "position_market": "US", "position_side": "SHORT", "qty": 1, "average_cost": 5, "option_contract_multiplier": 100},
                {"code": option, "position_market": "US", "position_side": "SHORT", "qty": 2, "average_cost": 5, "option_contract_multiplier": 100},
            ])

        def order_list_query(self, **_kwargs):
            return 0, pd.DataFrame()

    class Quote:
        def get_market_snapshot(self, codes):
            assert codes.count(option) == 1
            return 0, pd.DataFrame([
                {"code": option, "bid_price": 4.9, "ask_price": 5.1, "last_price": 5,
                 "option_gamma": 0.02, "option_contract_multiplier": 100, "update_time": now},
                {"code": "US.AAPL", "last_price": 300, "update_time": now},
            ])

    broker = FutuBrokerAdapter(SimpleNamespace(
        futu_trade_context=Trade(), futu_quote_context=Quote(),
        _futu_runtime_config={"FUTU_ACCOUNT_CURRENCY": "USD"},
    ))
    broker.set_datas([SimpleNamespace(_name=option), SimpleNamespace(_name="US.AAPL")])
    snapshot = broker.get_option_risk_snapshot()
    broker.close()

    assert snapshot["trusted"] is True
    assert snapshot["portfolio_gamma"] == -0.02 * 3 * 100


def test_futu_risk_snapshot_rejects_duplicate_contract_multiplier_mismatch():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    option = "US.AAPL260918P320000"
    now = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S")

    class Trade:
        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{
                "cash": 10_000, "total_assets": 10_000, "initial_margin": 1_000,
            }])

        def position_list_query(self, **_kwargs):
            return 0, pd.DataFrame([
                {"code": option, "position_market": "US", "position_side": "SHORT",
                 "qty": 1, "average_cost": 5, "option_contract_multiplier": 100},
                {"code": option, "position_market": "US", "position_side": "SHORT",
                 "qty": 1, "average_cost": 5, "option_contract_multiplier": 50},
            ])

        def order_list_query(self, **_kwargs):
            return 0, pd.DataFrame()

    class Quote:
        def get_market_snapshot(self, _codes):
            return 0, pd.DataFrame([
                {"code": option, "bid_price": 4.9, "ask_price": 5.1, "last_price": 5,
                 "option_gamma": 0.02, "option_contract_multiplier": 100,
                 "update_time": now},
                {"code": "US.AAPL", "last_price": 300, "update_time": now},
            ])

    broker = FutuBrokerAdapter(SimpleNamespace(
        futu_trade_context=Trade(), futu_quote_context=Quote(),
        _futu_runtime_config={"FUTU_ACCOUNT_CURRENCY": "USD"},
    ))
    snapshot = broker.get_option_risk_snapshot()
    broker.close()

    assert snapshot["trusted"] is False
    assert "multiplier mismatch" in snapshot["error"]


def test_futu_risk_snapshot_rejects_negative_gamma_fact():
    from live_trader.adapters.futu_broker import FutuBrokerAdapter

    option = "US.AAPL260918P320000"
    now = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S")

    class Trade:
        def accinfo_query(self, **_kwargs):
            return 0, pd.DataFrame([{"cash": 10_000, "total_assets": 10_000, "initial_margin": 1_000}])

        def position_list_query(self, **_kwargs):
            return 0, pd.DataFrame([{
                "code": option, "position_market": "US", "position_side": "SHORT",
                "qty": 1, "average_cost": 5, "option_contract_multiplier": 100,
            }])

        def order_list_query(self, **_kwargs):
            return 0, pd.DataFrame()

    class Quote:
        def get_market_snapshot(self, _codes):
            return 0, pd.DataFrame([
                {"code": option, "bid_price": 4.9, "ask_price": 5.1, "last_price": 5,
                 "option_gamma": -0.02, "option_contract_multiplier": 100, "update_time": now},
                {"code": "US.AAPL", "last_price": 300, "update_time": now},
            ])

    broker = FutuBrokerAdapter(SimpleNamespace(
        futu_trade_context=Trade(), futu_quote_context=Quote(),
        _futu_runtime_config={"FUTU_ACCOUNT_CURRENCY": "USD"},
    ))
    snapshot = broker.get_option_risk_snapshot()
    broker.close()

    assert snapshot["trusted"] is False
