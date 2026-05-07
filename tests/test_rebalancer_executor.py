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
    import common.rebalancer as rebalancer_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(rebalancer_module.time, "time", _fake_time)
    monkeypatch.setattr(rebalancer_module.time, "sleep", _fake_sleep)

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
    executor = rebalancer_module.OrderExecutor(broker)
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
    import common.rebalancer as rebalancer_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(rebalancer_module.time, "time", _fake_time)
    monkeypatch.setattr(rebalancer_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(rebalancer_module, "AlarmManager", lambda: DummyAlarmManager())

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
    executor = rebalancer_module.OrderExecutor(broker)
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
    import common.rebalancer as rebalancer_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(rebalancer_module.time, "time", _fake_time)
    monkeypatch.setattr(rebalancer_module.time, "sleep", _fake_sleep)

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(rebalancer_module, "AlarmManager", lambda: DummyAlarmManager())

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
    executor = rebalancer_module.OrderExecutor(broker)
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
    assert "继续等待，不据此跳过买入" in pushed[0]["content"]


def test_order_executor_waits_local_pending_sells_even_if_remote_empty(monkeypatch):
    import common.rebalancer as rebalancer_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)
        if clock["t"] >= 2.0:
            broker._pending_sells.clear()

    monkeypatch.setattr(rebalancer_module.time, "time", _fake_time)
    monkeypatch.setattr(rebalancer_module.time, "sleep", _fake_sleep)

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
            # 模拟券商 open orders 可见性延迟：短时间内返回空。
            return []

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = rebalancer_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)], "本地 pending_sells 未清空前不应放行买单。"
    assert clock["t"] >= 2.0, "应等待本地 pending_sells 进入终态。"
    assert broker.sync_calls == 1


def test_order_executor_warns_and_skips_buys_when_sell_not_submitted(monkeypatch):
    import common.rebalancer as rebalancer_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(rebalancer_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if data._name == "SPY.ARCA":
                return None
            return object()

    broker = DummyBroker()
    executor = rebalancer_module.OrderExecutor(broker)
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
    import common.rebalancer as rebalancer_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(rebalancer_module, "AlarmManager", lambda: DummyAlarmManager())

    class DummyBroker:
        def __init__(self):
            self.calls = []

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            return None

    broker = DummyBroker()
    executor = rebalancer_module.OrderExecutor(broker)
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
