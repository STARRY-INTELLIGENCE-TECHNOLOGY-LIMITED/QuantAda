from types import SimpleNamespace

import pandas as pd
import pytest

from common.options.chain import OPTION_CHAIN_COLUMNS
from common.options.universe import (
    historical_as_of_dates,
    reconcile_live_option_symbols,
    resolve_option_universe_spec,
    select_chain_candidates,
    split_symbol_pool,
)
from data_providers.option_universe import expand_option_universe, supports_historical_option_chain


class DummyStrategy:
    params = {
        "min_dte": 30,
        "max_dte": 45,
        "min_delta": -0.15,
        "max_delta": -0.10,
        "protective_put_delta": -0.03,
    }
    option_universe = ("PUT",)


class StockStrategy:
    params = {"selectTopK": 2}


def _chain_row(symbol, option_type, strike, expiry, delta, as_of="2026-09-04"):
    return {
        "timestamp": pd.Timestamp(as_of, tz="UTC"),
        "underlying": "US.SPY",
        "spot": 500.0,
        "option_symbol": symbol,
        "option_type": option_type,
        "strike": strike,
        "expiry": pd.Timestamp(expiry, tz="UTC"),
        "bid": 1.2,
        "ask": 1.3,
        "last": 1.25,
        "volume": 10,
        "open_interest": 100,
        "iv": 0.2,
        "delta": delta,
        "gamma": 0.01,
        "theta": -0.02,
        "vega": 0.1,
        "rho": -0.01,
        "contract_multiplier": 100.0,
        "currency": "USD",
    }


def _chain(*rows):
    return pd.DataFrame(list(rows), columns=OPTION_CHAIN_COLUMNS)


class DummyManager:
    def __init__(self, chains, historical=True):
        self.chains = chains
        self.calls = []
        self.historical = historical
        self.provider = SimpleNamespace(
            HISTORICAL_OPTION_CHAIN=historical,
            get_option_chain=self.get_option_chain,
        )

    def _split_source_names(self, specified_sources):
        return [str(specified_sources or "theta")]

    def _provider_for_source(self, name):
        return self.provider

    def _explicit_provider_method(self, provider, name):
        return getattr(provider, name, None)

    def _all_provider_instances(self):
        return [self.provider]

    def get_option_chain(self, underlying, specified_sources=None, **kwargs):
        as_of = kwargs.get("as_of")
        key = None if as_of is None else str(pd.Timestamp(as_of).date())
        self.calls.append((str(underlying), key, specified_sources))
        return self.chains.get((str(underlying), key))


def test_split_pool_keeps_underlyings_and_explicit_contracts():
    underlyings, options = split_symbol_pool(
        ["US.SPY", "US.SPY261016P00450000", "US.SPY", ""]
    )
    assert underlyings == ["US.SPY"]
    assert options == ["US.SPY261016P00450000"]


def test_stock_strategy_does_not_opt_in():
    assert resolve_option_universe_spec(StockStrategy) is None
    symbols = ["US.SPY", "US.QQQ"]
    assert expand_option_universe(symbols, strategy_class=StockStrategy) == symbols


def test_select_chain_candidates_filters_dte_delta_and_type():
    chain = _chain(
        _chain_row("US.SPY261016P00450000", "PUT", 450, "2026-10-16", -0.12),
        _chain_row("US.SPY261016C00450000", "CALL", 450, "2026-10-16", 0.55),
        _chain_row("US.SPY261218P00450000", "PUT", 450, "2026-12-18", -0.12),
        _chain_row("US.SPY261016P00300000", "PUT", 300, "2026-10-16", -0.02),
    )
    spec = resolve_option_universe_spec(DummyStrategy)
    selected = select_chain_candidates(chain, as_of="2026-09-04", spec=spec)
    assert selected == ["US.SPY261016P00450000"]


def test_historical_as_of_dates_include_bounds_and_skip_weekend():
    dates = historical_as_of_dates("2026-09-04", "2026-09-18", step_days=7)
    assert pd.Timestamp("2026-09-04") in dates
    assert pd.Timestamp("2026-09-18") in dates
    assert all(day.weekday() < 5 for day in dates)


def test_expand_historical_unions_contracts_and_keeps_underlyings():
    manager = DummyManager({
        ("US.SPY", "2026-09-04"): _chain(
            _chain_row("US.SPY261016P00450000", "PUT", 450, "2026-10-16", -0.12, "2026-09-04"),
        ),
        ("US.SPY", "2026-09-18"): _chain(
            _chain_row("US.SPY261016P00450000", "PUT", 450, "2026-10-16", -0.12, "2026-09-18"),
            _chain_row("US.SPY261030P00440000", "PUT", 440, "2026-10-30", -0.11, "2026-09-18"),
        ),
    })
    result = expand_option_universe(
        ["US.SPY"],
        strategy_class=DummyStrategy,
        data_manager=manager,
        specified_sources="theta",
        start_date="2026-09-04",
        end_date="2026-09-18",
        live=False,
        log=None,
    )
    assert result[0] == "US.SPY"
    assert "US.SPY261016P00450000" in result
    assert "US.SPY261030P00440000" in result
    assert all(call[1] is not None for call in manager.calls)


def test_expand_historical_rejects_current_only_provider():
    manager = DummyManager({}, historical=False)
    with pytest.raises(ValueError, match="theta or theta\\+futu"):
        expand_option_universe(
            ["US.SPY"],
            strategy_class=DummyStrategy,
            data_manager=manager,
            specified_sources="futu",
            start_date="2026-09-04",
            end_date="2026-09-18",
            live=False,
            log=None,
        )
    assert supports_historical_option_chain(manager, "futu") is False


def test_expand_historical_fail_closed_when_chain_missing():
    manager = DummyManager({})
    with pytest.raises(ValueError, match="unavailable"):
        expand_option_universe(
            ["US.SPY"],
            strategy_class=DummyStrategy,
            data_manager=manager,
            specified_sources="theta",
            start_date="2026-09-04",
            end_date="2026-09-18",
            live=False,
            log=None,
        )


def test_live_expand_queries_current_snapshot_without_as_of():
    manager = DummyManager({
        ("US.SPY", None): _chain(
            _chain_row("US.SPY261016P00450000", "PUT", 450, "2026-10-16", -0.12),
        ),
    })
    result = expand_option_universe(
        ["US.SPY"],
        strategy_class=DummyStrategy,
        data_manager=manager,
        specified_sources="theta+futu",
        live=True,
        as_of="2026-09-14",
        log=None,
    )
    assert result == ["US.SPY", "US.SPY261016P00450000"]
    assert manager.calls == [("US.SPY", None, "theta+futu")]


def test_reconcile_live_keeps_held_contract_off_chain():
    target = reconcile_live_option_symbols(
        ["US.SPY"],
        ["US.SPY261016P00450000"],
        held_symbols=["US.SPY260918P00480000"],
        pending_symbols=(),
        pending_trusted=True,
        current_symbols=["US.SPY", "US.SPY260918P00480000"],
    )
    assert target[0] == "US.SPY"
    assert "US.SPY261016P00450000" in target
    assert "US.SPY260918P00480000" in target


def test_reconcile_live_does_not_drop_when_pending_untrusted():
    target = reconcile_live_option_symbols(
        ["US.SPY"],
        ["US.SPY261016P00450000"],
        held_symbols=(),
        pending_symbols=(),
        pending_trusted=False,
        current_symbols=["US.SPY", "US.SPY260918P00480000"],
    )
    assert "US.SPY260918P00480000" in target


def test_explicit_option_symbols_are_kept():
    manager = DummyManager({
        ("US.SPY", "2026-09-04"): _chain(
            _chain_row("US.SPY261016P00450000", "PUT", 450, "2026-10-16", -0.12, "2026-09-04"),
        ),
        ("US.SPY", "2026-09-18"): _chain(
            _chain_row("US.SPY261016P00450000", "PUT", 450, "2026-10-16", -0.12, "2026-09-18"),
        ),
    })
    result = expand_option_universe(
        ["US.SPY", "US.SPY261016P00900000"],
        strategy_class=DummyStrategy,
        data_manager=manager,
        specified_sources="theta",
        start_date="2026-09-04",
        end_date="2026-09-18",
        live=False,
        log=None,
    )
    assert result[0] == "US.SPY"
    assert "US.SPY261016P00900000" in result
    assert "US.SPY261016P00450000" in result


def test_hybrid_chain_routes_live_to_futu_and_backtest_to_theta():
    from data_providers.hybrid_provider import HybridDataProvider
    hybrid = HybridDataProvider.__new__(HybridDataProvider)
    hybrid.live_mode = True
    hybrid.futu_provider = SimpleNamespace(
        get_option_chain_normalized=lambda *args, **kwargs: "FUTU",
    )
    hybrid.theta_provider = SimpleNamespace(
        get_option_chain=lambda *args, **kwargs: "THETA",
    )
    assert hybrid.get_option_chain("US.SPY") == "FUTU"
    hybrid.live_mode = False
    assert hybrid.get_option_chain("US.SPY", as_of="2026-01-02") == "THETA"


def test_sync_live_keeps_option_when_position_query_fails():
    from live_trader.engine import LiveTrader

    trader = object.__new__(LiveTrader)
    trader.strategy_class = DummyStrategy
    trader.config = {
        "params": DummyStrategy.params,
        "timeframe": "Days",
        "compression": 1,
    }
    trader._resolved_symbols = ["US.SPY"]
    spy = SimpleNamespace(_name="US.SPY")
    old = SimpleNamespace(_name="US.SPY260918P00480000")
    new = SimpleNamespace(_name="US.SPY261016P00450000")

    class Broker:
        def __init__(self):
            self.datas = [spy, old]

        def get_position(self, data):
            raise RuntimeError("position timeout")

        def get_pending_orders(self):
            return []

        def set_datas(self, datas):
            self.datas = datas

    trader.broker = Broker()
    trader.broker._last_pending_orders_fetch_failed = False
    trader._expand_option_universe = lambda symbols, live, as_of=None: ["US.SPY", new._name]
    trader._fetch_all_history_data = lambda symbols, context, **kwargs: {
        symbol: SimpleNamespace(_name=symbol) for symbol in symbols
    }

    held, pending, trusted, unknown = trader._option_position_and_pending_symbols()
    assert "US.SPY260918P00480000" in unknown
    assert "US.SPY260918P00480000" not in held
    assert pending == set()
    assert trusted is True

    trader._sync_live_option_universe(SimpleNamespace(now=pd.Timestamp("2026-09-14")))
    names = [getattr(data, "_name", "") for data in trader.broker.datas]
    assert "US.SPY260918P00480000" in names
    assert "US.SPY261016P00450000" in names
    assert names[0] == "US.SPY"


def test_classify_unknown_option_refresh_failure_does_not_skip_run():
    from live_trader.engine import LiveTrader

    trader = object.__new__(LiveTrader)
    trader.strategy_class = DummyStrategy
    trader.config = {"params": DummyStrategy.params}
    spy = SimpleNamespace(_name="US.SPY")
    option = SimpleNamespace(_name="US.SPY260918P00480000")

    class Broker:
        datas = [spy, option]
        is_live = True

        def get_position(self, data):
            raise RuntimeError("position timeout")

        def get_pending_orders(self):
            return []

    trader.broker = Broker()
    trader.broker._last_pending_orders_fetch_failed = False
    required, optional = trader._classify_live_refresh_failures({
        "failed_feeds": 1,
        "failed_symbols": ["US.SPY260918P00480000"],
    })
    assert required == []
    assert optional == []

def test_reconcile_live_adds_held_contract_not_in_current():
    target = reconcile_live_option_symbols(
        ["US.SPY"],
        ["US.SPY261016P00450000"],
        held_symbols=["US.SPY260918P00480000"],
        pending_symbols=["US.SPY260925P00470000"],
        pending_trusted=True,
        current_symbols=["US.SPY"],
    )
    assert target[0] == "US.SPY"
    assert "US.SPY261016P00450000" in target
    assert "US.SPY260918P00480000" in target
    assert "US.SPY260925P00470000" in target


def test_sync_live_adds_held_option_not_in_current_datas():
    from live_trader.engine import LiveTrader

    trader = object.__new__(LiveTrader)
    trader.strategy_class = DummyStrategy
    trader.config = {
        "params": DummyStrategy.params,
        "timeframe": "Days",
        "compression": 1,
    }
    trader._resolved_symbols = ["US.SPY"]
    spy = SimpleNamespace(_name="US.SPY")
    held = SimpleNamespace(_name="US.SPY260918P00480000")
    discovered = SimpleNamespace(_name="US.SPY261016P00450000")

    class Broker:
        def __init__(self):
            self.datas = [spy]

        def get_position(self, data):
            return SimpleNamespace(size=0)

        def get_pending_orders(self):
            return []

        def list_held_option_symbols(self):
            return [held._name]

        def set_datas(self, datas):
            self.datas = datas

    trader.broker = Broker()
    trader.broker._last_pending_orders_fetch_failed = False
    trader._expand_option_universe = lambda symbols, live, as_of=None: ["US.SPY", discovered._name]
    trader._fetch_all_history_data = lambda symbols, context, **kwargs: {
        symbol: SimpleNamespace(_name=symbol) for symbol in symbols
    }
    trader._sync_live_option_universe(SimpleNamespace(now=pd.Timestamp("2026-09-14")))
    names = [getattr(data, "_name", "") for data in trader.broker.datas]
    assert names[0] == "US.SPY"
    assert held._name in names
    assert discovered._name in names

