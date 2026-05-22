from datetime import datetime
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

import config
from data_providers.base_provider import BaseDataProvider
from live_trader.engine import LiveTrader
from risk_controls.base_risk_control import BaseRiskControl
from strategies.base_strategy import BaseStrategy


# 动态加载 Phase1/2 的单测桩，避免依赖 tests 目录作为可导入包
_broker_state_path = Path(__file__).with_name("test_broker_state.py")
_spec = importlib.util.spec_from_file_location("test_broker_state_for_e2e", _broker_state_path)
_mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader, "无法加载 test_broker_state.py，E2E 测试缺少基础 MockBroker。"
_spec.loader.exec_module(_mod)
MockBroker = _mod.MockBroker
MockOrderProxy = _mod.MockOrderProxy


class DummyDataProvider(BaseDataProvider):
    """E2E 集成测试专用数据源：仅返回稳定的内存 K 线。"""

    PRIORITY = 1

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
        end = pd.to_datetime(end_date) if end_date else pd.Timestamp.now().normalize()
        idx = pd.date_range(end=end, periods=5, freq="D")
        df = pd.DataFrame(
            {
                "open": [10.0, 10.0, 10.0, 10.0, 10.0],
                "high": [10.2, 10.2, 10.2, 10.2, 10.2],
                "low": [9.8, 9.8, 9.8, 9.8, 9.8],
                "close": [10.0, 10.0, 10.0, 10.0, 10.0],
                "volume": [10000, 10000, 10000, 10000, 10000],
            },
            index=idx,
        )
        return df

    def get_history(self, symbol: str, start_date: str, end_date: str,
                    timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
        return self.get_data(symbol, start_date, end_date, timeframe, compression)


class MockEngineBroker(MockBroker):
    """
    引擎集成测试 Broker:
    - 继承单元测试阶段的 MockBroker
    - 支持在途单跟踪与手动撤单/状态回放
    """

    def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None):
        initial_cash = float(cash_override if cash_override is not None else 100000.0)
        super().__init__(initial_cash=initial_cash)
        self._context = context
        self._commission_override = commission_override
        self._slippage_override = slippage_override

        # E2E 场景: 账户初始持有 1000 股，便于 14:45 触发清仓 sell
        self.mock_position = 1000

        # 内存订单簿: 记录每笔单的状态，支持 pending/cancelled/filled 手工推进
        self._order_book = {}
        self.cancel_requests = []

    def _submit_order(self, data, volume, side, price):
        oid = f"ORDER_{len(self.submitted_orders) + 1}"
        self.submitted_orders.append(
            {
                "id": oid,
                "side": side,
                "volume": volume,
                "symbol": data._name,
                "status": "Submitted",
            }
        )
        self._order_book[oid] = self.submitted_orders[-1]
        return MockOrderProxy(oid, is_buy_order=(side == "BUY"), status="Submitted")

    def get_pending_orders(self):
        pending = []
        for order in self.submitted_orders:
            if order.get("status") in {"PendingSubmit", "Submitted", "PendingCancel"}:
                pending.append(
                    {
                        "id": order["id"],
                        "symbol": order["symbol"],
                        "direction": order["side"],
                        "size": order["volume"],
                    }
                )
        return pending

    def cancel_order(self, oid: str):
        order = self._order_book.get(oid)
        if not order:
            return None
        if order.get("status") in {"Filled", "Canceled", "Rejected"}:
            return None
        order["status"] = "PendingCancel"
        self.cancel_requests.append(oid)
        return oid

    def simulate_order_status(self, oid: str, status: str):
        order = self._order_book.get(oid)
        if not order:
            return None

        order["status"] = status
        proxy = MockOrderProxy(oid, is_buy_order=(order["side"] == "BUY"), status=status)
        self.on_order_status(proxy)

        # 若测试需要推进真实持仓，可通过 Filled 回放更新持仓
        if status == "Filled":
            if order["side"] == "BUY":
                self.mock_position += order["volume"]
            elif order["side"] == "SELL":
                self.mock_position = max(0, self.mock_position - order["volume"])
        return proxy

    @staticmethod
    def is_live_mode(context):
        return True


class DummyHeartbeatStrategy(BaseStrategy):
    """
    心跳策略:
    - 09:35 建仓 target=0.5
    - 10:00 遍历 pending 并发起撤单
    - 14:45 清仓 target=0
    """

    def init(self):
        self.order = None
        self._did_open = False
        self._did_cancel = False
        self._did_close = False

    def next(self):
        now = self.broker.datetime.datetime()
        data = self.broker.datas[0]
        hhmm = now.strftime("%H:%M")

        if hhmm == "09:35" and not self._did_open:
            self.order = self.broker.order_target_percent(data=data, target=0.5)
            self._did_open = True

        elif hhmm == "10:00" and not self._did_cancel:
            for po in list(self.broker.get_pending_orders()):
                self.broker.cancel_order(po["id"])
            self._did_cancel = True

        elif hhmm == "14:45" and not self._did_close:
            self.order = self.broker.order_target_percent(data=data, target=0.0)
            self._did_close = True

    def notify_order(self, order):
        super().notify_order(order)
        # 集成测试中不锁单，保证下一个心跳仍会执行 next
        self.order = None


class CounterStrategy(BaseStrategy):
    def init(self):
        self.next_calls = 0
        self.order = None

    def next(self):
        self.next_calls += 1


class MockContext:
    def __init__(self, now):
        self.now = now


@pytest.fixture(autouse=True)
def _force_lot_size(monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 100)


def test_full_day_engine_lifecycle(monkeypatch):
    """
    E2E Red Team Test:
    还原完整日内链路: 09:35 买入 -> 10:00 撤单并回调 -> 14:45 清仓。
    """
    import live_trader.engine as engine_module

    # 1) 劫持引擎动态装配，接入内存版 Broker/DataProvider/Strategy
    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: DummyHeartbeatStrategy,
    )

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)

    # Step 1: 09:35 开盘建仓
    context.now = datetime(2026, 2, 17, 9, 35, 0)
    engine.run(context)

    buy_orders = [o for o in engine.broker.submitted_orders if o["side"] == "BUY"]
    assert len(buy_orders) == 1, "开盘阶段未触发买单，E2E 流程在第一步即失败！"

    buy_order = buy_orders[0]
    expected_deduct = buy_order["volume"] * 10.0 * engine.broker.safety_multiplier
    assert engine.broker._virtual_spent_cash == pytest.approx(expected_deduct), (
        "开盘建仓后虚拟账本扣减异常，资金安全垫记账与下单量不一致！"
    )
    assert buy_order["id"] in engine.broker._active_buys, "开盘买单未进入 _active_buys，后续撤单/拒单回调将失效！"

    # Step 2: 10:00 主动撤单 + 券商回调 Canceled
    context.now = datetime(2026, 2, 17, 10, 0, 0)
    engine.run(context)

    assert buy_order["id"] in engine.broker.cancel_requests, "10:00 未触发主动撤单，策略心跳逻辑失效！"

    canceled_proxy = engine.broker.simulate_order_status(buy_order["id"], "Canceled")
    engine.notify_order(canceled_proxy)

    assert engine.broker._virtual_spent_cash == pytest.approx(0.0), (
        "买单撤销后资金未回滚，发生幽灵账本冻结，可能导致后续无法下单！"
    )
    assert buy_order["id"] not in engine.broker._active_buys, "_active_buys 未清理，状态机会被旧订单污染！"
    assert len(engine.broker.get_pending_orders()) == 0, "撤单回调后 pending 队列未清空，存在死锁风险！"

    # Step 3: 14:45 收盘清仓
    context.now = datetime(2026, 2, 17, 14, 45, 0)
    engine.run(context)

    sell_orders = [o for o in engine.broker.submitted_orders if o["side"] == "SELL"]
    assert len(sell_orders) >= 1, "收盘阶段未触发清仓卖单，目标仓位归零链路失效！"
    assert sell_orders[-1]["volume"] == 1000, "清仓卖单数量异常，应与当前真实持仓 1000 股对齐！"


def test_live_data_source_override_uses_data_manager(monkeypatch):
    """
    实盘 data_source 覆盖回归:
    指定 data_source 时应通过 DataManager 拉取历史数据，而非默认 Provider。"""
    import live_trader.engine as engine_module

    captured = {}

    class ExplodingProvider(DummyDataProvider):
        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
            raise AssertionError("Adapter provider should not be used when data_source override is set.")

    class StubDataManager:
        def __init__(self):
            captured["manager"] = self

        def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                     specified_sources: str = None, timeframe: str = "Days",
                     compression: int = 1, refresh: bool = False) -> pd.DataFrame:
            captured["specified_sources"] = specified_sources
            end = pd.to_datetime(end_date) if end_date else pd.Timestamp.now().normalize()
            idx = pd.date_range(end=end, periods=3, freq="D")
            return pd.DataFrame(
                {
                    "open": [10.0, 10.1, 10.2],
                    "high": [10.2, 10.3, 10.4],
                    "low": [9.8, 9.9, 10.0],
                    "close": [10.0, 10.1, 10.2],
                    "volume": [10000, 10000, 10000],
                },
                index=idx,
            )

    monkeypatch.setattr(engine_module, "DataManager", StubDataManager)
    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, ExplodingProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["AAPL"],
        "params": {},
        "data_source": "tiingo",
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)

    assert captured.get("specified_sources") == "tiingo", (
        "data_source 覆盖未传递给 DataManager，实盘数据源切换失效。"
    )


def test_live_data_source_override_applies_to_selector(monkeypatch):
    """
    选股 data_source 覆盖回归:
    指定 data_source 时，selector 调用的 DataManager 也应强制使用该数据源。"""
    import live_trader.engine as engine_module

    captured = {"sources": []}

    class StubDataManager:
        def __init__(self):
            captured["manager"] = self

        def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                     specified_sources: str = None, timeframe: str = "Days",
                     compression: int = 1, refresh: bool = False) -> pd.DataFrame:
            captured["sources"].append(specified_sources)
            end = pd.to_datetime(end_date) if end_date else pd.Timestamp.now().normalize()
            idx = pd.date_range(end=end, periods=3, freq="D")
            return pd.DataFrame(
                {
                    "open": [10.0, 10.1, 10.2],
                    "high": [10.2, 10.3, 10.4],
                    "low": [9.8, 9.9, 10.0],
                    "close": [10.0, 10.1, 10.2],
                    "volume": [10000, 10000, 10000],
                },
                index=idx,
            )

    class DummySelector:
        def __init__(self, data_manager):
            self.data_manager = data_manager

        def run_selection(self):
            self.data_manager.get_data("AAPL", start_date="2026-01-01", end_date="2026-02-01")
            return ["AAPL"]

    monkeypatch.setattr(engine_module, "DataManager", StubDataManager)
    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )

    def _fake_get_class(class_name, paths):
        if class_name == "DummySelector":
            return DummySelector
        return CounterStrategy

    monkeypatch.setattr(engine_module, "get_class_from_name", _fake_get_class)

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "selection_name": "DummySelector",
        "params": {},
        "data_source": "tiingo",
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)

    assert captured["sources"], "selector 未调用 DataManager.get_data。"
    assert all(src == "tiingo" for src in captured["sources"]), (
        "selector 调用未强制 data_source，仅使用默认链路。"
    )


def test_backtest_mode_auto_warmup_start_date(monkeypatch):
    """
    回测模式默认预热:
    传入 start_date 后，引擎应自动向前拉取 ANNUAL_FACTOR 天历史。
    """
    import live_trader.engine as engine_module

    class RecordingDataProvider(DummyDataProvider):
        def __init__(self):
            super().__init__()
            self.history_calls = []

        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
            self.history_calls.append(
                {
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "timeframe": timeframe,
                    "compression": compression,
                }
            )
            return super().get_history(symbol, start_date, end_date, timeframe, compression)

    class MockBacktestBroker(MockEngineBroker):
        @staticmethod
        def is_live_mode(context):
            return False

        @staticmethod
        def extract_run_config(context):
            return {
                "start_date": "20260201",
                "end_date": "20260213",
                "cash": 100000.0,
            }

    monkeypatch.setattr(config, "ANNUAL_FACTOR", 252)
    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockBacktestBroker, RecordingDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: DummyHeartbeatStrategy,
    )

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)

    calls = engine.data_provider.history_calls
    assert calls, "回测初始化阶段未触发历史数据拉取。"

    expected_warmup_start = (pd.Timestamp("2026-02-01") - pd.Timedelta(days=252)).strftime("%Y-%m-%d")
    assert calls[0]["start_date"] == expected_warmup_start, "回测预热起点不正确，未按默认窗口前推。"
    assert calls[0]["end_date"] == "20260213", "回测结束日期透传异常。"


def test_selector_dataframe_contract_in_live_engine(monkeypatch):
    """
    实盘 selector 契约测试:
    1) selector 必须收到非空 data_manager
    2) selector 返回 DataFrame 时，引擎应读取 index 作为 symbols
    """
    import live_trader.engine as engine_module

    class FakeDataManager:
        def __init__(self):
            self.marker = "fake_dm"

    class DataFrameSelector:
        captured_data_manager = None
        run_calls = 0

        def __init__(self, data_manager):
            DataFrameSelector.captured_data_manager = data_manager

        def run_selection(self):
            DataFrameSelector.run_calls += 1
            # 覆盖去重与空值过滤逻辑
            return pd.DataFrame(index=["SHSE.600000", "SZSE.000001", "SHSE.600000", " "])

    def _resolver(class_name, paths):
        if class_name == "DataFrameSelector":
            return DataFrameSelector
        return DummyHeartbeatStrategy

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(engine_module, "DataManager", FakeDataManager)
    monkeypatch.setattr(engine_module, "get_class_from_name", _resolver)

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "selection_name": "DataFrameSelector",
        "platform": "mock_engine",
        "symbols": [],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)

    assert isinstance(DataFrameSelector.captured_data_manager, FakeDataManager), (
        "selector 未收到有效 data_manager，实盘选股契约仍然不完整。"
    )

    loaded_symbols = [d._name for d in engine.broker.datas]
    assert loaded_symbols == ["SHSE.600000", "SZSE.000001"], (
        "selector 返回 DataFrame 时应使用 index 作为标的列表。"
    )

    # 二次查询应命中缓存，不应再次执行 selector
    _ = engine._determine_symbols()
    _ = engine._determine_symbols()
    assert DataFrameSelector.run_calls == 1, "同一次引擎会话内，selector 不应被重复执行。"


def test_live_minutes_warmup_window_uses_full_timestamp(monkeypatch):
    """
    实盘分钟级预热:
    初始化拉取窗口应包含完整时间戳(含 HH:MM:SS)，避免分钟数据边界被截断到 00:00:00。
    """
    import live_trader.engine as engine_module

    class RecordingDataProvider(DummyDataProvider):
        def __init__(self):
            super().__init__()
            self.history_calls = []

        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
            self.history_calls.append(
                {
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "timeframe": timeframe,
                    "compression": compression,
                }
            )
            return super().get_history(symbol, start_date, end_date, timeframe, compression)

    monkeypatch.setattr(config, "ANNUAL_FACTOR", 30)
    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, RecordingDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: DummyHeartbeatStrategy,
    )

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "timeframe": "Minutes",
        "compression": 5,
        "params": {},
    }

    now = datetime(2026, 2, 17, 14, 35, 20)
    engine = LiveTrader(cfg)
    context = MockContext(now=now)
    engine.init(context)

    calls = engine.data_provider.history_calls
    assert calls, "分钟级初始化应触发至少一次 get_history。"
    first = calls[0]

    expected_start = (pd.Timestamp(now) - pd.Timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    expected_end = pd.Timestamp(now).strftime("%Y-%m-%d %H:%M:%S")

    assert first["timeframe"] == "Minutes", "分钟级配置应透传给 DataProvider。"
    assert first["start_date"] == expected_start, "分钟级预热起点应保留完整时分秒。"
    assert first["end_date"] == expected_end, "分钟级预热终点应保留完整时分秒。"


def test_risk_pending_order_terminal_precedence(monkeypatch):
    """
    风控状态机回归:
    若 pending 风控单对象同时表现为 is_canceled=True 且 is_accepted=True，
    引擎必须优先按终态处理并清理追踪，不能被 accepted 分支卡死。
    """
    import live_trader.engine as engine_module

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: DummyHeartbeatStrategy,
    )

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 35, 0))
    engine.init(context)
    engine.broker.set_datetime(context.now)

    class DummyRisk:
        def __init__(self):
            self.exit_triggered = set()

        def check(self, data):
            return None

        def notify_order(self, order):
            pass

    engine.risk_control = DummyRisk()

    symbol = engine.broker.datas[0]._name
    # 复用测试桩 MockOrderProxy: 对于 Canceled 状态，is_canceled=True 且 is_accepted=True
    engine._pending_risk_orders = {
        symbol: MockOrderProxy("RISK_1", is_buy_order=False, status="Canceled")
    }

    engine._check_risk_controls()

    assert symbol not in engine._pending_risk_orders, (
        "风控终态优先级错误：Canceled 风控单未被清理，存在状态机卡死风险。"
    )


def test_on_order_status_callback_sell_fill_triggers_balance_sync_only(monkeypatch):
    """
    回调链路测试:
    卖单成交后，必须触发 sync_balance。
    """
    import live_trader.engine as engine_module

    class DummyOrderProxy:
        def __init__(self):
            self.status = "Filled"
            self.data = SimpleNamespace(_name="SHSE.600000")
            self.executed = SimpleNamespace(size=100, price=10.0, value=1000.0, comm=0.0)

        def is_buy(self):
            return False

        def is_sell(self):
            return True

        def is_rejected(self):
            return False

        def is_canceled(self):
            return False

    class DummyBroker:
        def __init__(self):
            self.order_status_calls = 0
            self.sync_calls = 0
            self.proxy = DummyOrderProxy()

        def convert_order_proxy(self, raw_order):
            return self.proxy

        def on_order_status(self, proxy):
            self.order_status_calls += 1

        def sync_balance(self):
            self.sync_calls += 1

        def get_cash(self):
            return 12345.0

    class DummyStrategy:
        def __init__(self, broker):
            self.broker = broker
            self.notify_calls = 0

        def notify_order(self, order):
            self.notify_calls += 1

    broker = DummyBroker()
    strategy = DummyStrategy(broker)
    context = SimpleNamespace(
        strategy_instance=strategy,
        now=datetime(2026, 2, 17, 10, 5, 0)
    )
    raw_order = SimpleNamespace(statusMsg="filled")

    engine_module.on_order_status_callback(context, raw_order)

    assert broker.order_status_calls == 1, "回调应先喂给 broker.on_order_status。"
    assert strategy.notify_calls == 1, "回调应通知策略 notify_order。"
    assert broker.sync_calls == 1, "卖单成交后应触发资金同步。"


def test_on_order_status_callback_rejected_sell_should_not_retry(monkeypatch):
    """
    回调链路测试:
    卖单被拒绝时，不应触发 sync_balance。
    """
    import live_trader.engine as engine_module

    class DummyOrderProxy:
        def __init__(self):
            self.status = "Rejected"
            self.data = SimpleNamespace(_name="SHSE.600000")
            # size>0 用于验证仍不会进入“卖出成交重试”分支
            self.executed = SimpleNamespace(size=100, price=10.0, value=1000.0, comm=0.0)

        def is_buy(self):
            return False

        def is_sell(self):
            return True

        def is_rejected(self):
            return True

        def is_canceled(self):
            return False

    class DummyBroker:
        def __init__(self):
            self.sync_calls = 0
            self.proxy = DummyOrderProxy()

        def convert_order_proxy(self, raw_order):
            return self.proxy

        def on_order_status(self, proxy):
            pass

        def sync_balance(self):
            self.sync_calls += 1

        def get_cash(self):
            return 12345.0

    class DummyStrategy:
        def __init__(self, broker):
            self.broker = broker

        def notify_order(self, order):
            pass

    broker = DummyBroker()
    strategy = DummyStrategy(broker)
    context = SimpleNamespace(
        strategy_instance=strategy,
        now=datetime(2026, 2, 17, 10, 10, 0)
    )
    raw_order = SimpleNamespace(statusMsg="rejected")

    engine_module.on_order_status_callback(context, raw_order)

    assert broker.sync_calls == 0, "拒单不应触发资金同步。"


def test_on_order_status_callback_partial_sell_should_not_sync_balance(monkeypatch):
    """
    回调链路测试:
    卖单部分成交(非 Filled 终态)不应触发 sync_balance，避免日志/同步刷屏。
    """
    import live_trader.engine as engine_module

    class DummyOrderProxy:
        def __init__(self):
            self.id = "ORDER_PARTIAL_SELL_1"
            self.status = "Submitted"
            self.data = SimpleNamespace(_name="SPY.ARCA")
            self.executed = SimpleNamespace(size=100, price=10.0, value=1000.0, comm=0.0)

        def is_completed(self):
            return False

        def is_buy(self):
            return False

        def is_sell(self):
            return True

        def is_rejected(self):
            return False

        def is_canceled(self):
            return False

    class DummyBroker:
        def __init__(self):
            self.sync_calls = 0
            self.proxy = DummyOrderProxy()

        def convert_order_proxy(self, raw_order):
            return self.proxy

        def on_order_status(self, proxy):
            pass

        def sync_balance(self):
            self.sync_calls += 1

        def get_cash(self):
            return 12345.0

    class DummyStrategy:
        def __init__(self, broker):
            self.broker = broker

        def notify_order(self, order):
            pass

    broker = DummyBroker()
    strategy = DummyStrategy(broker)
    context = SimpleNamespace(
        strategy_instance=strategy,
        now=datetime(2026, 2, 17, 10, 12, 0)
    )
    raw_order = SimpleNamespace(statusMsg="partial sell")

    engine_module.on_order_status_callback(context, raw_order)

    assert broker.sync_calls == 0, "卖单部分成交不应触发资金同步。"


def test_on_order_status_callback_dedupes_duplicate_status(monkeypatch):
    """
    回调去重回归:
    同一订单同一状态重复回调，不应重复通知策略，避免日志/告警刷屏。
    """
    import live_trader.engine as engine_module

    class DummyOrderProxy:
        def __init__(self):
            self.id = "ORDER_DUP_1"
            self.status = "Inactive"
            self.data = SimpleNamespace(_name="GLD.ARCA")
            self.executed = SimpleNamespace(size=0.0, price=0.0, value=0.0, comm=0.0)

        def is_buy(self):
            return True

        def is_sell(self):
            return False

        def is_rejected(self):
            return True

        def is_canceled(self):
            return False

    class DummyBroker:
        def __init__(self):
            self.proxy = DummyOrderProxy()
            self.order_status_calls = 0

        def convert_order_proxy(self, raw_order):
            return self.proxy

        def on_order_status(self, proxy):
            self.order_status_calls += 1

    class DummyEngineStrategy:
        def __init__(self, broker):
            self.broker = broker
            self.notify_calls = 0

        def notify_order(self, order):
            self.notify_calls += 1

    broker = DummyBroker()
    strategy = DummyEngineStrategy(broker)
    context = SimpleNamespace(
        strategy_instance=strategy,
        now=datetime(2026, 2, 17, 10, 10, 0)
    )
    raw_order = SimpleNamespace(statusMsg="inactive")

    engine_module.on_order_status_callback(context, raw_order)
    engine_module.on_order_status_callback(context, raw_order)

    assert broker.order_status_calls == 2, "Broker 状态维护应仍可接收重复回调。"
    assert strategy.notify_calls == 1, "重复状态回调应被去重，避免重复通知策略。"


def test_on_order_status_callback_partial_fill_does_not_push_trade_alarm(monkeypatch):
    """
    成交推送降噪:
    部分成交(在途态)不应触发 push_trade，避免被分笔成交刷屏。
    """
    import live_trader.engine as engine_module

    class DummyOrderProxy:
        def __init__(self):
            self.id = "ORDER_PARTIAL_1"
            self.status = "Submitted"
            self.data = SimpleNamespace(_name="QQQ.NASDAQ")
            self.executed = SimpleNamespace(size=30.0, price=12.3, value=369.0, comm=0.0)
            self.trade = SimpleNamespace(order=SimpleNamespace(totalQuantity=100.0))

        def is_completed(self):
            return False

        def is_buy(self):
            return True

        def is_sell(self):
            return False

        def is_rejected(self):
            return False

        def is_canceled(self):
            return False

    class DummyBroker:
        def __init__(self):
            self.proxy = DummyOrderProxy()

        def convert_order_proxy(self, raw_order):
            return self.proxy

        def on_order_status(self, proxy):
            pass

    class DummyStrategy:
        def __init__(self, broker):
            self.broker = broker

        def notify_order(self, order):
            pass

    pushed_trades = []

    class DummyAlarmManager:
        def push_text(self, content, level='INFO'):
            pass

        def push_trade(self, trade_info):
            pushed_trades.append(trade_info)

    monkeypatch.setattr(engine_module, "AlarmManager", lambda: DummyAlarmManager())

    broker = DummyBroker()
    strategy = DummyStrategy(broker)
    context = SimpleNamespace(strategy_instance=strategy, now=datetime(2026, 2, 17, 10, 16, 0))
    raw_order = SimpleNamespace(statusMsg="partial")

    engine_module.on_order_status_callback(context, raw_order)

    assert pushed_trades == [], "部分成交不应触发成交推送，避免告警淹没。"


def test_on_order_status_callback_terminal_fill_pushes_once_with_target_qty(monkeypatch):
    """
    成交推送语义:
    Filled 终态仅推送一次，且数量使用最终目标下单量(totalQuantity/volume)。
    """
    import live_trader.engine as engine_module

    class DummyOrderProxy:
        def __init__(self):
            self.id = "ORDER_FILLED_1"
            self.status = "Filled"
            self.data = SimpleNamespace(_name="SPY.ARCA")
            self.executed = SimpleNamespace(
                size=40.0,
                price=10.0,
                value=400.0,
                comm=0.0,
                dt=datetime(2026, 2, 17, 10, 17, 32),
            )
            self.trade = SimpleNamespace(order=SimpleNamespace(totalQuantity=100.0))

        def is_completed(self):
            return True

        def is_buy(self):
            return True

        def is_sell(self):
            return False

        def is_rejected(self):
            return False

        def is_canceled(self):
            return False

    class DummyBroker:
        def __init__(self):
            self.proxy = DummyOrderProxy()
            self._order_status_calls = 0

        def convert_order_proxy(self, raw_order):
            return self.proxy

        def on_order_status(self, proxy):
            self._order_status_calls += 1

    class DummyStrategy:
        def __init__(self, broker):
            self.broker = broker

        def notify_order(self, order):
            pass

    pushed_trades = []

    class DummyAlarmManager:
        def push_text(self, content, level='INFO'):
            pass

        def push_trade(self, trade_info):
            pushed_trades.append(trade_info)

    monkeypatch.setattr(engine_module, "AlarmManager", lambda: DummyAlarmManager())

    broker = DummyBroker()
    strategy = DummyStrategy(broker)
    context = SimpleNamespace(strategy_instance=strategy, now=datetime(2026, 2, 17, 10, 17, 0))
    raw_order = SimpleNamespace(statusMsg="filled")

    engine_module.on_order_status_callback(context, raw_order)
    # 模拟同一订单终态后又回调了不同成交快照（签名变化）。
    broker.proxy.executed.price = 10.1
    broker.proxy.executed.value = 404.0
    engine_module.on_order_status_callback(context, raw_order)

    assert len(pushed_trades) == 1, "Filled 终态成交应只推送一次。"
    assert pushed_trades[0]["size"] == 100, "成交推送数量应为最终目标数量(totalQuantity)。"
    assert pushed_trades[0]["symbol"] == "SPY.ARCA"
    assert pushed_trades[0]["dt"] == "2026-02-17T10:17:32", "成交推送时间应优先使用真实执行时间。"


def test_on_order_status_callback_skips_submitted_alarm_for_untracked_external_order(monkeypatch):
    """
    回调告警回归:
    对未被 broker 本地跟踪的外部遗留单，不应推送“订单已提交”告警。
    """
    import live_trader.engine as engine_module

    class DummyOrderProxy:
        def __init__(self):
            self.id = "-8"
            self.status = "Submitted"
            self.data = SimpleNamespace(_name="PSQ.ARCA")
            self.executed = SimpleNamespace(size=0.0, price=0.0, value=0.0, comm=0.0)

        def is_buy(self):
            return True

        def is_sell(self):
            return False

        def is_rejected(self):
            return False

        def is_canceled(self):
            return False

    class DummyBroker:
        def __init__(self):
            self.proxy = DummyOrderProxy()
            self._active_buys = {}
            self._pending_sells = set()

        def convert_order_proxy(self, raw_order):
            return self.proxy

        def on_order_status(self, proxy):
            pass

    class DummyStrategy:
        def __init__(self, broker):
            self.broker = broker

        def notify_order(self, order):
            pass

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

        def push_trade(self, trade_info):
            pass

    monkeypatch.setattr(engine_module, "AlarmManager", lambda: DummyAlarmManager())

    broker = DummyBroker()
    strategy = DummyStrategy(broker)
    context = SimpleNamespace(strategy_instance=strategy, now=datetime(2026, 2, 17, 10, 15, 0))
    raw_order = SimpleNamespace(statusMsg="submitted")

    engine_module.on_order_status_callback(context, raw_order)

    submitted_msgs = [x for x in pushed if "订单已提交" in x["content"]]
    assert submitted_msgs == [], "外部未跟踪订单不应触发“订单已提交”告警。"


def test_on_order_status_callback_pushes_cancelled_alarm(monkeypatch):
    """
    回调告警回归:
    订单进入 Cancelled/ApiCancelled 终态时，应推送撤单通知。
    """
    import live_trader.engine as engine_module

    class DummyOrderProxy:
        def __init__(self):
            self.id = "-8"
            self.status = "Cancelled"
            self.data = SimpleNamespace(_name="PSQ.ARCA")
            self.executed = SimpleNamespace(size=0.0, price=0.0, value=0.0, comm=0.0)
            self.trade = SimpleNamespace(order=SimpleNamespace(totalQuantity=100.0))

        def is_buy(self):
            return True

        def is_sell(self):
            return False

        def is_rejected(self):
            return False

        def is_canceled(self):
            return True

    class DummyBroker:
        def __init__(self):
            self.proxy = DummyOrderProxy()

        def convert_order_proxy(self, raw_order):
            return self.proxy

        def on_order_status(self, proxy):
            pass

    class DummyStrategy:
        def __init__(self, broker):
            self.broker = broker

        def notify_order(self, order):
            pass

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

        def push_trade(self, trade_info):
            pass

    monkeypatch.setattr(engine_module, "AlarmManager", lambda: DummyAlarmManager())

    broker = DummyBroker()
    strategy = DummyStrategy(broker)
    context = SimpleNamespace(strategy_instance=strategy, now=datetime(2026, 2, 17, 10, 18, 0))
    raw_order = SimpleNamespace(statusMsg="cancelled")

    engine_module.on_order_status_callback(context, raw_order)

    cancel_msgs = [x for x in pushed if "订单已撤销" in x["content"]]
    assert len(cancel_msgs) == 1, "撤单终态应推送一次撤单通知。"
    assert "Cancelled" in cancel_msgs[0]["content"]
    assert "PSQ.ARCA" in cancel_msgs[0]["content"]


def test_refresh_live_data_rebases_window_for_daily(monkeypatch):
    """
    实盘日线刷新:
    - 每次应按固定预热窗口全量重拉
    - 不与旧数据 merge，直接替换为本次返回窗口
    - 仍按 ANNUAL_FACTOR 做窗口截断
    """
    import live_trader.engine as engine_module

    initial_idx = pd.to_datetime([
        "2026-02-01", "2026-02-02", "2026-02-03", "2026-02-04",
        "2026-02-05", "2026-02-06", "2026-02-07", "2026-02-08", "2026-02-09",
    ])
    initial_df = pd.DataFrame(
        {
            "open": [10.0] * len(initial_idx),
            "high": [10.5] * len(initial_idx),
            "low": [9.5] * len(initial_idx),
            "close": [10.0] * len(initial_idx),
            "volume": [1000] * len(initial_idx),
        },
        index=initial_idx,
    )

    rebased_idx = pd.to_datetime(["2026-02-08", "2026-02-09", "2026-02-10"])
    rebased_df = pd.DataFrame(
        {
            "open": [18.0, 20.0, 30.0],
            "high": [18.5, 20.5, 30.5],
            "low": [17.5, 19.5, 29.5],
            "close": [18.0, 20.0, 30.0],
            "volume": [1800, 2000, 3000],
        },
        index=rebased_idx,
    )

    class RefreshDataProvider(DummyDataProvider):
        def __init__(self):
            super().__init__()
            self.history_calls = []
            self._counter = 0

        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
            self._counter += 1
            self.history_calls.append(
                {
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "timeframe": timeframe,
                    "compression": compression,
                }
            )
            if self._counter == 1:
                return initial_df.copy()
            return rebased_df.copy()

    monkeypatch.setattr(config, "ANNUAL_FACTOR", 5)
    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, RefreshDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: DummyHeartbeatStrategy,
    )

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 10, 14, 45, 0))
    engine.init(context)

    engine._refresh_live_data(context)

    calls = engine.data_provider.history_calls
    assert len(calls) >= 2, "初始化+刷新至少应触发 2 次 get_history。"
    assert calls[1]["start_date"] == "2026-02-05", "日线刷新应按固定预热窗口全量重拉。"

    refreshed_df = engine.broker.datas[0].p.dataname
    assert refreshed_df.index.min() == pd.Timestamp("2026-02-08"), "日线应使用本次全量窗口替换旧数据。"
    assert refreshed_df.index.max() == pd.Timestamp("2026-02-10"), "刷新后应包含最新 bar。"
    assert refreshed_df.loc[pd.Timestamp("2026-02-09"), "close"] == pytest.approx(20.0), (
        "全量窗口数据应正确生效。"
    )


def test_live_run_skips_when_any_refresh_failed_and_risk_not_checked(monkeypatch):
    """
    实盘刷新质量门控:
    只要任一标的刷新失败，就应跳过策略执行（禁止调仓），且不执行风控检查。
    """
    import live_trader.engine as engine_module

    class FlakyRefreshProvider(DummyDataProvider):
        def __init__(self):
            super().__init__()
            self._symbol_calls = {}

        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
            # 仅让第二个标的在 refresh 阶段失败，验证“部分失败也必须跳过”。
            count = self._symbol_calls.get(symbol, 0) + 1
            self._symbol_calls[symbol] = count
            if count >= 2 and symbol == "SZSE.000001":
                return pd.DataFrame()
            return super().get_history(symbol, start_date, end_date, timeframe, compression)

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, FlakyRefreshProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000", "SZSE.000001"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)
    assert len(engine.broker.datas) == 2, "初始化应加载两路数据，用于刷新质量门控测试。"

    class DummyRisk:
        def __init__(self):
            self.check_calls = 0
            self.exit_triggered = set()

        def check(self, data):
            self.check_calls += 1
            return None

        def notify_order(self, order):
            pass

    engine.risk_control = DummyRisk()
    engine.run(context)

    assert engine.strategy.next_calls == 0, "存在刷新失败时应跳过策略执行。"
    assert engine.risk_control.check_calls == 0, "存在刷新失败时不应执行风控检查。"


def test_refresh_live_minutes_rebase_then_incremental_same_day(monkeypatch):
    """
    实盘分钟刷新:
    - 当天首次 refresh 应执行窗口全量重基准
    - 同日后续 refresh 走增量起点 (last_bar - 3*compression minutes)
    """
    import live_trader.engine as engine_module

    init_idx = pd.to_datetime([
        "2026-02-10 09:50:00",
        "2026-02-10 09:55:00",
        "2026-02-10 10:00:00",
        "2026-02-10 10:05:00",
        "2026-02-10 10:10:00",
    ])
    init_df = pd.DataFrame(
        {
            "open": [10.0, 10.1, 10.2, 10.3, 10.4],
            "high": [10.2, 10.3, 10.4, 10.5, 10.6],
            "low": [9.9, 10.0, 10.1, 10.2, 10.3],
            "close": [10.1, 10.2, 10.3, 10.4, 10.5],
            "volume": [100, 110, 120, 130, 140],
        },
        index=init_idx,
    )

    rebased_idx = pd.to_datetime([
        "2026-02-10 10:00:00",
        "2026-02-10 10:05:00",
        "2026-02-10 10:10:00",
        "2026-02-10 10:15:00",
        "2026-02-10 10:20:00",
    ])
    rebased_df = pd.DataFrame(
        {
            "open": [20.0, 20.1, 20.2, 20.3, 20.4],
            "high": [20.2, 20.3, 20.4, 20.5, 20.6],
            "low": [19.9, 20.0, 20.1, 20.2, 20.3],
            "close": [20.1, 20.2, 20.3, 20.4, 20.5],
            "volume": [200, 210, 220, 230, 240],
        },
        index=rebased_idx,
    )

    incremental_idx = pd.to_datetime([
        "2026-02-10 10:10:00",
        "2026-02-10 10:15:00",
        "2026-02-10 10:20:00",
        "2026-02-10 10:25:00",
    ])
    incremental_df = pd.DataFrame(
        {
            "open": [30.0, 30.1, 30.2, 30.3],
            "high": [30.2, 30.3, 30.4, 30.5],
            "low": [29.9, 30.0, 30.1, 30.2],
            "close": [30.1, 30.2, 30.3, 30.4],
            "volume": [300, 310, 320, 330],
        },
        index=incremental_idx,
    )

    class RefreshDataProvider(DummyDataProvider):
        def __init__(self):
            super().__init__()
            self.history_calls = []
            self._counter = 0

        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
            self._counter += 1
            self.history_calls.append(
                {
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "timeframe": timeframe,
                    "compression": compression,
                }
            )
            if self._counter == 1:
                return init_df.copy()
            if self._counter == 2:
                return rebased_df.copy()
            return incremental_df.copy()

    monkeypatch.setattr(config, "ANNUAL_FACTOR", 5)
    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, RefreshDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: DummyHeartbeatStrategy,
    )

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "timeframe": "Minutes",
        "compression": 5,
        "params": {},
    }

    context = MockContext(now=datetime(2026, 2, 10, 10, 20, 0))
    engine = LiveTrader(cfg)
    engine.init(context)

    # 当天首次 refresh：全量重基准
    engine._refresh_live_data(context)
    # 同日第二次 refresh：增量
    context.now = datetime(2026, 2, 10, 10, 25, 0)
    engine._refresh_live_data(context)

    calls = engine.data_provider.history_calls
    assert len(calls) >= 3, "初始化 + 两次刷新应至少触发 3 次 get_history。"
    assert calls[1]["start_date"] == "2026-02-05 10:20:00", "分钟级当天首刷应使用窗口全量重基准。"
    assert calls[2]["start_date"] == "2026-02-10 10:05:00", "同日后续刷新应回退 3*compression 分钟做增量。"

    refreshed_df = engine.broker.datas[0].p.dataname
    assert refreshed_df.index.max() == pd.Timestamp("2026-02-10 10:25:00"), "分钟级增量后应包含最新 bar。"
    assert refreshed_df.loc[pd.Timestamp("2026-02-10 10:20:00"), "close"] == pytest.approx(30.3), (
        "重复分钟应保留增量新值（keep='last'）。"
    )


def test_live_run_recovers_data_feeds_when_init_has_none(monkeypatch):
    """
    实盘自愈回归:
    若初始化阶段所有标的拉数失败导致 datas 为空，run() 应尝试恢复，
    恢复成功后继续执行策略 next。
    """
    import live_trader.engine as engine_module

    class FlakyDataProvider(DummyDataProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
            self.calls += 1
            if self.calls == 1:
                return pd.DataFrame()
            return super().get_history(symbol, start_date, end_date, timeframe, compression)

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, FlakyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)
    assert len(engine.broker.datas) == 0, "初始化首轮应模拟拉数失败，datas 必须为空。"

    engine.run(context)

    assert len(engine.broker.datas) == 1, "run() 自愈失败：未恢复 data feeds。"
    assert engine.strategy.next_calls == 1, "恢复成功后应继续执行策略 next。"


def test_live_run_skips_when_recovery_still_has_no_data(monkeypatch):
    """
    实盘保护回归:
    若恢复后仍无数据，run() 必须跳过，不得执行策略 next。
    """
    import live_trader.engine as engine_module

    class EmptyDataProvider(DummyDataProvider):
        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:
            return pd.DataFrame()

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, EmptyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)
    assert len(engine.broker.datas) == 0, "初始化应无数据，用于触发恢复保护分支。"

    engine.run(context)

    assert engine.strategy.next_calls == 0, "无数据时应跳过策略执行，防止误报空仓计划。"


def test_live_run_retries_refresh_and_continues_after_recovery(monkeypatch):
    """
    实盘刷新有限重试:
    若首轮 refresh 缺失，但有限次重试内恢复完整，应继续执行本轮策略。
    """
    import live_trader.engine as engine_module

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )
    monkeypatch.setattr(engine_module.time, "sleep", lambda _: None)

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)

    class DummyAlarmManager:
        def __init__(self):
            self.text_calls = []

        def push_text(self, content, level='INFO'):
            self.text_calls.append({"content": content, "level": level})

    dummy_alarm = DummyAlarmManager()
    engine.alarm_manager = dummy_alarm

    refresh_calls = {"count": 0}

    def _flaky_refresh(_ctx):
        refresh_calls["count"] += 1
        if refresh_calls["count"] == 1:
            return {"total_feeds": 2, "updated_feeds": 1, "failed_feeds": 1}
        return {"total_feeds": 2, "updated_feeds": 2, "failed_feeds": 0}

    engine._refresh_live_data = _flaky_refresh

    engine.run(context)

    assert refresh_calls["count"] == 2, "refresh 缺失后应在本轮内进行有限重试。"
    assert engine.strategy.next_calls == 1, "重试恢复完整后应继续执行策略 next。"
    assert dummy_alarm.text_calls == [], "重试恢复成功时不应推送最终失败报警。"


def test_live_run_skips_after_refresh_retry_exhausted(monkeypatch):
    """
    实盘刷新有限重试:
    若多次 refresh 仍存在缺失，应在重试耗尽后跳过本轮并报警一次。
    """
    import live_trader.engine as engine_module

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )
    monkeypatch.setattr(engine_module.time, "sleep", lambda _: None)

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 9, 30, 0))
    engine.init(context)

    class DummyAlarmManager:
        def __init__(self):
            self.text_calls = []

        def push_text(self, content, level='INFO'):
            self.text_calls.append({"content": content, "level": level})

    dummy_alarm = DummyAlarmManager()
    engine.alarm_manager = dummy_alarm

    refresh_calls = {"count": 0}

    def _always_incomplete_refresh(_ctx):
        refresh_calls["count"] += 1
        return {"total_feeds": 2, "updated_feeds": 1, "failed_feeds": 1}

    engine._refresh_live_data = _always_incomplete_refresh

    engine.run(context)

    assert refresh_calls["count"] == engine._LIVE_REFRESH_MAX_ATTEMPTS, "refresh 应只重试固定次数。"
    assert engine.strategy.next_calls == 0, "重试耗尽仍缺失时应跳过策略执行。"
    assert len(dummy_alarm.text_calls) == 1, "重试耗尽后应只推送一次最终失败报警。"
    assert "已重试" in dummy_alarm.text_calls[0]["content"]
    assert dummy_alarm.text_calls[0]["level"] == "ERROR"


def test_live_run_continues_when_overnight_cleanup_barrier_not_cleared_after_retries(monkeypatch):
    """
    隔夜撤单短确认屏障:
    若撤单后在途单始终未清空，应重试 5 次后继续本轮（不跳过）。
    """
    import live_trader.engine as engine_module

    class StickyPendingBroker(MockEngineBroker):
        def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None):
            super().__init__(context, cash_override, commission_override, slippage_override)
            self.cancel_calls = []

        def get_pending_orders(self):
            return [
                {"id": "OVN_1", "symbol": "SHSE.600000", "direction": "BUY", "size": 100}
            ]

        def cancel_pending_order(self, order_id: str) -> bool:
            self.cancel_calls.append(order_id)
            return True

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (StickyPendingBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )
    monkeypatch.setattr(engine_module.time, "sleep", lambda _: None)

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 3, 3, 9, 30, 0))
    engine.init(context)

    class DummyAlarmManager:
        def __init__(self):
            self.text_calls = []

        def push_text(self, content, level='INFO'):
            self.text_calls.append({"content": content, "level": level})

    dummy_alarm = DummyAlarmManager()
    engine.alarm_manager = dummy_alarm

    refresh_calls = {"count": 0}

    def _fake_refresh(_ctx):
        refresh_calls["count"] += 1
        return {"total_feeds": 1, "updated_feeds": 1, "failed_feeds": 0}

    engine._refresh_live_data = _fake_refresh

    engine.run(context)

    assert refresh_calls["count"] == 1, "即使隔夜在途未清空，重试后也应继续拉数流程。"
    assert engine.strategy.next_calls == 1, "即使隔夜在途未清空，重试后也应执行策略 next。"
    assert engine._overnight_cleanup_done_on == context.now.date(), "重试结束后应标记当日 cleanup 已完成。"
    assert len(engine.broker.cancel_calls) == 5, "在途未清空时应重试撤单 5 次。"
    assert len(dummy_alarm.text_calls) == 1, "5 次后仍未清空应触发一次失败报警。"
    assert dummy_alarm.text_calls[0]["level"] == "ERROR", "失败报警级别应为 ERROR。"
    assert "Overnight cleanup not fully cleared" in dummy_alarm.text_calls[0]["content"], (
        "失败报警内容应包含 cleanup 未完成信息。"
    )


def test_live_run_continues_after_overnight_cleanup_barrier_clears(monkeypatch):
    """
    隔夜撤单短确认屏障:
    若短确认窗口内在途单已清空，run() 应继续拉数并执行策略，且同日不重复清理。
    """
    import live_trader.engine as engine_module

    class ClearingPendingBroker(MockEngineBroker):
        def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None):
            super().__init__(context, cash_override, commission_override, slippage_override)
            self.cancel_calls = []
            self.sync_calls = 0
            self._pending_calls = 0

        def get_pending_orders(self):
            self._pending_calls += 1
            if self._pending_calls <= 2:
                return [
                    {"id": "OVN_1", "symbol": "SHSE.600000", "direction": "BUY", "size": 100}
                ]
            return []

        def cancel_pending_order(self, order_id: str) -> bool:
            self.cancel_calls.append(order_id)
            return True

        def sync_balance(self):
            self.sync_calls += 1
            return super().sync_balance()

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (ClearingPendingBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )
    monkeypatch.setattr(engine_module.time, "sleep", lambda _: None)

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 3, 3, 9, 30, 0))
    engine.init(context)

    refresh_calls = {"count": 0}

    def _fake_refresh(_ctx):
        refresh_calls["count"] += 1
        return {"total_feeds": 1, "updated_feeds": 1, "failed_feeds": 0}

    engine._refresh_live_data = _fake_refresh

    engine.run(context)

    assert refresh_calls["count"] == 1, "屏障通过后应继续拉数。"
    assert engine.strategy.next_calls == 1, "屏障通过后应执行策略 next。"
    assert engine.broker.cancel_calls == ["OVN_1"], "隔夜清理应尝试撤单。"
    assert engine.broker.sync_calls == 1, "发起撤单后应同步一次资金。"
    assert engine._overnight_cleanup_done_on == context.now.date(), "屏障通过后应标记当日 cleanup 完成。"

    # 同一自然日再次 run 不应重复清理隔夜单。
    context.now = datetime(2026, 3, 3, 10, 30, 0)
    engine.run(context)
    assert engine.broker.cancel_calls == ["OVN_1"], "同日二次 run 不应重复执行隔夜撤单。"


def test_pending_order_barrier_rejects_untrusted_empty_snapshot(monkeypatch):
    """
    空 pending 快照语义:
    若 broker 标记本次在途查询失败，即使返回 []，屏障也不能视为已清空。
    """
    import live_trader.engine as engine_module

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }
    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 3, 3, 9, 30, 0))
    engine.init(context)

    def _untrusted_empty_pending():
        engine.broker._last_pending_orders_fetch_failed = True
        engine.broker._last_pending_orders_fetch_error = "pending snapshot unavailable"
        return []

    engine.broker.get_pending_orders = _untrusted_empty_pending

    assert engine._confirm_pending_orders_cleared(max_checks=1, sleep_seconds=0.0) is False


def test_stale_strategy_order_not_cleared_when_pending_snapshot_untrusted(monkeypatch):
    """
    僵尸 order 自愈边界:
    pending 查询失败时不能把空列表当作柜台无在途，否则可能误清 strategy.order。
    """
    import live_trader.engine as engine_module

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
        "KEEP_OVERNIGHT_ORDERS": True,
    }
    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 3, 3, 9, 30, 0))
    engine.init(context)
    engine._refresh_live_data = lambda _ctx: {"total_feeds": 1, "updated_feeds": 1, "failed_feeds": 0}

    stale_order = MockOrderProxy("STALE_1", is_buy_order=True, status="Submitted")
    engine.strategy.order = stale_order

    def _untrusted_empty_pending():
        engine.broker._last_pending_orders_fetch_failed = True
        engine.broker._last_pending_orders_fetch_error = "pending snapshot unavailable"
        return []

    engine.broker.get_pending_orders = _untrusted_empty_pending
    engine.run(context)

    assert engine.strategy.order is stale_order
    assert engine.strategy.next_calls == 0


def test_live_run_skips_overnight_cleanup_when_keep_overnight_orders_true(monkeypatch):
    """
    KEEP_OVERNIGHT_ORDERS=True:
    应跳过隔夜清理流程，不调用 cleanup_overnight_orders。
    """
    import live_trader.engine as engine_module

    class KeepOvernightBroker(MockEngineBroker):
        def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None):
            super().__init__(context, cash_override, commission_override, slippage_override)
            self.cleanup_calls = 0

        def cleanup_overnight_orders(self):
            self.cleanup_calls += 1
            return {"total": 1, "canceled": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (KeepOvernightBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
        "KEEP_OVERNIGHT_ORDERS": True,
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 3, 3, 9, 30, 0))
    engine.init(context)

    refresh_calls = {"count": 0}

    def _fake_refresh(_ctx):
        refresh_calls["count"] += 1
        return {"total_feeds": 1, "updated_feeds": 1, "failed_feeds": 0}

    engine._refresh_live_data = _fake_refresh

    engine.run(context)

    assert refresh_calls["count"] == 1, "保留隔夜单时，本轮仍应继续拉数。"
    assert engine.strategy.next_calls == 1, "保留隔夜单时，本轮仍应继续执行策略。"
    assert engine.broker.cleanup_calls == 0, "KEEP_OVERNIGHT_ORDERS=True 时不应触发隔夜清理。"
    assert engine._overnight_cleanup_done_on is None, "跳过清理时不应写入 cleanup 执行日期。"


def test_live_run_continues_and_alarms_when_cleanup_raises(monkeypatch):
    """
    隔夜清理异常:
    cleanup_overnight_orders 抛错时应重试并最终告警，但不中断本轮执行。
    """
    import live_trader.engine as engine_module

    class CleanupErrorBroker(MockEngineBroker):
        def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None):
            super().__init__(context, cash_override, commission_override, slippage_override)
            self.cleanup_calls = 0

        def cleanup_overnight_orders(self):
            self.cleanup_calls += 1
            raise RuntimeError("cleanup failed")

        def get_pending_orders(self):
            return [
                {"id": "OVN_X", "symbol": "SHSE.600000", "direction": "BUY", "size": 100}
            ]

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (CleanupErrorBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )
    monkeypatch.setattr(engine_module.time, "sleep", lambda _: None)

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 3, 3, 9, 30, 0))
    engine.init(context)

    class DummyAlarmManager:
        def __init__(self):
            self.text_calls = []

        def push_text(self, content, level='INFO'):
            self.text_calls.append({"content": content, "level": level})

    dummy_alarm = DummyAlarmManager()
    engine.alarm_manager = dummy_alarm

    refresh_calls = {"count": 0}

    def _fake_refresh(_ctx):
        refresh_calls["count"] += 1
        return {"total_feeds": 1, "updated_feeds": 1, "failed_feeds": 0}

    engine._refresh_live_data = _fake_refresh

    engine.run(context)

    assert engine.broker.cleanup_calls == 5, "cleanup 异常场景应重试 5 次。"
    assert refresh_calls["count"] == 1, "cleanup 异常不应阻断本轮拉数。"
    assert engine.strategy.next_calls == 1, "cleanup 异常不应阻断本轮策略执行。"
    assert engine._overnight_cleanup_done_on == context.now.date(), "异常重试结束后应标记当日 cleanup 已完成。"
    assert len(dummy_alarm.text_calls) == 1, "5 次后仍失败应推送一次 ERROR 报警。"
    assert dummy_alarm.text_calls[0]["level"] == "ERROR"
    assert "Overnight cleanup not fully cleared" in dummy_alarm.text_calls[0]["content"]


def test_live_run_continues_when_cleanup_hook_unexpectedly_raises(monkeypatch):
    """
    隔夜清理入口异常:
    _cleanup_overnight_orders_before_refresh 自身抛错时应告警并继续本轮执行。
    """
    import live_trader.engine as engine_module

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: CounterStrategy,
    )

    cfg = {
        "strategy_name": "CounterStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 3, 3, 9, 30, 0))
    engine.init(context)

    class DummyAlarmManager:
        def __init__(self):
            self.text_calls = []

        def push_text(self, content, level='INFO'):
            self.text_calls.append({"content": content, "level": level})

    dummy_alarm = DummyAlarmManager()
    engine.alarm_manager = dummy_alarm

    refresh_calls = {"count": 0}

    def _fake_refresh(_ctx):
        refresh_calls["count"] += 1
        return {"total_feeds": 1, "updated_feeds": 1, "failed_feeds": 0}

    def _raise_cleanup_hook(_ctx):
        raise RuntimeError("cleanup hook crashed")

    engine._refresh_live_data = _fake_refresh
    monkeypatch.setattr(engine, "_cleanup_overnight_orders_before_refresh", _raise_cleanup_hook)

    engine.run(context)

    assert refresh_calls["count"] == 1, "清理入口异常不应阻断本轮拉数。"
    assert engine.strategy.next_calls == 1, "清理入口异常不应阻断本轮策略执行。"
    assert len(dummy_alarm.text_calls) == 1, "清理入口异常应推送一次 ERROR 报警。"
    assert dummy_alarm.text_calls[0]["level"] == "ERROR"
    assert "Unexpected error in overnight cleanup hook" in dummy_alarm.text_calls[0]["content"]


def test_risk_unknown_status_blocks_but_not_crash(monkeypatch):
    """
    风控未知状态:
    应阻断策略继续执行（返回 triggered_action=True），但不崩溃且保留待跟踪订单。
    """
    import live_trader.engine as engine_module

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(
        engine_module,
        "get_class_from_name",
        lambda class_name, paths: DummyHeartbeatStrategy,
    )

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 10, 30, 0))
    engine.init(context)
    engine.broker.set_datetime(context.now)

    class DummyRisk:
        def __init__(self):
            self.check_calls = 0
            self.exit_triggered = set()

        def check(self, data):
            self.check_calls += 1
            return None

        def notify_order(self, order):
            pass

    class UnknownOrder:
        def is_pending(self):
            return False

        def is_accepted(self):
            return False

        def is_completed(self):
            return False

        def is_rejected(self):
            return False

        def is_canceled(self):
            return False

    engine.risk_control = DummyRisk()
    symbol = engine.broker.datas[0]._name
    engine._pending_risk_orders = {symbol: UnknownOrder()}

    triggered = engine._check_risk_controls()

    assert triggered is True, "未知订单状态应触发阻断，避免策略继续下单。"
    assert symbol in engine._pending_risk_orders, "未知状态下不应误删待跟踪风险订单。"
    assert engine.risk_control.check_calls == 0, "未知状态时应先阻断，不应继续执行风险 check。"


def test_live_engine_supports_multi_risk_chain_without_fail_open(monkeypatch):
    """
    实盘多风控回归:
    risk='A,B' 时应加载为链式风控，不得因为逗号字符串导致整体失效(None)。
    """
    import live_trader.engine as engine_module

    class NoopRisk(BaseRiskControl):
        def __init__(self, broker, params=None):
            super().__init__(broker, params)
            self.check_calls = 0

        def check(self, data):
            self.check_calls += 1
            return None

    class SellRisk(BaseRiskControl):
        def __init__(self, broker, params=None):
            super().__init__(broker, params)
            self.check_calls = 0

        def check(self, data):
            self.check_calls += 1
            return "SELL"

    def _resolver(class_name, paths):
        mapping = {
            "DummyHeartbeatStrategy": DummyHeartbeatStrategy,
            "NoopRisk": NoopRisk,
            "SellRisk": SellRisk,
        }
        return mapping[class_name]

    monkeypatch.setattr(
        engine_module.LiveTrader,
        "_load_adapter_classes",
        lambda self, platform: (MockEngineBroker, DummyDataProvider),
    )
    monkeypatch.setattr(engine_module, "get_class_from_name", _resolver)

    cfg = {
        "strategy_name": "DummyHeartbeatStrategy",
        "platform": "mock_engine",
        "symbols": ["SHSE.600000"],
        "cash": 100000.0,
        "params": {},
        "risk": "NoopRisk,SellRisk",
        "risk_params": {},
    }

    engine = LiveTrader(cfg)
    context = MockContext(now=datetime(2026, 2, 17, 10, 30, 0))
    engine.init(context)
    engine.broker.set_datetime(context.now)

    assert engine.risk_control is not None, "多风控配置不应退化为无风控。"
    controls = getattr(engine.risk_control, "controls", None)
    assert controls is not None and len(controls) == 2, "多风控应构建链式对象并加载全部模块。"

    triggered = engine._check_risk_controls()

    assert triggered is True, "链式风控中任一模块触发 SELL 时应阻断策略并执行风控动作。"
    assert controls[0].check_calls >= 1, "链式风控第1个模块未被执行。"
    assert controls[1].check_calls >= 1, "链式风控第2个模块未被执行。"
    sell_orders = [o for o in engine.broker.submitted_orders if o["side"] == "SELL"]
    assert sell_orders, "风控触发后应实际发出 SELL 平仓单。"


def test_launch_live_prefers_connection_alarm_window_over_global_default(monkeypatch):
    """
    启动器报警窗口优先级:
    连接配置中的 alarm_window 应覆盖 config.LIVE_SCHEDULE_ALARM_WINDOW。
    """
    import live_trader.engine as engine_module

    captured = {}
    module_name = "live_trader.adapters.fake_broker"
    fake_module = ModuleType(module_name)

    class FakeBroker(engine_module.BaseLiveBroker):
        @classmethod
        def launch(cls, conn_cfg, strategy_path, params, **kwargs):
            captured["launch"] = {
                "conn_cfg": conn_cfg,
                "strategy_path": strategy_path,
                "params": params,
                "kwargs": kwargs,
            }

    FakeBroker.__module__ = module_name
    fake_module.FakeBroker = FakeBroker

    class DummyAlarmManager:
        def set_runtime_context(self, **kwargs):
            captured["runtime_context"] = kwargs

        def push_exception(self, context, error):
            captured["exception"] = {"context": context, "error": str(error)}

    dummy_alarm = DummyAlarmManager()

    monkeypatch.setattr(
        config,
        "BROKER_ENVIRONMENTS",
        {
            "fake_broker": {
                "real": {
                    "schedule": "1d:15:45:00",
                    "timezone": "America/New_York",
                    "alarm_window": "5m:30s",
                }
            }
        },
    )
    monkeypatch.setattr(config, "LIVE_SCHEDULE_ALARM_WINDOW", "30m:15m")
    monkeypatch.setattr(
        engine_module.importlib,
        "import_module",
        lambda import_name: fake_module if import_name == module_name else None,
    )
    monkeypatch.setattr(engine_module, "AlarmManager", lambda: dummy_alarm)

    engine_module.launch_live(
        broker_name="fake_broker",
        conn_name="real",
        strategy_path="strategies.my_strategy",
        params={"lookback": 20},
        symbols=["QQQ"],
    )

    assert "exception" not in captured, "连接级 alarm_window 覆盖不应导致 launch 崩溃。"
    assert captured["runtime_context"]["alarm_window"] == "5m:30s"
    assert captured["runtime_context"]["schedule_rule"] == "1d:15:45:00"
    assert captured["runtime_context"]["schedule_timezone"] == "America/New_York"

