from types import SimpleNamespace


def test_calculate_plan_does_not_push_without_runtime_context(monkeypatch):
    import common.rebalancer as rebalancer_module

    class DummyData:
        def __init__(self, name):
            self._name = name

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


def test_calculate_plan_uses_data_identity_not_overloaded_equality(monkeypatch):
    import common.rebalancer as rebalancer_module

    monkeypatch.setattr(rebalancer_module.config, "PRINT_PLAN", False)

    class WeirdComparableData:
        def __init__(self, name):
            self._name = name

        def __eq__(self, other):
            return True

        def __hash__(self):
            return 1

    held = WeirdComparableData("AAA")
    target_a = WeirdComparableData("BBB")
    target_b = WeirdComparableData("CCC")

    plan = rebalancer_module.PortfolioRebalancer.calculate_plan(
        current_positions={held: 1000.0},
        target_symbols=[target_a, target_b],
        total_capital=1000.0,
        select_top_k=2,
        rebalance_threshold=0.0,
    )

    assert plan["sell_clear"] == [held]
    assert plan["reduce"] == []
    assert plan["increase"] == [(target_a, 500.0), (target_b, 500.0)]


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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

    class DummyBroker:
        def __init__(self):
            self.calls = []
            self.sync_calls = 0
            self._pending_sells = set()

        def get_position(self, data):
            return SimpleNamespace(size=0, price=1.0)

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


def test_order_executor_tracks_all_split_sell_ids_and_total_cash_release(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    class DummyBroker:
        is_live = True

        def __init__(self):
            self.calls = []
            self._pending_sells = set()

        def get_position(self, data):
            size = 1_486_700 if data._name == "SHSE.512010" and clock["t"] < 1.0 else 0
            return SimpleNamespace(size=size, price=0.37)

        def get_current_price(self, data):
            return 0.37 if data._name == "SHSE.512010" else 1.13

        def get_rebalance_cash(self):
            return 14.09 if clock["t"] < 3.0 else 550_100.0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target), clock["t"]))
            if data._name == "SHSE.512010":
                self._pending_sells.update({"SELL_1", "SELL_2"})
                return SimpleNamespace(
                    id="SELL_1",
                    batch_order_ids=("SELL_1", "SELL_2"),
                    submitted_size=1_000_000,
                    batch_submitted_size=1_486_700,
                    batch_submit_failed=False,
                )
            return object()

        def get_pending_orders(self):
            return []

        def sync_balance(self):
            return None

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_POLL_SECONDS = 1.0
    executor._POST_SELL_CASH_POLL_SECONDS = 1.0
    executor._POST_SELL_CASH_WAIT_SECONDS = 5.0
    plan = {
        "sell_clear": [SimpleNamespace(_name="SHSE.512010")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="SHSE.513050"), 554_553.19)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [
        ("SHSE.512010", 0.0, 0.0),
        ("SHSE.513050", 554_553.19, 3.0),
    ]
    assert broker._pending_sells == set()


def test_order_executor_blocks_buy_when_sell_ids_disappear_but_position_misses_target(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}
    monkeypatch.setattr(executor_module.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        executor_module.time,
        "sleep",
        lambda seconds: clock.__setitem__("t", clock["t"] + float(seconds)),
    )
    pushed = []
    monkeypatch.setattr(
        executor_module.runtime_notifications,
        "push_text",
        lambda content, level="INFO": pushed.append({"content": content, "level": level}),
    )

    class DummyBroker:
        is_live = True

        def __init__(self):
            self.calls = []
            self._pending_sells = set()
            self._last_pending_orders_fetch_failed = False

        def get_position(self, data):
            size = 1_486_700 if not self.calls else 486_700
            return SimpleNamespace(size=size, price=0.37)

        def get_current_price(self, data):
            return 0.37 if data._name == "SHSE.512010" else 1.13

        def get_rebalance_cash(self):
            return 550_100.0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if data._name == "SHSE.512010":
                return SimpleNamespace(
                    id="SELL_1",
                    batch_order_ids=("SELL_1", "SELL_2"),
                    batch_submitted_size=1_486_700,
                    batch_submit_failed=False,
                )
            return object()

        def get_pending_orders(self):
            return []

        def sync_balance(self):
            return None

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_WARN_SECONDS = 1.0
    executor._SELL_SETTLE_HARD_SECONDS = 3.0
    plan = {
        "sell_clear": [SimpleNamespace(_name="SHSE.512010")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="SHSE.513050"), 554_553.19)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SHSE.512010", 0.0)]
    assert pushed and pushed[-1]["level"] == "ERROR"
    assert "did not reach broker position targets" in pushed[-1]["content"]


def test_order_executor_waits_for_accepted_sell_children_after_partial_batch_failure(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}
    monkeypatch.setattr(executor_module.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        executor_module.time,
        "sleep",
        lambda seconds: clock.__setitem__("t", clock["t"] + float(seconds)),
    )
    pushed = []
    monkeypatch.setattr(
        executor_module.runtime_notifications,
        "push_text",
        lambda content, level="INFO": pushed.append({"content": content, "level": level}),
    )

    class DummyBroker:
        is_live = True

        def __init__(self):
            self.calls = []
            self.pending_reads = 0
            self._pending_sells = {"SELL_1"}
            self._last_pending_orders_fetch_failed = False

        def get_position(self, data):
            size = 1_486_700 if not self.calls else (
                1_486_700 if self.pending_reads < 2 else 486_700
            )
            return SimpleNamespace(size=size, price=0.37)

        def get_current_price(self, data):
            return 0.37

        def get_rebalance_cash(self):
            return 0.0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            return SimpleNamespace(
                id="SELL_1",
                batch_order_ids=("SELL_1",),
                submitted_size=1_000_000,
                batch_submitted_size=1_000_000,
                batch_submit_failed=True,
            )

        def get_pending_orders(self):
            self.pending_reads += 1
            if self.pending_reads == 1:
                return [{
                    "id": "SELL_1",
                    "symbol": "SHSE.512010",
                    "direction": "SELL",
                    "size": 1_000_000,
                }]
            return []

        def sync_balance(self):
            return None

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_POLL_SECONDS = 1.0
    plan = {
        "sell_clear": [SimpleNamespace(_name="SHSE.512010")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="SHSE.513050"), 554_553.19)],
    }

    executor.execute_plan(plan)

    assert broker.calls == [("SHSE.512010", 0.0), ("SHSE.513050", 554_553.19)]
    assert broker.pending_reads >= 2, "已受理子单必须等待到可信终态后再结束本轮。"
    assert broker._pending_sells == set()
    assert pushed and pushed[0]["level"] == "ERROR"


def test_order_executor_waits_for_post_sell_cash_snapshot_before_final_buy(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    class DummyBroker:
        is_live = True
        safety_multiplier = 1.0

        def __init__(self):
            self.calls = []
            self.sync_calls = 0
            self._pending_sells = set()
            self.sell_submitted = False

        def get_position(self, data):
            size = 100 if data._name == "SPY.ARCA" and not self.sell_submitted else 0
            return SimpleNamespace(size=size, price=100.0)

        def get_current_price(self, data):
            return 100.0 if data._name == "SPY.ARCA" else 10.0

        def get_rebalance_cash(self):
            if not self.sell_submitted:
                return 1000.0
            if clock["t"] < 3.0:
                return 8000.0
            return 11000.0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target), clock["t"]))
            if data._name == "SPY.ARCA":
                self.sell_submitted = True
                self._pending_sells.add("SELL_LOCAL_1")
                return SimpleNamespace(id="SELL_LOCAL_1", platform_order=SimpleNamespace(volume=100))
            return object()

        def get_pending_orders(self):
            return []

        def sync_balance(self):
            self.sync_calls += 1

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_POLL_SECONDS = 1.0
    executor._POST_SELL_CASH_POLL_SECONDS = 1.0
    executor._POST_SELL_CASH_WAIT_SECONDS = 5.0
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 11000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls[0] == ("SPY.ARCA", 0.0, 0.0)
    assert broker.calls[1] == ("EWJ.ARCA", 11000.0, 3.0), "应等待卖出释放现金快照追上后再最终买入。"
    assert broker.sync_calls >= 2


def test_order_executor_post_sell_cash_wait_times_out_and_continues(monkeypatch):
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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

    class DummyBroker:
        is_live = True
        safety_multiplier = 1.0

        def __init__(self):
            self.calls = []
            self._pending_sells = set()
            self.sell_submitted = False

        def get_position(self, data):
            size = 100 if data._name == "SPY.ARCA" and not self.sell_submitted else 0
            return SimpleNamespace(size=size, price=100.0)

        def get_current_price(self, data):
            return 100.0 if data._name == "SPY.ARCA" else 10.0

        def get_rebalance_cash(self):
            return 1000.0 if not self.sell_submitted else 8000.0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target), clock["t"]))
            if data._name == "SPY.ARCA":
                self.sell_submitted = True
                self._pending_sells.add("SELL_LOCAL_1")
                return SimpleNamespace(id="SELL_LOCAL_1", platform_order=SimpleNamespace(volume=100))
            return object()

        def get_pending_orders(self):
            return []

        def sync_balance(self):
            return None

    broker = DummyBroker()
    executor = executor_module.OrderExecutor(broker)
    executor._SELL_SETTLE_POLL_SECONDS = 1.0
    executor._POST_SELL_CASH_POLL_SECONDS = 1.0
    executor._POST_SELL_CASH_WAIT_SECONDS = 2.0
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 11000.0)],
    }

    executor.execute_plan(plan)

    assert broker.calls[0] == ("SPY.ARCA", 0.0, 0.0)
    assert broker.calls[1] == ("EWJ.ARCA", 11000.0, 2.0), "现金估算追不上时必须有界超时并继续执行。"
    assert any(item["level"] == "WARNING" for item in pushed)
    assert any("现金快照仍低于本轮卖出释放预期" in item["content"] for item in pushed)


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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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
    buy_calls = [call for call in broker.calls if call[0] == "EWJ.ARCA"]
    assert buy_calls == [("EWJ.ARCA", 30000.0)], "卖单长时间未清空时，本轮等待内只做一次低频滚动买入。"
    assert any(item["level"] == "ERROR" for item in pushed), "硬等待结束仍未确认卖出时应告警。"


def test_order_executor_delays_rolling_buy_and_tops_up_after_sell_clear(monkeypatch):
    import common.order_executor as executor_module

    clock = {"t": 0.0}

    def _fake_time():
        return clock["t"]

    def _fake_sleep(seconds):
        clock["t"] += float(seconds)

    monkeypatch.setattr(executor_module.time, "time", _fake_time)
    monkeypatch.setattr(executor_module.time, "sleep", _fake_sleep)

    buy_data = SimpleNamespace(_name="EWJ.ARCA")
    sell_data = SimpleNamespace(_name="SPY.ARCA")

    class DummyBroker:
        safety_multiplier = 1.0

        def __init__(self, sell_clear_after, show_buy_pending=True):
            self.sell_clear_after = sell_clear_after
            self.show_buy_pending = show_buy_pending
            self.calls = []
            self.sync_calls = 0
            self._pending_sells = set()
            self._active_buys = {}
            self.positions = {"SPY.ARCA": 100, "EWJ.ARCA": 0}
            self.sell_submitted = False

        def get_position(self, data):
            size = self.positions.get(data._name, 0)
            if (
                data._name == "SPY.ARCA"
                and self.sell_submitted
                and clock["t"] >= self.sell_clear_after
            ):
                size = 0
            return SimpleNamespace(size=size, price=1.0)

        def get_current_price(self, data):
            return 1.0

        def get_rebalance_cash(self):
            return 1000.0

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if data._name == "SPY.ARCA":
                self.sell_submitted = True
                self._pending_sells.add("SELL_LOCAL_1")
                return SimpleNamespace(id="SELL_LOCAL_1", platform_order=SimpleNamespace(volume=100))
            self._active_buys[f"BUY_{len(self._active_buys) + 1}"] = {"data": data}
            return object()

        def get_pending_orders(self):
            orders = []
            if clock["t"] < self.sell_clear_after:
                orders.append({"id": "SELL_LOCAL_1", "symbol": "SPY.ARCA", "direction": "SELL", "size": 100})
            if self._active_buys and self.show_buy_pending:
                orders.append({"id": "BUY_1", "symbol": "EWJ", "direction": "BUY", "size": 1000})
            return orders

        def sync_balance(self):
            self.sync_calls += 1

    def run_case(sell_clear_after, warn_after, show_buy_pending=True):
        clock["t"] = 0.0
        broker = DummyBroker(sell_clear_after=sell_clear_after, show_buy_pending=show_buy_pending)
        executor = executor_module.OrderExecutor(broker)
        executor._SELL_SETTLE_WARN_SECONDS = warn_after
        executor._SELL_SETTLE_HARD_SECONDS = max(5.0, warn_after * 2.0)
        executor.execute_plan({
            "sell_clear": [sell_data],
            "reduce": [],
            "increase": [(buy_data, 100000.0)],
        })
        return broker, clock["t"]

    fast_broker, fast_elapsed = run_case(sell_clear_after=0.0, warn_after=10.0)
    assert fast_broker.calls == [
        ("SPY.ARCA", 0.0),
        ("EWJ.ARCA", 100000.0),
    ], "卖单能快速确认清空时，不应先滚动买入一小笔。"
    assert fast_elapsed < 10.0

    slow_broker, _ = run_case(sell_clear_after=2.0, warn_after=1.0)
    assert slow_broker.calls == [
        ("SPY.ARCA", 0.0),
        ("EWJ.ARCA", 1000.0),
        ("EWJ.ARCA", 100000.0),
    ], "卖单确认清空后，即使本轮滚动买入仍在途，也应继续提交目标补齐。"
    assert fast_broker.sync_calls == 1
    assert slow_broker.sync_calls == 1
    assert "SELL_LOCAL_1" not in slow_broker._pending_sells

    hidden_buy_broker, _ = run_case(sell_clear_after=2.0, warn_after=1.0, show_buy_pending=False)
    assert hidden_buy_broker.calls == [
        ("SPY.ARCA", 0.0),
        ("EWJ.ARCA", 1000.0),
    ], "滚动 BUY 只有本地 active 但未出现在柜台 pending 快照时，不应继续补齐以免重复计算仓位。"


def test_order_executor_tops_up_customer_case_after_100_share_rolling_buy(monkeypatch):
    import common.order_executor as executor_module

    monkeypatch.setattr(executor_module.config, "LOT_SIZE", 100)
    clock = {"t": 0.0}
    monkeypatch.setattr(executor_module.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        executor_module.time,
        "sleep",
        lambda seconds: clock.__setitem__("t", clock["t"] + float(seconds)),
    )

    sell_data = SimpleNamespace(_name="SHSE.512010")
    buy_data = SimpleNamespace(_name="SHSE.513050")

    class Broker:
        is_live = True
        safety_multiplier = 1.0

        def __init__(self):
            self.calls = []
            self.buy_targets = []
            self.accepted_buy_sizes = []
            self.buy_size = 0
            self._pending_sells = set()
            self._active_buys = {}
            self._last_pending_orders_fetch_failed = False

        def get_position(self, data):
            if data._name == sell_data._name:
                size = 264_700 if clock["t"] < 360.0 else 0
                return SimpleNamespace(size=size, price=0.373)
            return SimpleNamespace(size=self.buy_size, price=1.132)

        def get_current_price(self, data):
            return 0.373 if data._name == sell_data._name else 1.132

        def get_rebalance_cash(self):
            return 120.0 if clock["t"] < 360.0 else 98_753.80

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target), clock["t"]))
            if data._name == sell_data._name:
                self._pending_sells.add("SELL_CUSTOMER")
                return SimpleNamespace(
                    id="SELL_CUSTOMER",
                    submitted_size=264_700,
                    batch_order_ids=("SELL_CUSTOMER",),
                    batch_submitted_size=264_700,
                    batch_submit_failed=False,
                )

            self.buy_targets.append(float(target))
            if len(self.buy_targets) == 1:
                self.accepted_buy_sizes.append(100)
                self.buy_size = 100
                self._active_buys["BUY_100"] = {"data": data}
            else:
                self.accepted_buy_sizes.append(87_200)
                self.buy_size = 87_300
            return object()

        def get_pending_orders(self):
            if clock["t"] >= 307.0:
                self._active_buys.clear()
            orders = []
            if clock["t"] < 360.0:
                orders.append({
                    "id": "SELL_CUSTOMER",
                    "symbol": sell_data._name,
                    "direction": "SELL",
                    "size": 264_700,
                })
            if "BUY_100" in self._active_buys:
                orders.append({
                    "id": "BUY_100",
                    "symbol": buy_data._name,
                    "direction": "BUY",
                    "size": 100,
                })
            return orders

        def sync_balance(self):
            return None

    broker = Broker()
    executor = executor_module.OrderExecutor(broker)
    executor.execute_plan({
        "sell_clear": [sell_data],
        "reduce": [],
        "increase": [(buy_data, 98_883.86)],
    })

    assert broker.accepted_buy_sizes == [100, 87_200]
    assert [round(value, 2) for value in broker.buy_targets] == [120.0, 98_883.86]
    assert clock["t"] < 600.0


def test_order_executor_uses_current_cash_when_sell_not_submitted(monkeypatch):
    import common.order_executor as executor_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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

    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)]
    assert len(pushed) == 2, "应分别推送卖单未提交与尽力执行告警。"
    assert all(item["level"] == "ERROR" for item in pushed)
    assert "SELL order not submitted" in pushed[0]["content"]
    assert "cash-limited partial execution" in pushed[1]["content"]


def test_order_executor_warns_when_buy_not_submitted(monkeypatch):
    import common.order_executor as executor_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

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


def test_order_executor_keeps_unknown_buy_failure_visible_in_quiet_backtest(monkeypatch, capsys):
    import common.order_executor as executor_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

    class DummyBacktestBroker:
        is_live = False
        verbose = False

        def order_target_value(self, data, target):
            return None

    broker = DummyBacktestBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    out = capsys.readouterr().out
    assert "BUY order not submitted" in out
    assert len(pushed) == 1
    assert pushed[0]["level"] == "ERROR"


def test_order_executor_keeps_buy_not_submitted_visible_in_verbose_backtest(monkeypatch, capsys):
    import common.order_executor as executor_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

    class DummyBacktestBroker:
        is_live = False
        verbose = True

        def order_target_value(self, data, target):
            return None

    broker = DummyBacktestBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    out = capsys.readouterr().out
    assert "BUY order not submitted" in out
    assert len(pushed) == 1
    assert pushed[0]["level"] == "ERROR"


def test_order_executor_treats_backtest_min_lot_buy_skip_as_noop(monkeypatch, capsys):
    import common.order_executor as executor_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

    class DummyBacktestBroker:
        is_live = False
        verbose = True
        _last_order_target_skip_reason = None

        def order_target_value(self, data, target):
            self._last_order_target_skip_reason = 'insufficient_cash_for_min_lot'
            return None

    broker = DummyBacktestBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    out = capsys.readouterr().out
    assert "BUY order not submitted" not in out
    assert pushed == []


def test_order_executor_treats_backtest_benign_sell_skip_as_noop_and_continues_buys(monkeypatch, capsys):
    import common.order_executor as executor_module

    pushed = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    monkeypatch.setattr(executor_module.runtime_notifications, "push_text", DummyAlarmManager().push_text)

    class DummyBacktestBroker:
        is_live = False
        verbose = True
        _last_order_target_skip_reason = None

        def __init__(self):
            self.calls = []

        def order_target_value(self, data, target):
            self.calls.append((data._name, float(target)))
            if data._name == "SPY.ARCA":
                self._last_order_target_skip_reason = 'target_already_met'
                return None
            self._last_order_target_skip_reason = None
            return object()

    broker = DummyBacktestBroker()
    executor = executor_module.OrderExecutor(broker)
    plan = {
        "sell_clear": [SimpleNamespace(_name="SPY.ARCA")],
        "reduce": [],
        "increase": [(SimpleNamespace(_name="EWJ.ARCA"), 100000.0)],
    }

    executor.execute_plan(plan)

    out = capsys.readouterr().out
    assert broker.calls == [("SPY.ARCA", 0.0), ("EWJ.ARCA", 100000.0)]
    assert "SELL order not submitted" not in out
    assert "Planned BUY orders are skipped" not in out
    assert pushed == []
