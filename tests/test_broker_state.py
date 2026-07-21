from datetime import datetime
from unittest.mock import MagicMock

import pytest

import config
from live_trader.adapters.base_broker import BaseLiveBroker, BaseOrderProxy
from live_trader.data_bridge.data_warm import BrokerDataWarmBridge


class MockOrderProxy(BaseOrderProxy):
    def __init__(self, oid, is_buy_order, status="PendingSubmit"):
        self._id = oid
        self._is_buy = is_buy_order
        self._status = status
        self.executed = MagicMock(size=0, price=0, value=0, comm=0)

    @property
    def id(self):
        return self._id

    def is_completed(self):
        return self._status == "Filled"

    def is_canceled(self):
        return self._status == "Canceled"

    def is_rejected(self):
        return self._status == "Rejected"

    def is_pending(self):
        return self._status in ["PendingSubmit", "Submitted", "PendingCancel"]

    def is_accepted(self):
        return self._status in ["PendingSubmit", "Submitted", "PendingCancel"]

    def is_buy(self):
        return self._is_buy

    def is_sell(self):
        return not self._is_buy


class MockBroker(BaseLiveBroker):
    def __init__(self, initial_cash):
        self.mock_cash = initial_cash
        super().__init__(context=MagicMock())
        self.submitted_orders = []
        self.mock_position = 0
        self._datetime = None

    def _fetch_real_cash(self):
        return self.mock_cash

    def get_position(self, data):
        return MagicMock(size=self.mock_position)

    def get_current_price(self, data):
        return 10.0

    def get_pending_orders(self):
        # 将 submitted_orders 中暂未流转为最终状态的单子模拟为在途单返回。
        # 在该测试桩里，submitted_orders 默认全部视作在途单，除非测试显式模拟完成/撤销语义。
        pending = []
        for order in self.submitted_orders:
            status = order.get("status", "Submitted")
            if status not in {"PendingSubmit", "Submitted", "PendingCancel"}:
                continue
            pending.append(
                {
                    "id": order["id"],
                    "symbol": order.get("symbol", "SHSE.600000"),
                    "direction": order["side"],
                    "size": order["volume"],
                }
            )
        return pending

    def getvalue(self):
        return self.mock_cash

    def _submit_order(self, data, volume, side, price):
        oid = f"ORDER_{len(self.submitted_orders) + 1}"
        proxy = MockOrderProxy(oid, is_buy_order=(side == "BUY"))
        self.submitted_orders.append(
            {
                "id": oid,
                "side": side,
                "volume": volume,
                "symbol": data._name,
                "status": "Submitted",
            }
        )
        return proxy

    def convert_order_proxy(self, raw_order):
        return raw_order

    @staticmethod
    def is_live_mode(context):
        return True


def _make_data(symbol="SHSE.600000"):
    data = MagicMock()
    data._name = symbol
    return data


@pytest.fixture(autouse=True)
def _force_lot_size_100(monkeypatch):
    monkeypatch.setattr(config, "LOT_SIZE", 100)
    monkeypatch.setattr(config, "BROKER_LOT_LIMITS", 0)


def test_base_safety_multiplier_has_no_fixed_cash_buffer():
    """
    资金利用率回归:
    默认 live broker 只估算滑点和手续费，不再额外固定保留 0.2% 现金。
    """
    broker = MockBroker(initial_cash=100000.0)

    assert broker.safety_multiplier == pytest.approx(1.0013)


def test_stateless_buy_skips_when_cash_insufficient_even_with_pending_sell():
    """
    无状态回归:
    卖单在途且现金不足时，买单应当场失败，不进入任何重试缓存。
    后续若要买入，必须由下一次策略信号重新触发。
    """
    broker = MockBroker(initial_cash=100.0)
    data = _make_data()

    broker._pending_sells.add("SELL_1")

    first_try = broker.order_target_value(data, target=1000)
    assert first_try is None, "现金不足时应直接返回 None（无状态不入队）"
    assert len(broker.submitted_orders) == 0, "首次尝试不应发送真实委托"

    broker.on_order_status(MockOrderProxy("SELL_1", is_buy_order=False, status="Filled"))
    assert "SELL_1" not in broker._pending_sells, "卖单 Filled 后应从 _pending_sells 移除"
    assert len(broker.submitted_orders) == 0, "卖单回调不应偷偷发出买单"

    # 第二次由策略再次调用时，才会按当前现金重新尝试下单
    broker.mock_cash = 5000.0
    second_try = broker.order_target_value(data, target=1000)
    assert second_try is not None, "再次触发信号后应按最新现金下单"
    assert len(broker.submitted_orders) == 1, "第二次尝试应发出真实买单"
    assert broker.submitted_orders[0]["side"] == "BUY", "买单方向应为 BUY"
    assert broker.submitted_orders[0]["volume"] == 100, "买单数量应为 100 股"


def test_live_buy_splits_by_configured_broker_lot_limit(monkeypatch):
    monkeypatch.setattr(config, "BROKER_LOT_LIMITS", 1_000_000)
    broker = MockBroker(initial_cash=20_000_000.0)
    data = _make_data("SHSE.512010")

    proxy = broker.order_target_value(data, target=14_848_000.0)

    assert proxy is not None
    assert proxy.id == "ORDER_1", "拆单调用应保持原有单代理返回契约。"
    assert [order["volume"] for order in broker.submitted_orders] == [1_000_000, 484_800]
    assert set(broker._active_buys) == {"ORDER_1", "ORDER_2"}


def test_backtest_buy_ignores_broker_lot_limit(monkeypatch):
    monkeypatch.setattr(config, "BROKER_LOT_LIMITS", 1_000_000)
    broker = MockBroker(initial_cash=20_000_000.0)
    broker.is_live = False
    data = _make_data("SHSE.512010")

    proxy = broker.order_target_value(data, target=14_848_000.0)

    assert proxy is not None
    assert [order["volume"] for order in broker.submitted_orders] == [1_484_800]


def test_later_split_child_sync_failure_retries_by_lot_step(monkeypatch):
    monkeypatch.setattr(config, "BROKER_LOT_LIMITS", 1000)

    class RetryLaterChildBroker(MockBroker):
        def __init__(self, initial_cash):
            super().__init__(initial_cash)
            self.submit_attempts = []

        def _submit_order(self, data, volume, side, price):
            self.submit_attempts.append(volume)
            if len(self.submit_attempts) == 2:
                return None
            return super()._submit_order(data, volume, side, price)

    broker = RetryLaterChildBroker(initial_cash=100000.0)
    data = _make_data("SHSE.512010")

    proxy = broker.order_target_value(data, target=14000.0)

    assert proxy is not None
    assert proxy.id == "ORDER_1"
    assert broker.submit_attempts == [1000, 400, 300]
    assert [order["volume"] for order in broker.submitted_orders] == [1000, 300]
    assert broker._active_buys["ORDER_2"]["retries"] == 1
    assert not hasattr(broker, "_buffered_rejected_retries")


def test_later_split_child_sync_failure_exhausts_shared_downgrade_and_alarms(monkeypatch):
    import live_trader.adapters.base_broker as base_module

    monkeypatch.setattr(config, "LOT_SIZE", 1)
    monkeypatch.setattr(config, "BROKER_LOT_LIMITS", 30)
    pushed = []
    monkeypatch.setattr(
        base_module.runtime_notifications,
        "push_text",
        lambda content, level='INFO': pushed.append({"content": content, "level": level}) or True,
    )

    class AlwaysFailLaterChildBroker(MockBroker):
        def __init__(self, initial_cash):
            super().__init__(initial_cash)
            self.submit_attempts = []

        def _submit_order(self, data, volume, side, price):
            self.submit_attempts.append(volume)
            if len(self.submit_attempts) > 1:
                return None
            return super()._submit_order(data, volume, side, price)

    broker = AlwaysFailLaterChildBroker(initial_cash=10000.0)
    data = _make_data("TEST.SPLIT")

    proxy = broker.order_target_value(data, target=600.0)

    assert proxy is not None
    assert proxy.id == "ORDER_1"
    assert broker.submit_attempts == [30, 30, 29, 28, 27, 26, 25, 23, 20, 16, 11, 6]
    assert [order["volume"] for order in broker.submitted_orders] == [30]
    assert len(pushed) == 1
    assert pushed[0]["level"] == "ERROR"
    assert "downgrade retries exhausted" in pushed[0]["content"]
    assert not hasattr(broker, "_deferred_orders")
    assert not hasattr(broker, "_buffered_rejected_retries")


def test_base_broker_uses_composed_data_warm_bridge():
    broker = MockBroker(initial_cash=1000.0)

    assert isinstance(broker._data_warm, BrokerDataWarmBridge)
    assert broker._data_warm._host is broker
    assert not isinstance(broker, BrokerDataWarmBridge)


def test_auto_downgrade_and_refund():
    """
    买单被拒绝后:
    1) 先回退上一笔虚拟资金预扣
    2) 再按 lot_size 自动降级重试并重新预扣
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data()

    first_proxy = broker.order_target_value(data, target=2000)  # 200 股
    assert first_proxy is not None, "首笔买单应提交成功"
    assert first_proxy.id == "ORDER_1", "首笔订单 ID 应为 ORDER_1"
    assert broker.submitted_orders[0]["volume"] == 200, "首笔订单应为 200 股"

    expected_before_reject = 200 * 10.0 * broker.safety_multiplier
    assert broker._virtual_spent_cash == pytest.approx(expected_before_reject), "首笔订单的虚拟预扣金额异常"

    # 模拟柜台在 rejected 前已将原单从在途列表移除，允许立即重试。
    broker.submitted_orders[0]["status"] = "Inactive"
    broker.on_order_status(MockOrderProxy("ORDER_1", is_buy_order=True, status="Rejected"))

    expected_after_reject = 100 * 10.0 * broker.safety_multiplier
    assert broker._virtual_spent_cash == pytest.approx(expected_after_reject), "拒单后资金回退/重扣计算不正确"
    assert len(broker.submitted_orders) == 2, "拒单后应自动触发降级重试订单"
    assert broker.submitted_orders[1]["id"] == "ORDER_2", "降级重试订单 ID 应为 ORDER_2"
    assert broker.submitted_orders[1]["side"] == "BUY", "降级重试订单方向应为 BUY"
    assert broker.submitted_orders[1]["volume"] == 100, "降级后股数应按 lot_size 减少为 100"
    assert "ORDER_1" not in broker._active_buys, "被拒订单应从 _active_buys 移除"
    assert "ORDER_2" in broker._active_buys, "降级重试后的新订单应进入 _active_buys"


def test_rejected_buy_downgrades_by_lot_step_first(monkeypatch):
    """
    拒单后降级:
    可解释性优先语义下，前 5 次应按 LOT_SIZE 线性递减，而不是按资金比例重算。
    """
    monkeypatch.setattr(config, "LOT_SIZE", 1)

    broker = MockBroker(initial_cash=10000.0)
    data = _make_data()

    first_proxy = broker.order_target_value(data, target=290)  # 29 股
    assert first_proxy is not None, "首笔买单应提交成功"
    assert broker.submitted_orders[0]["volume"] == 29, "前置条件失败: 首笔应为 29 股"

    # 即使现金窗口收紧，首轮拒单仍优先执行 LOT_SIZE 阶梯降级。
    broker.mock_cash = 271.0
    broker.submitted_orders[0]["status"] = "Inactive"
    broker.on_order_status(MockOrderProxy("ORDER_1", is_buy_order=True, status="Rejected"))

    assert len(broker.submitted_orders) == 2, "拒单后应触发重试订单"
    assert broker.submitted_orders[1]["side"] == "BUY", "降级后应继续提交 BUY 订单"
    assert broker.submitted_orders[1]["volume"] == 28, "LOT_SIZE=1 时首轮拒单应从 29 降到 28。"
    assert "ORDER_2" in broker._active_buys, "降级重试后的订单应进入 _active_buys"
    assert broker._virtual_spent_cash == pytest.approx(28 * 10.0 * broker.safety_multiplier), (
        "降级后的虚拟占资应与 28 股一致。"
    )


def test_rejected_buy_retries_immediately_without_buffer(monkeypatch):
    """
    无状态回归:
    拒单后应当场降级重提，不等待 Cancel 回调，也不缓存跨回调意图。
    """
    monkeypatch.setattr(config, "LOT_SIZE", 1)

    broker = MockBroker(initial_cash=10000.0)
    data = _make_data()

    first_proxy = broker.order_target_value(data, target=290)  # 29 股
    assert first_proxy is not None, "首笔买单应提交成功"
    assert broker.submitted_orders[0]["volume"] == 29, "前置条件失败: 首笔应为 29 股"
    assert broker.submitted_orders[0]["status"] == "Submitted", "前置条件失败: 首笔应仍在途"

    broker.mock_cash = 280.0
    broker.on_order_status(MockOrderProxy("ORDER_1", is_buy_order=True, status="Rejected"))

    assert len(broker.submitted_orders) == 2, "拒单后应立即提交降级重试单"
    assert broker.submitted_orders[1]["side"] == "BUY", "降级重试方向应为 BUY"
    assert broker.submitted_orders[1]["volume"] == 28, "首轮拒单应先按 LOT_SIZE 线性降级到 28 股"
    assert not hasattr(broker, "_buffered_rejected_retries"), "无状态实现不应再维护拒单缓冲队列"


def test_rejected_buy_uses_geometric_after_five_lot_steps(monkeypatch):
    """
    阶段化降级回归:
    前 5 次按 LOT_SIZE 线性降级，第 6 次起进入几何降级。
    """
    monkeypatch.setattr(config, "LOT_SIZE", 1)

    broker = MockBroker(initial_cash=10000.0)
    data = _make_data()

    first_proxy = broker.order_target_value(data, target=300)  # 30 股
    assert first_proxy is not None, "首笔买单应提交成功"
    assert broker.submitted_orders[0]["volume"] == 30, "前置条件失败: 首笔应为 30 股"

    # 连续触发 6 次拒单，观察降级路径:
    # 30 -> 29 -> 28 -> 27 -> 26 -> 25 -> 23(几何: 25*0.95 向下取整)
    for oid_num in range(1, 7):
        broker.on_order_status(MockOrderProxy(f"ORDER_{oid_num}", is_buy_order=True, status="Rejected"))

    volumes = [o["volume"] for o in broker.submitted_orders]
    assert volumes[:7] == [30, 29, 28, 27, 26, 25, 23], (
        "降级路径应为前 5 次线性减 1，第 6 次切到几何降级。"
    )


def test_rejected_buy_multi_symbols_retry_independently(monkeypatch):
    """
    多标的回归:
    同一根 K 线内多个买单各自拒单时，应分别降级并重提，互不依赖。
    """
    monkeypatch.setattr(config, "LOT_SIZE", 1)

    broker = MockBroker(initial_cash=10000.0)
    data_a = _make_data("AAA.TEST")
    data_b = _make_data("BBB.TEST")

    p1 = broker.order_target_value(data_a, target=290)  # ORDER_1: 29
    p2 = broker.order_target_value(data_b, target=290)  # ORDER_2: 29
    assert p1 is not None and p2 is not None, "前置失败：两个标的的首单都应成功发出"
    assert len(broker.submitted_orders) == 2, "前置失败：应先有 2 笔初始买单"

    broker.mock_cash = 280.0
    broker.on_order_status(MockOrderProxy("ORDER_1", is_buy_order=True, status="Rejected"))

    broker.mock_cash = 200.0
    broker.on_order_status(MockOrderProxy("ORDER_2", is_buy_order=True, status="Rejected"))

    assert len(broker.submitted_orders) == 4, "两个标的拒单后都应产生各自的降级重试单"
    assert broker.submitted_orders[2]["symbol"] == "AAA.TEST", "第一个标的的重试单 symbol 错误"
    assert broker.submitted_orders[3]["symbol"] == "BBB.TEST", "第二个标的的重试单 symbol 错误"
    assert broker.submitted_orders[2]["volume"] < 29, "第一个标的重试单必须低于原始下单量"
    assert broker.submitted_orders[3]["volume"] < 29, "第二个标的重试单必须低于原始下单量"


def test_synchronous_rejected_buy_retries_immediately_without_false_success():
    """
    券商同步返回 Rejected 时，也必须走同一套当场降级重试逻辑。
    防止只打印 BUY 信号、但柜台没有真实在途委托。
    """

    class SyncRejectedBroker(MockBroker):
        def _submit_order(self, data, volume, side, price):
            oid = f"ORDER_{len(self.submitted_orders) + 1}"
            status = "Rejected" if side == "BUY" and len(self.submitted_orders) == 0 else "Submitted"
            proxy = MockOrderProxy(oid, is_buy_order=(side == "BUY"), status=status)
            self.submitted_orders.append(
                {
                    "id": oid,
                    "side": side,
                    "volume": volume,
                    "symbol": data._name,
                    "status": status,
                }
            )
            return proxy

    broker = SyncRejectedBroker(initial_cash=100000.0)
    data = _make_data()

    proxy = broker.order_target_value(data, target=2000.0)  # 首单 200 股，同步拒单后应降到 100 股

    assert proxy is not None, "同步拒单后若降级单成功，应把降级单返回给调用方。"
    assert proxy.id == "ORDER_2", "返回对象应是本轮实际可跟踪的降级委托。"
    assert len(broker.submitted_orders) == 2, "同步拒单必须立即触发一次降级重试。"
    assert broker.submitted_orders[0]["status"] == "Rejected"
    assert broker.submitted_orders[1]["side"] == "BUY"
    assert broker.submitted_orders[1]["volume"] == 100
    assert "ORDER_1" not in broker._active_buys, "同步拒单原单不应残留 active_buys。"
    assert broker._active_buys["ORDER_2"]["shares"] == 100
    assert broker._virtual_spent_cash == pytest.approx(100 * 10.0 * broker.safety_multiplier)


def test_synchronous_rejected_sell_returns_none_and_does_not_pending():
    """
    卖单同步拒绝时不能算作已提交；执行器上层应据此跳过后续买入。
    """

    class SyncRejectedSellBroker(MockBroker):
        def _submit_order(self, data, volume, side, price):
            oid = f"ORDER_{len(self.submitted_orders) + 1}"
            status = "Rejected" if side == "SELL" else "Submitted"
            proxy = MockOrderProxy(oid, is_buy_order=(side == "BUY"), status=status)
            self.submitted_orders.append(
                {
                    "id": oid,
                    "side": side,
                    "volume": volume,
                    "symbol": data._name,
                    "status": status,
                }
            )
            return proxy

    broker = SyncRejectedSellBroker(initial_cash=100000.0)
    data = _make_data()
    broker.mock_position = 500

    proxy = broker.order_target_value(data, target=0.0)

    assert proxy is None, "同步拒绝的 SELL 不应返回成功代理。"
    assert len(broker.submitted_orders) == 1
    assert broker.submitted_orders[0]["status"] == "Rejected"
    assert broker._pending_sells == set(), "同步拒绝的 SELL 不应进入 pending_sells。"


def test_broken_order_state_is_treated_as_not_accepted_and_refunded():
    """
    适配器状态方法异常时，基类必须保守处理为未受理，
    不能留下 active_buys 或虚拟占资。
    """

    class BrokenStateProxy(MockOrderProxy):
        @property
        def status(self):
            return "BrokenState"

        def is_completed(self):
            raise RuntimeError("status unavailable")

        def is_canceled(self):
            raise RuntimeError("status unavailable")

        def is_rejected(self):
            raise RuntimeError("status unavailable")

        def is_pending(self):
            raise RuntimeError("status unavailable")

        def is_accepted(self):
            raise RuntimeError("status unavailable")

    class BrokenStateBroker(MockBroker):
        def _submit_order(self, data, volume, side, price):
            oid = f"ORDER_{len(self.submitted_orders) + 1}"
            proxy = BrokenStateProxy(oid, is_buy_order=(side == "BUY"))
            self.submitted_orders.append(
                {
                    "id": oid,
                    "side": side,
                    "volume": volume,
                    "symbol": data._name,
                    "status": "BrokenState",
                }
            )
            return proxy

    broker = BrokenStateBroker(initial_cash=100000.0)
    data = _make_data()

    proxy = broker.order_target_value(data, target=1000.0)

    assert proxy is None, "状态不可读时应按未受理处理。"
    assert len(broker.submitted_orders) == 1
    assert broker._active_buys == {}, "未受理订单不得残留 active_buys。"
    assert broker._virtual_spent_cash == pytest.approx(0.0), "未受理订单必须释放虚拟占资。"


def test_accepted_only_order_state_stays_active_in_base_broker():
    """
    框架层状态契约回归:
    有些 broker 可能只通过 is_accepted() 表达“已被柜台受理但不在本地 pending 枚举内”。
    基类不能把 accepted-only 代理误判为终态，否则会提前释放虚拟占资或丢失卖单监控。
    """

    class AcceptedOnlyProxy(MockOrderProxy):
        def __init__(self, oid, is_buy_order):
            super().__init__(oid, is_buy_order=is_buy_order, status="AcceptedOnly")

        def is_pending(self):
            return False

        def is_accepted(self):
            return True

    class AcceptedOnlyBroker(MockBroker):
        def _submit_order(self, data, volume, side, price):
            oid = f"ORDER_{len(self.submitted_orders) + 1}"
            proxy = AcceptedOnlyProxy(oid, is_buy_order=(side == "BUY"))
            self.submitted_orders.append(
                {
                    "id": oid,
                    "side": side,
                    "volume": volume,
                    "symbol": data._name,
                    "status": "AcceptedOnly",
                }
            )
            return proxy

    data = _make_data()

    buy_broker = AcceptedOnlyBroker(initial_cash=100000.0)
    buy_proxy = buy_broker.order_target_value(data, target=1000.0)
    expected_reserved = 100 * 10.0 * buy_broker.safety_multiplier

    assert buy_proxy is not None
    assert buy_proxy.id in buy_broker._active_buys
    assert buy_broker._virtual_spent_cash == pytest.approx(expected_reserved)

    callback_ret = buy_broker.on_order_status(buy_proxy)

    assert callback_ret is buy_proxy, "accepted-only 买单仍是活跃委托，不能被当作终态清理。"
    assert buy_proxy.id in buy_broker._active_buys
    assert buy_broker._virtual_spent_cash == pytest.approx(expected_reserved)

    sell_broker = AcceptedOnlyBroker(initial_cash=100000.0)
    sell_broker.mock_position = 500
    sell_proxy = sell_broker.order_target_value(data, target=0.0)

    assert sell_proxy is not None
    sell_broker._pending_sells.clear()
    sell_broker.on_order_status(sell_proxy)

    assert sell_proxy.id in sell_broker._pending_sells, "accepted-only 卖单必须继续纳入在途卖单监控。"


def test_accepted_order_without_id_is_not_treated_as_submitted():
    """
    框架层契约回归:
    BaseOrderProxy.id 是在途跟踪与撤单的硬契约。同步返回 accepted/pending 但无 id 时，
    基类不能打印成功信号或留下不可追踪的 active/pending 状态。
    """

    class NoIdProxy(MockOrderProxy):
        @property
        def id(self):
            return ""

    class NoIdBroker(MockBroker):
        def _submit_order(self, data, volume, side, price):
            proxy = NoIdProxy("", is_buy_order=(side == "BUY"), status="Submitted")
            self.submitted_orders.append(
                {
                    "id": "",
                    "side": side,
                    "volume": volume,
                    "symbol": data._name,
                    "status": "Submitted",
                }
            )
            return proxy

    data = _make_data()

    buy_broker = NoIdBroker(initial_cash=100000.0)
    buy_proxy = buy_broker.order_target_value(data, target=1000.0)

    assert buy_proxy is None
    assert buy_broker._active_buys == {}
    assert buy_broker._virtual_spent_cash == pytest.approx(0.0)

    sell_broker = NoIdBroker(initial_cash=100000.0)
    sell_broker.mock_position = 500
    sell_proxy = sell_broker.order_target_value(data, target=0.0)

    assert sell_proxy is None
    assert sell_broker._pending_sells == set()


def test_stale_state_reset_cross_day():
    """
    跨日推进时，清理陈旧状态:
    - _pending_sells
    """
    broker = MockBroker(initial_cash=10000.0)

    broker.set_datetime(datetime(2026, 2, 16, 14, 55, 0))
    broker._pending_sells.add("SELL_STALE_1")
    assert len(broker._pending_sells) == 1, "预置的脏 pending_sells 状态注入失败"

    broker.set_datetime(datetime(2026, 2, 17, 9, 31, 0))

    assert len(broker._pending_sells) == 0, "跨日后 _pending_sells 必须被清空"


def test_risk_block_buy():
    """
    风控锁命中后，买单必须被物理拦截，不进入任何真实下单流程。
    这里使用 order_target_value 验证底层拦截分支。
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data("SHSE.600000")
    broker.lock_for_risk("SHSE.600000")

    ret = broker.order_target_value(data, target=20000)

    assert ret is None, "风控锁命中时应直接返回 None"
    assert len(broker.submitted_orders) == 0, "风控拦截后不应发出真实订单"


def test_risk_block_buy_target_percent():
    """
    风控锁命中后，order_target_percent 也必须被物理拦截。
    防止目标仓位接口绕过风控锁重新买回。
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data("SHSE.600000")
    broker.set_datas([data])
    broker.lock_for_risk("SHSE.600000")

    ret = broker.order_target_percent(data, target=0.5)

    assert ret is None, "风控锁命中时 order_target_percent 应直接返回 None"
    assert len(broker.submitted_orders) == 0, "风控拦截后不应发出真实订单"


def test_lot_size_truncation():
    """
    碎片股拦截:
    计算得到 50 股，LOT_SIZE=100 时应截断为 0 并取消委托。
    """
    broker = MockBroker(initial_cash=1000.0)
    data = _make_data()

    ret = broker.order_target_value(data, target=500)  # 500/10 = 50 股

    assert ret is None, "不足一手时应直接取消下单并返回 None"
    assert len(broker.submitted_orders) == 0, "碎片股拦截后不应有真实委托"
    assert broker._virtual_spent_cash == pytest.approx(0.0), "订单未提交时 _virtual_spent_cash 应保持 0"


def test_target_percent_rebalance():
    """
    目标仓位再平衡:
    - 组合总资产 = 10 万 (现金 5 万 + 持仓市值 5 万)
    - 当前持仓 = 5000 股, 价格 = 10
    - target=0.8 => 目标市值 8 万 => 目标股数 8000 => 应买入 3000
    """
    broker = MockBroker(initial_cash=50000.0)
    data = _make_data()
    broker.mock_position = 5000
    broker.set_datas([data])

    ret = broker.order_target_percent(data, target=0.8)

    assert ret is not None, "目标仓位再平衡应产生买单"
    assert len(broker.submitted_orders) == 1, "再平衡应只产生 1 笔订单"
    assert broker.submitted_orders[0]["side"] == "BUY", "再平衡方向应为 BUY"
    assert broker.submitted_orders[0]["volume"] == 3000, "target=0.8 时应精确买入 3000 股"


def test_smart_sell_anti_shorting():
    """
    卖空物理拦截:
    真实持仓 5000，策略试图卖 8000 时，系统必须截断为最多卖出 5000。
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data()
    broker.mock_position = 5000

    ret = broker._smart_sell(data, shares=8000, price=10.0)

    assert ret is not None, "合法可卖持仓存在时应返回卖单代理"
    assert len(broker.submitted_orders) == 1, "应只发出 1 笔卖单"
    assert broker.submitted_orders[0]["side"] == "SELL", "卖出路径应提交 SELL 方向"
    assert broker.submitted_orders[0]["volume"] == 5000, "卖空拦截失败: 卖出量必须被截断到真实持仓 5000"


def test_smart_sell_respects_sellable_position_t1():
    """
    T+1 可卖仓位约束:
    即使真实持仓>0，只要可卖仓位=0，也必须跳过卖单，避免反复触发“仓位不足”拒单。
    """
    class T1AwareMockBroker(MockBroker):
        def __init__(self, initial_cash):
            super().__init__(initial_cash)
            self.mock_sellable = 0

        def get_sellable_position(self, data):
            return self.mock_sellable

    broker = T1AwareMockBroker(initial_cash=100000.0)
    data = _make_data()
    broker.mock_position = 5000
    broker.mock_sellable = 0

    ret = broker._smart_sell(data, shares=5000, price=10.0)

    assert ret is None, "可卖仓位为 0 时应直接跳过卖单"
    assert len(broker.submitted_orders) == 0, "T+1 拦截后不应发出任何 SELL 委托"


def test_smart_sell_odd_lot_release():
    """
    清仓碎股放行:
    真实持仓 150，LOT_SIZE=100；卖出 150(或更多)时应允许直接卖 150，确保可完全清仓。
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data()
    broker.mock_position = 150

    ret = broker._smart_sell(data, shares=999, price=10.0)

    assert ret is not None, "清仓路径应发出卖单"
    assert len(broker.submitted_orders) == 1, "清仓场景应只发出 1 笔卖单"
    assert broker.submitted_orders[0]["side"] == "SELL", "清仓提交方向应为 SELL"
    assert broker.submitted_orders[0]["volume"] == 150, "清仓碎股应放行 150 股，不应被截断为 100"


def test_sell_rejected_does_not_replay_or_enqueue_buy():
    """
    无状态回归:
    卖单拒绝后仅清理 pending_sells，不做任何历史意图重放或补下单。
    """
    broker = MockBroker(initial_cash=100.0)
    data = _make_data()

    broker._pending_sells.add("SELL_1")
    ret = broker.order_target_value(data, target=1000)
    assert ret is None, "前置失败：现金不足时应直接失败"

    broker.on_order_status(MockOrderProxy("SELL_1", is_buy_order=False, status="Rejected"))

    assert "SELL_1" not in broker._pending_sells, "卖单拒绝后应移除 pending sell 监控"
    assert len(broker.submitted_orders) == 0, "拒单回调阶段不应补发买单"


def test_sell_expired_clears_pending_without_replay():
    """
    非在途终态回归:
    卖单进入 Expired 等非 pending 终态时，必须清理 pending_sells，不得补发买单。
    """
    broker = MockBroker(initial_cash=100.0)

    broker._pending_sells.add("SELL_1")
    broker.on_order_status(MockOrderProxy("SELL_1", is_buy_order=False, status="Expired"))

    assert "SELL_1" not in broker._pending_sells, "Expired 卖单应移除 pending sell 监控"
    assert len(broker.submitted_orders) == 0, "Expired 卖单回调阶段不应补发买单"


def test_expected_size_with_pending_orders():
    """
    在途穿透防重下单:
    第一次 target=0.5 发出 5000 股 BUY 后，在未成交前再次下同目标，
    应识别在途仓位并避免重复下单。
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data()

    first = broker.order_target_percent(data, target=0.5)
    assert first is not None, "第一次再平衡应发出买单"
    assert len(broker.submitted_orders) == 1, "第一次调用后应有 1 笔订单"
    assert broker.submitted_orders[0]["side"] == "BUY", "第一次订单方向应为 BUY"
    assert broker.submitted_orders[0]["volume"] == 5000, "第一次 target=0.5 应买入 5000 股"
    assert broker.get_expected_size(data) == 5000, "get_expected_size 应识别到 5000 股在途买单"

    # 将 mock 现金上调等于已预扣虚拟金额，保持 NAV 在第二次调用时不失真。
    # 该步骤用于隔离测试目标：验证 expected_size 穿透计算可令 delta_shares 归零并阻止重复下单。
    broker.mock_cash = 100000.0 + broker._virtual_spent_cash

    second = broker.order_target_percent(data, target=0.5)
    assert second is None, "在途仓位已覆盖目标时应返回 None"
    assert len(broker.submitted_orders) == 1, "第二次调用不应新增任何订单"


def test_intraday_long_gap_reset():
    """
    日内长中断(>600s)防御:
    虽未跨日，但 10:00 -> 10:15 的长间隔应触发 stale state reset。
    """
    broker = MockBroker(initial_cash=10000.0)
    data = _make_data()

    broker._pending_sells.add("SELL_STALE_1")
    broker._active_buys["BUY_STALE_1"] = {
        "data": data,
        "shares": 100,
        "price": 10.0,
        "lot_size": 100,
        "retries": 0,
    }
    broker._virtual_spent_cash = 1000.0

    broker.set_datetime(datetime(2026, 2, 17, 10, 0, 0))
    broker.set_datetime(datetime(2026, 2, 17, 10, 15, 0))

    assert len(broker._pending_sells) == 0, "日内长中断后 _pending_sells 必须被强制清空"
    assert len(broker._active_buys) == 0, "日内长中断后 _active_buys 必须被强制清空"
    assert broker._virtual_spent_cash == pytest.approx(0.0), "日内长中断后虚拟占资必须清空"


def test_scheduled_intraday_gap_does_not_reset_stale_state():
    """
    schedule 兼容:
    30m/1h 这类调度间隔天然超过 10 分钟，不能按长中断清理日内在途状态。
    """
    broker = MockBroker(initial_cash=10000.0)
    broker._context.schedule_rule = "1h:09:30:00"
    data = _make_data()

    broker._pending_sells.add("SELL_STALE_1")
    broker._active_buys["BUY_STALE_1"] = {
        "data": data,
        "shares": 100,
        "price": 10.0,
        "lot_size": 100,
        "retries": 0,
    }
    broker._virtual_spent_cash = 1000.0

    broker.set_datetime(datetime(2026, 2, 17, 10, 0, 0))
    broker.set_datetime(datetime(2026, 2, 17, 11, 0, 0))

    assert len(broker._pending_sells) == 1, "schedule 正常间隔不应清理 _pending_sells"
    assert len(broker._active_buys) == 1, "schedule 正常间隔不应清理 _active_buys"
    assert broker._virtual_spent_cash == pytest.approx(1000.0), "schedule 正常间隔不应清理虚拟占资"


def test_virtual_ledger_not_cleared_by_intraday_bar_progress():
    """
    占资口径回归:
    _virtual_spent_cash 只能在跨日时清零，日内 bar 推进(例如 10:00 -> 10:01)不应清零。
    """
    broker = MockBroker(initial_cash=10000.0)

    broker.set_datetime(datetime(2026, 2, 17, 10, 0, 0))
    broker._virtual_spent_cash = 1234.5

    # 日内正常推进
    broker.set_datetime(datetime(2026, 2, 17, 10, 1, 0))
    assert broker._virtual_spent_cash == pytest.approx(1234.5), (
        "日内 bar 推进不应清零 _virtual_spent_cash。"
    )

    # 跨日推进
    broker.set_datetime(datetime(2026, 2, 18, 9, 31, 0))
    assert broker._virtual_spent_cash == pytest.approx(0.0), (
        "跨日时必须清零 _virtual_spent_cash。"
    )


def test_cross_day_reset_still_cleans_pending_and_active():
    """
    跨日恢复兜底:
    只要存在 _pending_sells/_active_buys 脏状态，就必须触发 reset。
    """
    broker = MockBroker(initial_cash=10000.0)
    data = _make_data()

    broker.set_datetime(datetime(2026, 2, 16, 14, 55, 0))
    broker._pending_sells.add("SELL_STALE_1")
    broker._active_buys["BUY_STALE_1"] = {
        "data": data,
        "shares": 100,
        "price": 10.0,
        "lot_size": 100,
        "retries": 0,
    }
    broker._virtual_spent_cash = 1000.0

    assert len(broker._pending_sells) == 1, "前置失败：pending_sells 注入失败。"
    assert len(broker._active_buys) == 1, "前置失败：active_buys 注入失败。"

    broker.set_datetime(datetime(2026, 2, 17, 9, 31, 0))

    assert len(broker._pending_sells) == 0, "跨日后 _pending_sells 必须被清空。"
    assert len(broker._active_buys) == 0, "跨日后 _active_buys 必须被清空。"


def test_cash_override_and_virtual_ledger_exhaustion():
    """
    资金覆写 + 虚拟账本耗尽:
    - 总现金 10 万，但策略可用额度 override=2 万
    - 第 1 单买 1500 股(~15000)
    - 第 2 单再买 1000 股(~10000)时应因剩余额度不足触发自动降级并 lot 向下取整
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data()
    broker._cash_override = 20000.0

    assert broker.get_cash() == pytest.approx(20000.0), "cash_override 生效失败: 初始可用资金应被限制为 20000"

    first = broker.order_target_value(data, target=15000.0)  # 1500 股
    assert first is not None, "第一笔买单应成功发出"
    assert len(broker.submitted_orders) == 1, "第一笔买单后应有 1 笔订单"
    assert broker.submitted_orders[0]["side"] == "BUY", "第一笔订单方向应为 BUY"
    assert broker.submitted_orders[0]["volume"] == 1500, "第一笔订单数量应为 1500 股"

    # 模拟策略资金池只保留 override 额度口径，确保虚拟账本可直接消耗该额度。
    broker.mock_cash = 20000.0
    cash_after_first = broker.get_cash()
    assert cash_after_first < 5000.0 + 1.0, "第一笔后剩余可用额度应约为 5000（含费用估算误差）"

    # 这里直接调用 _smart_buy_value，隔离验证“资金不足 -> 自动降级”逻辑，
    # 避免被 expected_size 的在途仓位穿透规则改写为卖出分支。
    second = broker._smart_buy_value(data, shares=1000.0, price=10.0, target_value=10000.0)
    assert second is not None, "第二笔应触发降级后继续发单，而不是直接丢弃"
    assert len(broker.submitted_orders) == 2, "第二笔降级订单应成功提交"
    assert broker.submitted_orders[1]["side"] == "BUY", "第二笔订单方向应为 BUY"
    assert broker.submitted_orders[1]["volume"] == 400, "第二笔应按剩余额度降级并 lot 取整到 400 股"


def test_buy_order_canceled_virtual_cash_leak():
    """
    Red Team Test:
    高危漏洞检测 - 买单被人工撤销(Canceled)后，验证虚拟账本是否正确释放资金。
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data()

    # 先发起一笔 1000 股买单，预期在虚拟账本里预扣 1000 * 10 * safety_multiplier
    first = broker.order_target_value(data, target=10000.0)
    assert first is not None, "前置失败：1000 股买单应成功发出"
    assert first.id == "ORDER_1", "前置失败：首笔订单 ID 应为 ORDER_1"
    assert "ORDER_1" in broker._active_buys, "前置失败：活跃买单跟踪器中应包含 ORDER_1"

    pre_deduct = 1000 * 10.0 * broker.safety_multiplier
    assert broker._virtual_spent_cash == pytest.approx(pre_deduct), "前置失败：首笔买单的虚拟预扣金额不正确"

    # 模拟用户在柜台端手动撤单 -> 回调 Canceled
    broker.on_order_status(MockOrderProxy("ORDER_1", is_buy_order=True, status="Canceled"))

    # 断言1：活跃订单应移除
    assert "ORDER_1" not in broker._active_buys, "买单撤销后，_active_buys 未清理，存在状态机脏数据风险！"

    # 断言2（核心）：虚拟资金必须回退，否则会出现“幽灵占资”
    assert broker._virtual_spent_cash == pytest.approx(0.0), (
        "买单撤销后，虚拟资金未回退，发生幽灵账本泄漏！"
    )


def test_buy_order_filled_releases_virtual_cash():
    """
    占资终态回归:
    买单 Filled 后，_virtual_spent_cash 必须回退到 0，避免与柜台已扣现金发生双重扣减。
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data()

    first = broker.order_target_value(data, target=10000.0)  # 1000 股
    assert first is not None, "前置失败：首笔买单应成功发出"
    assert first.id == "ORDER_1", "前置失败：首笔订单 ID 应为 ORDER_1"
    assert "ORDER_1" in broker._active_buys, "前置失败：活跃买单跟踪器中应包含 ORDER_1"

    pre_deduct = 1000 * 10.0 * broker.safety_multiplier
    assert broker._virtual_spent_cash == pytest.approx(pre_deduct), "前置失败：首笔买单虚拟预扣金额异常"

    # 模拟柜台成交后物理现金已扣减
    broker.mock_cash = 90000.0
    broker.on_order_status(MockOrderProxy("ORDER_1", is_buy_order=True, status="Filled"))

    assert "ORDER_1" not in broker._active_buys, "买单成交后，_active_buys 未清理，存在状态机脏数据风险！"
    assert broker._virtual_spent_cash == pytest.approx(0.0), (
        "买单成交后，虚拟资金未回退，发生可用资金双重扣减风险！"
    )
    assert broker.get_cash() == pytest.approx(90000.0), (
        "买单成交后 get_cash 应与柜台实扣现金对齐，不能继续被虚拟账本二次扣减。"
    )


def test_buy_order_expired_releases_virtual_cash_without_retry():
    """
    非在途终态回归:
    买单进入非 pending 终态(如 Expired)时，必须回退虚拟占资，但不能继续重试。
    """
    broker = MockBroker(initial_cash=100000.0)
    data = _make_data()

    first = broker.order_target_value(data, target=10000.0)
    assert first is not None
    assert "ORDER_1" in broker._active_buys

    broker.on_order_status(MockOrderProxy("ORDER_1", is_buy_order=True, status="Expired"))

    assert "ORDER_1" not in broker._active_buys, "Expired 买单应从 _active_buys 清理"
    assert broker._virtual_spent_cash == pytest.approx(0.0), "Expired 买单应回退虚拟占资"
    assert len(broker.submitted_orders) == 1, "Expired 买单不应触发重试"


def test_manual_force_reset_recovery():
    """
    Red Team Test:
    极端灾难恢复 - 在内部状态机乱套后，force_reset_state 应兜底清理并恢复可用资金。
    """
    broker = MockBroker(initial_cash=100000.0)

    # 构造“乱套状态”：虚拟占资异常 + 卖单监控残留
    broker._virtual_spent_cash = 43210.0
    broker._pending_sells.add("SELL_STUCK_1")

    # 前置校验，确保脏状态确实存在
    assert broker._virtual_spent_cash > 0, "前置失败：虚拟占资注入失败"
    assert len(broker._pending_sells) == 1, "前置失败：_pending_sells 注入失败"

    # 执行灾备重置，并立即同步余额
    broker.force_reset_state()
    broker.sync_balance()

    # 状态清空断言
    assert len(broker._pending_sells) == 0, "强制重置失败：_pending_sells 未被清空，可能导致买单永久阻塞！"

    # 现金恢复断言（核心）：可用现金必须回到真实资金水平
    real_cash = broker._fetch_real_cash()
    assert broker.get_cash() == pytest.approx(real_cash), (
        "强制重置后可用现金未恢复到真实余额，虚拟账本仍在错误占资！"
    )
