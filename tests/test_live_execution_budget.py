import math
from types import SimpleNamespace

import pytest
import pandas as pd

import config
import common.live_execution_budget as budget_module
import common.order_executor as executor_module
from common.live_execution_budget import (
    begin_live_run_budget,
    resolve_live_run_budget_seconds,
)
from live_trader.adapters.base_broker import BaseLiveBroker, BaseOrderProxy
from live_trader.engine import LiveTrader


class _OrderProxy(BaseOrderProxy):
    def __init__(self, oid, side, status="Submitted"):
        self._id = oid
        self.side = side
        self.status = status
        self.executed = SimpleNamespace(size=0, price=0, value=0, comm=0)

    @property
    def id(self):
        return self._id

    def is_completed(self):
        return self.status == "Filled"

    def is_canceled(self):
        return self.status == "Canceled"

    def is_rejected(self):
        return self.status == "Rejected"

    def is_pending(self):
        return self.status in {"PendingSubmit", "Submitted", "PendingCancel"}

    def is_accepted(self):
        return self.is_pending()

    def is_buy(self):
        return self.side == "BUY"

    def is_sell(self):
        return self.side == "SELL"


class _BudgetBroker(BaseLiveBroker):
    def __init__(self, clock, cash=1_000_000.0, position=0, submit_seconds=0.0):
        self.clock = clock
        self.real_cash = cash
        self.position = position
        self.submit_seconds = submit_seconds
        self.submitted = []
        self.pending_queries = 0
        super().__init__(context=SimpleNamespace())

    def _fetch_real_cash(self):
        return self.real_cash

    def getvalue(self):
        return self.real_cash

    def get_position(self, data):
        return SimpleNamespace(size=self.position, sellable=self.position, price=10.0)

    def get_current_price(self, data):
        return 10.0

    def get_pending_orders(self):
        self.pending_queries += 1
        return [
            {
                "id": order.id,
                "symbol": order.data._name,
                "direction": order.side,
                "size": order.submitted_size,
            }
            for order in self.submitted
            if order.is_pending()
        ]

    def _submit_order(self, data, volume, side, price):
        self.clock["t"] += self.submit_seconds
        proxy = _OrderProxy(f"ORDER_{len(self.submitted) + 1}", side)
        proxy.data = data
        proxy.submitted_size = volume
        self.submitted.append(proxy)
        return proxy

    def convert_order_proxy(self, raw_order):
        return raw_order

    @staticmethod
    def is_live_mode(context):
        return True


@pytest.fixture
def fake_clock(monkeypatch):
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += max(0.0, float(seconds))

    monkeypatch.setattr(budget_module.time, "monotonic", now)
    monkeypatch.setattr(budget_module.time, "sleep", sleep)
    monkeypatch.setattr(executor_module.time, "time", now)
    return clock


def test_budget_resolution_is_bounded_and_schedule_aware():
    assert resolve_live_run_budget_seconds(
        {"LIVE_RUN_MAX_EXECUTION_SECONDS": 600},
        SimpleNamespace(schedule_rule="1d:14:45:00"),
    ) == 600.0
    assert resolve_live_run_budget_seconds(
        {"LIVE_RUN_MAX_EXECUTION_SECONDS": 600},
        SimpleNamespace(schedule_rule="5m:09:30:00"),
    ) == 240.0
    assert resolve_live_run_budget_seconds(
        {"LIVE_RUN_MAX_EXECUTION_SECONDS": 600},
        SimpleNamespace(schedule_rule="1m:00:00:00"),
    ) == 48.0
    with pytest.raises(ValueError, match="Second-level schedule.*not supported"):
        resolve_live_run_budget_seconds(
            {"LIVE_RUN_MAX_EXECUTION_SECONDS": 600},
            SimpleNamespace(schedule_rule="5s:00:00:00"),
        )
    assert resolve_live_run_budget_seconds(
        {
            "LIVE_RUN_MAX_EXECUTION_SECONDS": 600,
            "timeframe": "Seconds",
            "compression": 1,
        }
    ) == pytest.approx(0.8)
    assert resolve_live_run_budget_seconds(
        {
            "LIVE_RUN_MAX_EXECUTION_SECONDS": 600,
            "timeframe": "Minutes",
            "compression": 5,
        }
    ) == 240.0


@pytest.mark.parametrize("invalid_value", [None, 0, -1, "bad", math.nan, math.inf])
def test_invalid_or_nonpositive_budget_falls_back_safely(invalid_value):
    assert resolve_live_run_budget_seconds(
        {"LIVE_RUN_MAX_EXECUTION_SECONDS": invalid_value}
    ) == 600.0


def test_sell_and_cash_wait_share_deadline_and_preserve_final_buy_window(fake_clock, monkeypatch):
    sell_data = SimpleNamespace(_name="SPY.ARCA")
    buy_data = SimpleNamespace(_name="EWJ.ARCA")

    class Broker:
        is_live = True
        safety_multiplier = 1.0

        def __init__(self):
            self._pending_sells = set()
            self.calls = []
            self.sell_submitted = False

        def get_position(self, data):
            if data._name == sell_data._name:
                size = 100 if not self.sell_submitted or fake_clock["t"] < 8.0 else 0
            else:
                size = 0
            return SimpleNamespace(size=size, price=1.0)

        def get_current_price(self, data):
            return 1.0

        def get_rebalance_cash(self):
            return 0.0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target), fake_clock["t"]))
            if data._name == sell_data._name:
                self.sell_submitted = True
                self._pending_sells.add("SELL_1")
                return SimpleNamespace(id="SELL_1", submitted_size=100)
            return object()

        def get_pending_orders(self):
            if fake_clock["t"] < 8.0:
                return [{"id": "SELL_1", "symbol": sell_data._name, "direction": "SELL", "size": 100}]
            return []

        def sync_balance(self):
            return None

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", lambda *args, **kwargs: True)
    broker = Broker()
    begin_live_run_budget(broker, {"LIVE_RUN_MAX_EXECUTION_SECONDS": 10})
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_POLL_SECONDS = 1.0
    executor._POST_SELL_CASH_POLL_SECONDS = 0.5
    executor._POST_SELL_CASH_WAIT_SECONDS = 0.5

    executor.execute_plan({
        "sell_clear": [sell_data],
        "reduce": [],
        "increase": [(buy_data, 100.0)],
    })

    buy_calls = [call for call in broker.calls if call[0] == buy_data._name]
    assert buy_calls == [(buy_data._name, 100.0, 9.0)]
    assert fake_clock["t"] == 9.0
    assert fake_clock["t"] <= 10.0


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_split_orders_stop_at_deadline_and_keep_accepted_children(fake_clock, monkeypatch, side):
    monkeypatch.setattr(config, "LOT_SIZE", 100)
    monkeypatch.setattr(config, "BROKER_LOT_LIMITS", 100)
    position = 300 if side == "SELL" else 0
    broker = _BudgetBroker(fake_clock, position=position, submit_seconds=1.0)
    begin_live_run_budget(broker, {"LIVE_RUN_MAX_EXECUTION_SECONDS": 2})
    data = SimpleNamespace(_name="SHSE.512010")

    target = 0.0 if side == "SELL" else 3000.0
    proxy = broker.order_target_value(data, target=target)

    assert proxy is not None
    assert [order.submitted_size for order in broker.submitted] == [100, 100]
    if side == "BUY":
        assert set(broker._active_buys) == {"ORDER_1", "ORDER_2"}
    else:
        assert proxy.batch_order_ids == ("ORDER_1", "ORDER_2")
        assert proxy.batch_submitted_size == 200
        assert proxy.batch_submit_failed is True
        assert broker._pending_sells == {"ORDER_1", "ORDER_2"}


def test_overnight_cleanup_stops_canceling_at_deadline(fake_clock):
    class CleanupBroker(_BudgetBroker):
        def __init__(self):
            super().__init__(fake_clock)
            self.canceled = []

        def get_pending_orders(self):
            return [
                {"id": f"OLD_{index}", "symbol": "SHSE.600000", "direction": "BUY", "size": 100}
                for index in range(1, 4)
            ]

        def cancel_pending_order(self, order_id):
            self.canceled.append(order_id)
            fake_clock["t"] += 1.0
            return True

    broker = CleanupBroker()
    begin_live_run_budget(broker, {"LIVE_RUN_MAX_EXECUTION_SECONDS": 2})

    summary = broker.cleanup_overnight_orders()

    assert broker.canceled == ["OLD_1", "OLD_2"]
    assert summary == {"total": 3, "canceled": 2, "failed": 0, "skipped": 1}


def test_live_refresh_does_not_start_next_feed_after_deadline(fake_clock):
    index = pd.DatetimeIndex(["2026-07-28"])
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=index,
    )
    feeds = [
        SimpleNamespace(_name="SHSE.600000", p=SimpleNamespace(dataname=frame.copy())),
        SimpleNamespace(_name="SHSE.600001", p=SimpleNamespace(dataname=frame.copy())),
    ]
    broker = SimpleNamespace(datas=feeds)
    begin_live_run_budget(broker, {"LIVE_RUN_MAX_EXECUTION_SECONDS": 1})

    class Provider:
        def __init__(self):
            self.calls = []

        def get_history(self, symbol, *args, **kwargs):
            self.calls.append(symbol)
            fake_clock["t"] += 1.0
            return frame.copy()

    trader = object.__new__(LiveTrader)
    trader.broker = broker
    trader.data_provider = Provider()
    trader.config = {"timeframe": "Days", "compression": 1}
    trader._intraday_rebase_done_on = {}

    stats = trader._refresh_live_data(SimpleNamespace(now=pd.Timestamp("2026-07-29 14:45:00")))

    assert trader.data_provider.calls == ["SHSE.600000"]
    assert stats == {"total_feeds": 2, "updated_feeds": 0, "failed_feeds": 2}


def test_rejected_buy_cannot_retry_on_a_later_runs_budget(fake_clock, monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 100)
    broker = _BudgetBroker(fake_clock)
    data = SimpleNamespace(_name="SHSE.600000")
    begin_live_run_budget(broker, {"LIVE_RUN_MAX_EXECUTION_SECONDS": 1})
    first = broker.order_target_value(data, target=2000.0)
    original_deadline = broker._active_buys[first.id]["run_deadline"]

    fake_clock["t"] = 2.0
    begin_live_run_budget(broker, {"LIVE_RUN_MAX_EXECUTION_SECONDS": 10})
    first.status = "Rejected"
    broker.on_order_status(first)

    assert original_deadline == 1.0
    assert len(broker.submitted) == 1
    assert broker._active_buys == {}
    assert broker._virtual_spent_cash == 0.0


def test_rejected_buy_still_downgrades_while_original_budget_remains(fake_clock, monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 100)
    broker = _BudgetBroker(fake_clock)
    data = SimpleNamespace(_name="SHSE.600000")
    begin_live_run_budget(broker, {"LIVE_RUN_MAX_EXECUTION_SECONDS": 10})
    first = broker.order_target_value(data, target=2000.0)

    fake_clock["t"] = 1.0
    first.status = "Rejected"
    broker.on_order_status(first)

    assert [order.submitted_size for order in broker.submitted] == [200, 100]
    assert set(broker._active_buys) == {"ORDER_2"}


def test_backtest_order_path_stays_synchronous_and_ignores_live_pending(fake_clock, monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 100)
    monkeypatch.setattr(config, "BROKER_LOT_LIMITS", 100)
    broker = _BudgetBroker(fake_clock)
    broker.is_live = False
    data = SimpleNamespace(_name="SHSE.600000")

    proxy = broker.order_target_value(data, target=3000.0)

    assert proxy is not None
    assert [order.submitted_size for order in broker.submitted] == [300]
    assert broker.pending_queries == 0
    assert fake_clock["t"] == 0.0
