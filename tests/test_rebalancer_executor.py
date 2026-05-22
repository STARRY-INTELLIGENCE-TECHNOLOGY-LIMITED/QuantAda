from types import SimpleNamespace


def test_calculate_plan_pushes_plan_via_semantic_tag(monkeypatch):
    import common.rebalancer as rebalancer_module

    pushed = []

    class DummyData:
        def __init__(self, name):
            self._name = name

    class DummyAlarmManager:
        def push_plan(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(rebalancer_module, "AlarmManager", lambda: DummyAlarmManager())
    monkeypatch.setattr(rebalancer_module.config, "PRINT_PLAN", True)
    monkeypatch.setattr(rebalancer_module.config, "LOG", False)

    current_positions = {DummyData("SPY.ARCA"): 50000.0}
    target_symbols = [DummyData("QQQ.NASDAQ")]
    plan = rebalancer_module.PortfolioRebalancer.calculate_plan(
        current_positions=current_positions,
        target_symbols=target_symbols,
        total_capital=100000.0,
        select_top_k=1,
        rebalance_threshold=0.05,
    )

    assert plan["sell_clear"], "应正常生成调仓计划。"
    assert len(pushed) == 1, "PRINT_PLAN=True 时应通过 push_plan 推送计划。"
    assert "调仓计划生成" in pushed[0]["content"]


def test_order_executor_waits_for_sell_settlement_then_buys(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            return object()

        def get_pending_orders(self):
            if clock["t"] < 3.0:
                return [{"id": "S1", "symbol": "SPY.ARCA", "direction": "SELL", "size": 100}]
            return []

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)], "应先卖后买，且卖单终态后再买入。"
    assert clock["t"] >= 3.0, "应等待卖单终态。"
    assert broker.sync_calls == 1, "卖单终态后应同步资金。"


def test_order_executor_waits_beyond_60s_until_sell_settles_then_buys(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            return object()

        def get_pending_orders(self):
            if clock["t"] < 106.0:
                return [{"id": "S1", "symbol": "SPY.ARCA", "direction": "SELL", "size": 100}]
            return []

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)], "卖单未终态时应持续等待，终态后再买入。"
    assert clock["t"] >= 106.0, "卖单等待不应受 60 秒上限限制。"
    assert broker.sync_calls == 1, "卖单终态后应同步资金。"
    assert len(pushed) == 0, "未达到 5 分钟告警阈值时不应推送消息。"


def test_order_executor_warns_after_5m_but_keeps_waiting_until_sell_settles(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            return object()

        def get_pending_orders(self):
            if clock["t"] < 306.0:
                return [{"id": "S1", "symbol": "SPY.ARCA", "direction": "SELL", "size": 100}]
            return []

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)], "超过 5 分钟后仍应继续等待终态并在完成后买入。"
    assert clock["t"] >= 306.0, "超过 5 分钟告警后不应停止等待。"
    assert broker.sync_calls == 1, "卖单终态后应同步资金。"
    assert len(pushed) == 1, "超过 5 分钟应推送一次告警。"
    assert pushed[0]["level"] == "WARNING"
    assert "300 秒内未全部终态" in pushed[0]["content"]
    assert "继续等待并按已确认现金滚动买入" in pushed[0]["content"]


def test_order_executor_clears_local_pending_sell_when_remote_sell_empty(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0
            self._pending_sells = set()

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if float(target) == 0.0:
                self._pending_sells.add("SELL_LOCAL_1")
                return SimpleNamespace(id="SELL_LOCAL_1")
            return object()

        def get_pending_orders(self):
            # 模拟终态回调被当前调度阻塞，但柜台在途单已无本轮 SELL。
            return []

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)], "远端 SELL 在途连续为空时，应清理本轮本地滞后 pending 并继续买入。"
    assert clock["t"] < 300.0, "不应因为本地回调滞后等待到 300 秒并误报警。"
    assert broker.sync_calls == 1
    assert pushed == []
    assert "SELL_LOCAL_1" not in broker._pending_sells


def test_order_executor_backtest_executes_plan_without_waiting(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}
    sleep_calls = []

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        sleep_calls.append(seconds)
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    class DummyBacktestBroker:
        is_live = False

        def __init__(self):
            self.calls = []
            self.pending_calls = 0
            self.sync_calls = 0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            return object()

        def get_pending_orders(self):
            self.pending_calls += 1
            raise AssertionError("回测路径不应查询在途订单。")

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBacktestBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)]
    assert broker.pending_calls == 0, "回测必须按计划执行，不应走实盘 pending 确认。"
    assert broker.sync_calls == 0, "回测不应触发实盘资金同步。"
    assert sleep_calls == [], "回测不应进入任何等待循环。"


def test_order_executor_reconciles_missing_sell_terminal_by_live_position(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0
            self._pending_sells = set()
            self.position_size = 100

        def get_position(self, data):
            return SimpleNamespace(size=self.position_size)

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if float(target) == 0.0:
                self.position_size = 0
                self._pending_sells.add("SELL_REAL_1")
                return SimpleNamespace(
                    id="SELL_REAL_1",
                    platform_order=SimpleNamespace(volume=100),
                )
            return object()

        def get_pending_orders(self):
            return []

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)], "应通过实时持仓一致性确认卖单已完成后继续买入。"
    assert broker.sync_calls == 1, "卖单终态缺失但持仓已归零时，应同步资金并继续执行。"
    assert "SELL_REAL_1" not in broker._pending_sells, "卖单终态缺失时应清理已被实时持仓确认完成的本地 pending 状态。"


def test_order_executor_stops_when_sell_timeout_and_position_not_reached(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0
            self._pending_sells = {"SELL_STUCK_1"}
            self.position_size = 100

        def get_position(self, data):
            return SimpleNamespace(size=self.position_size)

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if float(target) == 0.0:
                return SimpleNamespace(
                    id="SELL_STUCK_1",
                    platform_order=SimpleNamespace(volume=100),
                )
            return object()

        def get_pending_orders(self):
            return [{"id": "SELL_STUCK_1", "symbol": "SPY.ARCA", "direction": "SELL", "size": 100}]

        def get_rebalance_cash(self):
            return 0.0

        def get_current_price(self, data):
            return 1.0

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0)], "卖单超时且无已确认现金时不应继续买入。"
    assert broker.sync_calls == 1, "硬等待结束后应尝试同步资金，但不得全量放行买入。"
    assert len(pushed) >= 1, "超时失败应至少推送一次错误告警。"
    assert any(item["level"] == "ERROR" for item in pushed)


def test_order_executor_does_not_clear_sell_when_empty_pending_snapshot_untrusted(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0
            self._pending_sells = {"SELL_UNTRUSTED_1"}
            self._last_pending_orders_fetch_failed = False
            self._last_pending_orders_fetch_error = None

        def get_position(self, data):
            return SimpleNamespace(size=100, price=1.0)

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if float(target) == 0.0:
                return SimpleNamespace(
                    id="SELL_UNTRUSTED_1",
                    platform_order=SimpleNamespace(volume=100),
                )
            return object()

        def get_pending_orders(self):
            self._last_pending_orders_fetch_failed = True
            self._last_pending_orders_fetch_error = "broker pending query failed"
            return []

        def get_rebalance_cash(self):
            return 0.0

        def get_current_price(self, data):
            return 1.0

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_WARN_SECONDS = 1.0
    executor._SELL_SETTLE_HARD_SECONDS = 3.0
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0)], "查询结果不可信时，不应把空 pending 快照当作卖单完成。"
    assert "SELL_UNTRUSTED_1" in broker._pending_sells
    assert any(item["level"] == "ERROR" for item in pushed)


def test_order_executor_does_not_full_release_untracked_sell_when_pending_query_fails(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if float(target) == 0.0:
                return SimpleNamespace(id="")
            return object()

        def get_pending_orders(self):
            raise RuntimeError("broker unavailable")

        def get_rebalance_cash(self):
            return 0.0

        def get_position(self, data):
            return SimpleNamespace(size=0, price=1.0)

        def get_current_price(self, data):
            return 1.0

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_WARN_SECONDS = 2.0
    executor._SELL_SETTLE_HARD_SECONDS = 5.0
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0)], "无 ID 卖单且在途查询失败时，不应判定卖单已清空并全量买入。"
    assert broker.sync_calls == 1
    assert any(item["level"] == "ERROR" for item in pushed)


def test_order_executor_hard_wait_returns_so_later_schedule_can_run(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pass

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self._pending_sells = {"SELL_STUCK_1"}
            self.position_size = 100

        def get_position(self, data):
            return SimpleNamespace(size=self.position_size, price=1.0)

        def get_current_price(self, data):
            return 1.0

        def get_rebalance_cash(self):
            return 0.0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if float(target) == 0.0:
                return SimpleNamespace(id="SELL_STUCK_1", platform_order=SimpleNamespace(volume=100))
            return object()

        def get_pending_orders(self):
            if self._pending_sells:
                return [{"id": "SELL_STUCK_1", "symbol": "SPY.ARCA", "direction": "SELL", "size": 100}]
            return []

        def sync_balance(self):
            pass

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_WARN_SECONDS = 1.0
    executor._SELL_SETTLE_HARD_SECONDS = 3.0

    stuck_plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }
    executor.execute_plan(stuck_plan)

    assert broker.calls == [("SPY.ARCA", 0.0)], "卖单硬等待后必须返回，不能永久占住 schedule 回调。"
    assert clock["t"] >= 3.0

    broker._pending_sells.clear()
    next_plan = {
        "sell_clear": [],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }
    executor.execute_plan(next_plan)

    assert broker.calls[-1] == ("EWJ.ARCA", 100000.0), "前一轮硬等待返回后，后续调度应仍能继续执行。"


def test_order_executor_rolls_buys_with_confirmed_cash_without_full_release(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    buy_data = SimpleNamespace(_name="EWJ.ARCA")
    sell_data = SimpleNamespace(_name="SPY.ARCA")

    class DummyBroker:
        safety_multiplier = 1.0

        def __init__(self):
            self.calls = []
            self.sync_calls = 0
            self._pending_sells = {"SELL_STUCK_1"}
            self._active_buys = {}
            self.positions = {"SPY.ARCA": 100, "EWJ.ARCA": 0}
            self.cash_by_time = [(0.0, 0.0), (2.0, 30000.0), (4.0, 60000.0)]
            self.pending_buy_seen = False

        def get_position(self, data):
            return SimpleNamespace(size=self.positions.get(data._name, 0), price=1.0)

        def get_current_price(self, data):
            return 1.0

        def get_rebalance_cash(self):
            cash = 0.0
            for threshold, value in self.cash_by_time:
                if clock["t"] >= threshold:
                    cash = value
            return cash

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if data._name == "SPY.ARCA":
                return SimpleNamespace(id="SELL_STUCK_1", platform_order=SimpleNamespace(volume=100))
            self._active_buys[f"BUY_{len(self._active_buys) + 1}"] = {"data": data}
            return object()

        def get_pending_orders(self):
            orders = [{"id": "SELL_STUCK_1", "symbol": "SPY.ARCA", "direction": "SELL", "size": 100}]
            if self._active_buys and not self.pending_buy_seen:
                self.pending_buy_seen = True
                orders.append({"id": "BUY_1", "symbol": "EWJ", "direction": "BUY", "size": 30000})
            elif self._active_buys:
                self._active_buys.clear()
                self.positions["EWJ.ARCA"] = 30000
            return orders

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_WARN_SECONDS = 2.0
    executor._SELL_SETTLE_HARD_SECONDS = 5.0
    plan = {
        "sell_clear": [sell_data],
        "reduce": [],
        "increase": [(buy_data, 100000.0)],
    }

    executor.execute_plan(plan)

    assert ("EWJ.ARCA", 30000.0) in broker.calls, "应在现金部分确认后先滚动买入一部分。"
    assert ("EWJ.ARCA", 100000.0) not in broker.calls, "卖出未被持仓确认时不应全量释放目标买入。"
    assert broker.calls.count(("EWJ.ARCA", 30000.0)) == 1, "同一段已释放买入不应在等待期间重复提交。"
    assert any(item["level"] == "ERROR" for item in pushed), "硬等待结束仍未确认卖出时应告警。"


def test_order_executor_warns_and_skips_buys_when_sell_not_submitted(monkeypatch):
    import common.order_executor as executor_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if data._name == "SPY.ARCA":
                return None
            return object()

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0)], "卖单未提交时不应继续买入。"
    assert len(pushed) == 2, "应分别推送卖单未提交与跳过买入告警。"
    assert all(item["level"] == "ERROR" for item in pushed)
    assert "SELL order not submitted" in pushed[0]["content"]
    assert "Planned BUY orders are skipped" in pushed[1]["content"]


def test_order_executor_warns_when_buy_not_submitted(monkeypatch):
    import common.order_executor as executor_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            return None

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("EWJ.ARCA", 100000.0)]
    assert len(pushed) == 1
    assert pushed[0]["level"] == "ERROR"
    assert "BUY order not submitted" in pushed[0]["content"]
