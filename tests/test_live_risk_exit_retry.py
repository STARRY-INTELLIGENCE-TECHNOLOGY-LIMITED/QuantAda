"""风控退出重试、结算对账、GM 可用资金与隔夜清理边界的回归测试。"""

import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import config
from common.live_runtime import order_created_before
from live_trader.engine import LiveTrader
from risk_controls.sample_stop_loss_take_profit import SampleStopLossTakeProfit

_flow_path = Path(__file__).with_name("test_engine_flow.py")
_spec = importlib.util.spec_from_file_location("test_engine_flow_for_risk_retry", _flow_path)
_flow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_flow)
MockEngineBroker = _flow.MockEngineBroker
MockOrderProxy = _flow.MockOrderProxy
DummyDataProvider = _flow.DummyDataProvider
CounterStrategy = _flow.CounterStrategy
MockContext = _flow.MockContext


@pytest.fixture(autouse=True)
def _force_lot_size(monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 100)


def _make_engine(monkeypatch, broker_cls=MockEngineBroker, now=datetime(2026, 2, 17, 10, 30, 0)):
    import live_trader.engine as engine_module

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (broker_cls, DummyDataProvider),
    )
    monkeypatch.setattr(engine_module, "get_class_from_name", lambda class_name, paths: CounterStrategy)
    monkeypatch.setattr(engine_module.time, "sleep", lambda _: None)
    engine = LiveTrader({
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    })
    context = MockContext(now=now)
    engine.init(context)
    engine.broker.set_datetime(context.now)
    return engine, context


class _LatchingSellRisk:
    """模拟样例风控: check 触发 SELL 时先置位 exit_triggered 闩锁。"""

    def __init__(self):
        self.exit_triggered = set()
        self.check_calls = 0

    def check(self, data):
        self.check_calls += 1
        if data._name in self.exit_triggered:
            return None
        self.exit_triggered.add(data._name)
        return "SELL"

    def notify_order(self, order):
        pass


# ---------- #3 风控闩锁在退出失败/未提交时释放 ----------

def test_live_unsubmitted_risk_exit_releases_latch_and_retries(monkeypatch):
    engine, _ = _make_engine(monkeypatch)
    engine.risk_control = _LatchingSellRisk()
    symbol = engine.broker.datas[0]._name
    calls = []

    def _no_order(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(engine.broker, "order_target_percent", _no_order)

    engine._check_risk_controls()
    assert symbol not in engine.risk_control.exit_triggered, "未提交的退出不能留下永久闩锁。"
    engine._check_risk_controls()
    assert len(calls) == 2, "下一轮应重新检查并重试退出。"


def test_live_rejected_risk_exit_releases_latch(monkeypatch):
    engine, _ = _make_engine(monkeypatch)
    engine.risk_control = _LatchingSellRisk()
    symbol = engine.broker.datas[0]._name
    engine.risk_control.exit_triggered.add(symbol)
    engine._pending_risk_orders = {symbol: MockOrderProxy("RISK_1", is_buy_order=False, status="Rejected")}

    engine._check_risk_controls()

    sells = [o for o in engine.broker.submitted_orders if o["side"] == "SELL"]
    assert sells, "被拒的风控退出单应释放闩锁并在同轮重新发起平仓。"


def test_sample_stop_loss_releases_latch_on_failed_exit_order():
    position = SimpleNamespace(size=1000, price=10.0)
    broker = SimpleNamespace(log=lambda *a, **k: None, getposition=lambda data: position)
    risk = SampleStopLossTakeProfit(broker, {"stop_loss_pct": 0.05, "take_profit_pct": 0.0})
    data = SimpleNamespace(_name="SHSE.600000", close=[9.0])

    assert risk.check(data) == "SELL"
    assert risk.check(data) is None, "退出在途时不应重复触发。"

    rejected_buy = SimpleNamespace(
        data=data, is_rejected=lambda: True, is_buy=lambda: True, is_sell=lambda: False
    )
    risk.notify_order(rejected_buy)
    assert "SHSE.600000" in risk.exit_triggered, "非平仓方向的失败单不应释放闩锁。"

    canceled_sell = SimpleNamespace(
        data=data,
        is_rejected=lambda: False,
        is_canceled=lambda: True,
        is_buy=lambda: False,
        is_sell=lambda: True,
    )
    risk.notify_order(canceled_sell)
    assert risk.check(data) == "SELL", "平仓单被撤后下一根 bar 应重试退出。"


# ---------- #4 风控在途单以券商在途快照为准 ----------

def test_stale_risk_proxy_dropped_when_order_no_longer_working(monkeypatch):
    engine, _ = _make_engine(monkeypatch)
    engine.risk_control = _LatchingSellRisk()
    symbol = engine.broker.datas[0]._name
    engine.risk_control.exit_triggered.add(symbol)
    # 提交时代理仍显示 Submitted，但券商在途列表里已经没有该单（被拒/撤）。
    engine._pending_risk_orders = {symbol: MockOrderProxy("GONE_1", is_buy_order=False, status="Submitted")}

    engine._check_risk_controls()

    sells = [o for o in engine.broker.submitted_orders if o["side"] == "SELL"]
    assert sells, "过期代理不应永久阻塞止损，应重新发起平仓。"
    assert engine._pending_risk_orders[symbol].id != "GONE_1"


def test_risk_proxy_kept_when_order_still_working_or_snapshot_untrusted(monkeypatch):
    engine, _ = _make_engine(monkeypatch)
    engine.risk_control = _LatchingSellRisk()
    symbol = engine.broker.datas[0]._name
    engine.broker.submitted_orders.append(
        {"id": "LIVE_1", "side": "SELL", "volume": 1000, "symbol": symbol, "status": "Submitted"}
    )
    engine._pending_risk_orders = {symbol: MockOrderProxy("LIVE_1", is_buy_order=False, status="Submitted")}

    assert engine._check_risk_controls() is True
    assert engine._pending_risk_orders[symbol].id == "LIVE_1"
    assert engine.risk_control.check_calls == 0

    # 快照不可信时不能把空列表当作“已不在途”。
    engine._pending_risk_orders = {symbol: MockOrderProxy("GONE_2", is_buy_order=False, status="Submitted")}

    def _untrusted():
        engine.broker._last_pending_orders_fetch_failed = True
        return []

    monkeypatch.setattr(engine.broker, "get_pending_orders", _untrusted)
    assert engine._check_risk_controls() is True
    assert engine._pending_risk_orders[symbol].id == "GONE_2"


# ---------- #5 结算对账: 幂等记录、不阻断风控 ----------

class _SettlementBroker:
    is_live = True

    def __init__(self, events, apply_error=None):
        self.events = events
        self.apply_error = apply_error
        self.apply_calls = 0
        self.blocks = {}

    def reconcile_option_settlement(self):
        return {"trusted": True, "supported": True, "events": self.events, "adjustments": ()}

    def apply_option_settlement_adjustments(self, adjustments, events=()):
        self.apply_calls += 1
        if self.apply_error:
            raise self.apply_error

    def set_option_entry_kill_switch(self, blocked, *, reason="", source="manual"):
        if blocked:
            self.blocks[source] = reason

    def clear_option_entry_kill_switch(self, source="manual"):
        self.blocks.pop(source, None)


def _settlement_trader(broker, strategy):
    trader = object.__new__(LiveTrader)
    trader.broker = broker
    trader.alarm_manager = None
    trader.strategy = strategy
    trader._processed_settlement_event_ids = {}
    return trader


def test_settlement_downstream_failure_does_not_block_or_double_apply():
    events = ({"event_id": "E1", "symbol": "US.AAPL", "type": "ASSIGNED_PUT"},)
    broker = _SettlementBroker(events)

    def _fail(_events):
        raise RuntimeError("strategy boom")

    trader = _settlement_trader(broker, SimpleNamespace(reconcile_settlement=_fail))

    assert trader._reconcile_option_settlement() is True, "下游失败不能阻断风控退出。"
    assert "settlement" in broker.blocks, "下游失败应阻断期权开仓。"
    assert trader._reconcile_option_settlement() is True
    assert broker.apply_calls == 1, "已应用的结算事件不能在下一轮重复应用。"


def test_settlement_apply_failure_retries_next_run_without_blocking():
    events = ({"event_id": "E1", "symbol": "US.AAPL", "type": "ASSIGNED_PUT"},)
    broker = _SettlementBroker(events, apply_error=RuntimeError("apply boom"))
    trader = _settlement_trader(broker, SimpleNamespace())

    assert trader._reconcile_option_settlement() is True
    assert "E1" not in trader._processed_settlement_event_ids
    broker.apply_error = None
    assert trader._reconcile_option_settlement() is True
    assert broker.apply_calls == 2
    assert "E1" in trader._processed_settlement_event_ids


def test_settlement_processed_ids_trim_oldest_first():
    broker = _SettlementBroker(({"event_id": "NEW"},))
    trader = _settlement_trader(broker, SimpleNamespace())
    trader._processed_settlement_event_ids = dict.fromkeys(f"OLD_{i}" for i in range(5001))

    trader._reconcile_option_settlement()

    kept = list(trader._processed_settlement_event_ids)
    assert len(kept) == 2500
    assert kept[-1] == "NEW" and "OLD_0" not in kept and "OLD_5000" in kept


# ---------- #6 GM 可用资金不双重扣减挂单冻结 ----------

def test_gm_get_cash_does_not_double_count_frozen_orders(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module
    from unittest.mock import MagicMock

    monkeypatch.setattr(gm_module, "context", None, raising=False)
    snapshot = {"available": 8000.0, "order_frozen": 2000.0, "nav": 10000.0}
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(**snapshot), raising=False)
    broker = gm_module.GmBrokerAdapter(context=MagicMock())
    broker._virtual_spent_cash = 2000.0

    assert broker.get_cash() == pytest.approx(8000.0), "柜台已冻结的挂单资金不能再扣一次。"

    # 柜台冻结滞后: 只扣除尚未冻结的部分。
    snapshot.update(available=10000.0, order_frozen=0.0)
    assert broker.get_cash() == pytest.approx(8000.0)
    snapshot.update(available=9000.0, order_frozen=1000.0)
    assert broker.get_cash() == pytest.approx(8000.0)


# ---------- #7 隔夜清理只撤创建于当日之前的委托 ----------

def test_order_created_before_handles_unknown_and_timezones():
    day = pd.Timestamp("2026-03-03")
    assert order_created_before(None, day) is None
    assert order_created_before("bad", day) is None
    assert order_created_before(pd.Timestamp("2026-03-02 14:00"), day) is True
    assert order_created_before(pd.Timestamp("2026-03-03 09:31"), day) is False
    aware_day = pd.Timestamp("2026-03-03", tz="America/New_York")
    assert order_created_before(pd.Timestamp("2026-03-03 15:00", tz="UTC"), aware_day) is False
    assert order_created_before(pd.Timestamp("2026-03-03 04:00", tz="UTC"), aware_day) is True


class _DatedPendingBroker(MockEngineBroker):
    def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None):
        super().__init__(context, cash_override, commission_override, slippage_override)
        self.cancel_calls = []
        self.dated_pending = [
            {"id": "OLD", "symbol": "SHSE.600000", "direction": "BUY", "size": 100,
             "created_at": pd.Timestamp("2026-03-02 14:50")},
            {"id": "TODAY", "symbol": "SHSE.600000", "direction": "SELL", "size": 100,
             "created_at": pd.Timestamp("2026-03-03 09:31")},
            {"id": "UNKNOWN", "symbol": "SHSE.600000", "direction": "BUY", "size": 100},
        ]

    def get_pending_orders(self):
        return list(self.dated_pending)

    def cancel_pending_order(self, order_id):
        self.cancel_calls.append(order_id)
        self.dated_pending = [po for po in self.dated_pending if po["id"] != order_id]
        return True


def test_overnight_cleanup_keeps_current_day_orders_after_restart(monkeypatch):
    engine, context = _make_engine(monkeypatch, _DatedPendingBroker, now=datetime(2026, 3, 3, 10, 30, 0))
    engine._overnight_cleanup_done_on = None  # 模拟进程重启后内存标记丢失

    engine._cleanup_overnight_orders_before_refresh(context)

    assert sorted(engine.broker.cancel_calls) == ["OLD", "UNKNOWN"], "当日委托不能被隔夜清理撤销。"
    assert [po["id"] for po in engine.broker.dated_pending] == ["TODAY"]
    assert engine._overnight_cleanup_done_on == context.now.date(), "屏障只看隔夜单，当日单不应阻止完成标记。"


def test_overnight_cleanup_not_marked_done_without_attempt(monkeypatch):
    import live_trader.engine as engine_module

    engine, context = _make_engine(monkeypatch, _DatedPendingBroker, now=datetime(2026, 3, 3, 9, 30, 0))
    monkeypatch.setattr(engine_module, "live_run_budget_expired", lambda *a, **k: True)

    engine._cleanup_overnight_orders_before_refresh(context)

    assert engine.broker.cancel_calls == []
    assert engine._overnight_cleanup_done_on is None, "未做任何尝试时不能标记当日已清理。"


def test_gm_pending_orders_expose_created_at(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module
    from unittest.mock import MagicMock

    monkeypatch.setattr(gm_module, "context", None, raising=False)
    # 隔离真实 SDK: 构造期不得发起任何柜台查询。
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0), raising=False)
    broker = gm_module.GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    order = {
        "cl_ord_id": "GM1", "symbol": "SHSE.600000", "side": gm_module.OrderSide_Buy,
        "volume": 100, "filled_volume": 0, "account_id": "A1",
        "created_at": datetime(2026, 3, 2, 14, 50),
    }
    monkeypatch.setattr(gm_module, "_require_gm_api", lambda: None, raising=False)
    monkeypatch.setattr(broker, "_query_unfinished_orders_single_account", lambda q: [order])
    fake_api = SimpleNamespace(get_unfinished_orders=lambda: [order])
    monkeypatch.setitem(__import__("sys").modules, "gm.api", fake_api)

    pending = broker.get_pending_orders()

    assert broker._last_pending_orders_fetch_failed is False
    assert pending[0]["created_at"] == pd.Timestamp("2026-03-02 14:50", tz="Asia/Shanghai")


def test_futu_pending_orders_expose_market_local_created_at():
    _futu = importlib.util.spec_from_file_location(
        "test_futu_adapter_for_created_at", Path(__file__).with_name("test_futu_adapter.py")
    )
    futu_tests = importlib.util.module_from_spec(_futu)
    _futu.loader.exec_module(futu_tests)
    fake = futu_tests.FakeTradeContext()
    fake.orders.loc[0, 'create_time'] = '2026-03-02 14:50:00.000'
    broker, _ = futu_tests._broker(fake)

    pending = broker.get_pending_orders()

    assert broker._last_pending_orders_fetch_failed is False
    assert pending[0]['created_at'] == pd.Timestamp('2026-03-02 14:50', tz='Asia/Shanghai')


def test_order_created_before_uses_the_aware_side_timezone_not_the_host_timezone(monkeypatch):
    # 无时区一侧按带时区一侧解释；与主机时区无关。
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(__import__("time"), "tzset"):
        __import__("time").tzset()
    created = pd.Timestamp("2026-03-03 00:30", tz="Asia/Shanghai")
    assert order_created_before(created, pd.Timestamp("2026-03-03")) is False
    assert order_created_before(pd.Timestamp("2026-03-02 23:30"), pd.Timestamp("2026-03-03", tz="Asia/Shanghai")) is True
