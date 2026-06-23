import sys
import datetime
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest


# 1. 拦截 gm 模块导入，注入 Mock 模块
mock_gm = MagicMock()
mock_gm_api = MagicMock()

# 定义掘金状态常量 (模拟值)
mock_gm_api.OrderStatus_New = 1
mock_gm_api.OrderStatus_PartiallyFilled = 2
mock_gm_api.OrderStatus_Filled = 3
mock_gm_api.OrderStatus_Canceled = 4
mock_gm_api.OrderStatus_Rejected = 5
mock_gm_api.OrderStatus_PendingNew = 6
mock_gm_api.OrderStatus_PendingCancel = 7
mock_gm_api.OrderStatus_Expired = 8
mock_gm_api.OrderSide_Buy = 1
mock_gm_api.OrderSide_Sell = 2
mock_gm_api.OrderType_Market = 11
mock_gm_api.OrderType_Limit = 12

sys.modules["gm"] = mock_gm
sys.modules["gm.api"] = mock_gm_api

# 延迟导入被测模块，确保它使用上述 Mock 的 gm.api
sys.modules.pop("live_trader.adapters.gm_broker", None)
from live_trader.adapters.gm_broker import GmOrderProxy, GmBrokerAdapter
from live_trader.data_bridge.data_warm import SchedulePlanner


# 2. 构造掘金底层订单替身
class DummyGMOrder:
    def __init__(self, status, side=1, filled_volume=0, filled_vwap=0.0, commission=0.0):
        self.cl_ord_id = "GM_TEST_001"
        self.status = status
        self.side = side
        self.filled_volume = filled_volume
        self.filled_vwap = filled_vwap
        self.commission = commission
        # 故意不设置 filled_amount，用于测试 fallback 逻辑


def test_gm_status_translation_accuracy():
    """
    Red Team Test:
    验证 GmOrderProxy 对部成/拒单/撤单状态的翻译是否准确，避免状态机误判。
    """
    # 1) 部成 (PartiallyFilled): 应保持 pending，不可视作 completed
    partial_order = DummyGMOrder(status=mock_gm_api.OrderStatus_PartiallyFilled)
    partial_proxy = GmOrderProxy(partial_order, is_live=True)
    assert partial_proxy.is_pending(), "状态翻译错误：部成单必须保持 pending，不能提前释放监控！"
    assert not partial_proxy.is_completed(), "状态翻译错误：部成单不能被判定为 completed！"

    # 2) 拒单 (Rejected): 应标记 rejected，且不再 pending
    rejected_order = DummyGMOrder(status=mock_gm_api.OrderStatus_Rejected)
    rejected_proxy = GmOrderProxy(rejected_order, is_live=True)
    assert rejected_proxy.is_rejected(), "状态翻译错误：拒单必须触发 is_rejected()=True！"
    assert not rejected_proxy.is_pending(), "状态翻译错误：拒单不应继续处于 pending！"

    # 3) 撤单 (Canceled): 应标记 canceled，且不再 pending
    canceled_order = DummyGMOrder(status=mock_gm_api.OrderStatus_Canceled)
    canceled_proxy = GmOrderProxy(canceled_order, is_live=True)
    assert canceled_proxy.is_canceled(), "状态翻译错误：撤单必须触发 is_canceled()=True！"
    assert not canceled_proxy.is_pending(), "状态翻译错误：撤单不应继续处于 pending！"

    # 4) 废单/过期 (Expired): 应明确离开 pending，避免本地卖单监控永久等待。
    expired_order = DummyGMOrder(status=mock_gm_api.OrderStatus_Expired)
    expired_proxy = GmOrderProxy(expired_order, is_live=True)
    assert not expired_proxy.is_completed(), "过期单不能被误判为成交。"
    assert not expired_proxy.is_pending(), "过期单必须离开 pending，避免无限等待。"
    assert not expired_proxy.is_accepted(), "过期单不能继续被视为 accepted。"

    # 5) 撤单中 (PendingCancel): 仍是柜台在途态，必须继续等待最终撤单/成交回报。
    pending_cancel_order = DummyGMOrder(status=mock_gm_api.OrderStatus_PendingCancel)
    pending_cancel_proxy = GmOrderProxy(pending_cancel_order, is_live=True)
    assert pending_cancel_proxy.is_pending(), "撤单中仍属于在途态，应继续等待最终回报。"


def test_gm_executed_stats_fallback():
    """
    Red Team Test:
    验证成交金额缺失字段时的兜底逻辑: executed.value = filled_volume * filled_vwap。
    """
    filled_order = DummyGMOrder(
        status=mock_gm_api.OrderStatus_Filled,
        side=mock_gm_api.OrderSide_Buy,
        filled_volume=1000,
        filled_vwap=10.5,
        commission=12.3,
    )
    proxy = GmOrderProxy(filled_order, is_live=True)
    executed = proxy.executed

    assert executed.size == 1000, "成交统计错误：executed.size 应等于 filled_volume=1000！"
    assert executed.price == 10.5, "成交统计错误：executed.price 应等于 filled_vwap=10.5！"
    assert executed.value == pytest.approx(10500.0), "容错失败：缺失 filled_amount 时，executed.value 应回退为 1000*10.5=10500！"
    assert executed.comm == pytest.approx(12.3), "成交统计错误：executed.comm 应等于 commission 字段！"


def test_gm_executed_stats_exposes_execution_dt():
    """
    成交时间回归:
    GmOrderProxy.executed.dt 应优先暴露柜台回报中的实际更新时间。
    """
    filled_order = DummyGMOrder(
        status=mock_gm_api.OrderStatus_Filled,
        side=mock_gm_api.OrderSide_Buy,
        filled_volume=1000,
        filled_vwap=10.5,
        commission=12.3,
    )
    filled_order.updated_at = "2026-04-08 14:45:33.123456"

    proxy = GmOrderProxy(filled_order, is_live=True)
    assert proxy.executed.dt.isoformat() == "2026-04-08T14:45:33.123456"


def test_gm_live_vs_backtest_completion_logic():
    """
    Red Team Test:
    验证 PendingNew 在实盘与回测模式下的 completed 判定分歧，防止实盘误判已完成。
    """
    pending_new_order_live = DummyGMOrder(status=mock_gm_api.OrderStatus_PendingNew)
    pending_new_order_backtest = DummyGMOrder(status=mock_gm_api.OrderStatus_PendingNew)

    live_proxy = GmOrderProxy(pending_new_order_live, is_live=True)
    backtest_proxy = GmOrderProxy(pending_new_order_backtest, is_live=False)

    assert not live_proxy.is_completed(), "致命错误：实盘模式下 PendingNew 不能被视为已完成！"
    assert backtest_proxy.is_completed(), "兼容性错误：回测模式下 PendingNew 应被视为已完成！"


def test_gm_is_accepted_only_for_active_status():
    """
    适配器语义回归:
    is_accepted 仅应对在途态返回 True，终态(Filled/Canceled/Rejected)必须返回 False。
    """
    active_new = GmOrderProxy(DummyGMOrder(status=mock_gm_api.OrderStatus_New), is_live=True)
    active_partial = GmOrderProxy(DummyGMOrder(status=mock_gm_api.OrderStatus_PartiallyFilled), is_live=True)
    active_pending_new = GmOrderProxy(DummyGMOrder(status=mock_gm_api.OrderStatus_PendingNew), is_live=True)
    terminal_filled = GmOrderProxy(DummyGMOrder(status=mock_gm_api.OrderStatus_Filled), is_live=True)
    terminal_canceled = GmOrderProxy(DummyGMOrder(status=mock_gm_api.OrderStatus_Canceled), is_live=True)
    terminal_rejected = GmOrderProxy(DummyGMOrder(status=mock_gm_api.OrderStatus_Rejected), is_live=True)

    assert active_new.is_accepted(), "OrderStatus_New 应被视为 accepted。"
    assert active_partial.is_accepted(), "OrderStatus_PartiallyFilled 应被视为 accepted。"
    assert active_pending_new.is_accepted(), "OrderStatus_PendingNew 应被视为 accepted。"
    assert not terminal_filled.is_accepted(), "OrderStatus_Filled 不应被视为 accepted。"
    assert not terminal_canceled.is_accepted(), "OrderStatus_Canceled 不应被视为 accepted。"
    assert not terminal_rejected.is_accepted(), "OrderStatus_Rejected 不应被视为 accepted。"


def test_gm_expired_is_not_pending_and_not_accepted():
    expired_order = DummyGMOrder(status=mock_gm_api.OrderStatus_Expired)
    proxy = GmOrderProxy(expired_order, is_live=True)

    assert not proxy.is_pending(), "Expired 单必须离开 pending。"
    assert not proxy.is_accepted(), "Expired 单不能被视为 accepted。"


def test_gm_submit_order_live_limit_with_auto_downsize(monkeypatch):
    """
    实盘分支测试:
    - BUY 使用限价单
    - 资金不足时自动降仓并按整手取整
    """
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []

    # 覆盖导入降级路径下被置空的常量，确保测试聚焦交易逻辑本身。
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return [DummyGMOrder(status=mock_gm_api.OrderStatus_New, side=kwargs["side"])]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    # 避免 __init__ 阶段访问真实 SDK 返回值
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    broker = GmBrokerAdapter(context=MagicMock(), slippage_override=0.01, commission_override=0.0003)
    broker.is_live = True
    # 测试阶段固定可用资金，强制触发降仓
    monkeypatch.setattr(broker, "_fetch_real_cash", lambda: 20300.0)

    data = SimpleNamespace(_name="SHSE.600000")
    proxy = broker._submit_order(data=data, volume=3000, side="BUY", price=10.0)

    assert proxy is not None, "实盘下单应返回有效代理对象。"
    assert len(order_calls) == 1, "应实际调用一次 order_volume。"
    call = order_calls[0]

    expected_freeze_price = round(10.0 * (1 + 0.01), 4)  # 实盘 BUY 限价
    expected_buffer_rate = 1.0 + 0.0003 + 0.002
    expected_volume = int(20300.0 / (expected_freeze_price * expected_buffer_rate) // 100) * 100

    assert call["order_type"] == mock_gm_api.OrderType_Limit, "实盘应使用限价单。"
    assert call["price"] == pytest.approx(expected_freeze_price), "实盘 BUY 限价计算不正确。"
    assert call["volume"] == expected_volume, "GM 实盘二次降仓不应重复计入滑点。"
    assert 0 < call["volume"] < 3000, "该场景应发生实质降仓。"


def test_gm_submit_order_live_default_slippage_matches_launch_default(monkeypatch):
    """
    默认值一致性:
    未显式传 slippage 时，GM 实盘默认委托滑点应与 launch 默认值保持一致(0.0001)。
    """
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []

    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return [DummyGMOrder(status=mock_gm_api.OrderStatus_New, side=kwargs["side"])]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock(), commission_override=0.0003)
    broker.is_live = True
    monkeypatch.setattr(broker, "_fetch_real_cash", lambda: 1_000_000.0)

    data = SimpleNamespace(_name="SHSE.600000")
    proxy = broker._submit_order(data=data, volume=1000, side="BUY", price=10.0)

    assert proxy is not None, "默认滑点场景下应成功下单。"
    assert len(order_calls) == 1, "应实际调用一次 order_volume。"
    assert order_calls[0]["price"] == pytest.approx(10.001), "GM 实盘默认滑点应为 0.0001，而不是 0.01。"


def test_gm_submit_order_logs_when_cash_fit_falls_below_min_lot(monkeypatch, capsys):
    """
    小资金可观测性:
    二次降仓后若仍不足一手，不应静默返回 None，必须打印明确日志。
    """
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []

    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return [DummyGMOrder(status=mock_gm_api.OrderStatus_New, side=kwargs["side"])]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock(), slippage_override=0.0001, commission_override=0.0003)
    broker.is_live = True
    monkeypatch.setattr(broker, "_fetch_real_cash", lambda: 500.0)

    data = SimpleNamespace(_name="SHSE.600000")
    proxy = broker._submit_order(data=data, volume=1000, side="BUY", price=10.0)

    captured = capsys.readouterr()

    assert proxy is None, "不足一手时应直接放弃下单。"
    assert order_calls == [], "不足一手时不应真正调用 order_volume。"
    assert "insufficient for minimum lot" in captured.out, "不足一手时必须输出明确日志。"


def test_gm_submit_order_backtest_market_mode(monkeypatch):
    """
    回测分支测试:
    - BUY 使用市价单
    - 市价单价格应传 0（交由引擎撮合）
    """
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []

    # 覆盖导入降级路径下被置空的常量，确保测试聚焦交易逻辑本身。
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return [DummyGMOrder(status=mock_gm_api.OrderStatus_New, side=kwargs["side"])]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    broker = GmBrokerAdapter(context=MagicMock(), slippage_override=0.01, commission_override=0.0003)
    broker.is_live = False
    monkeypatch.setattr(broker, "_fetch_real_cash", lambda: 1_000_000.0)

    data = SimpleNamespace(_name="SHSE.600000")
    proxy = broker._submit_order(data=data, volume=1000, side="BUY", price=10.0)

    assert proxy is not None, "回测下单应返回有效代理对象。"
    assert len(order_calls) == 1, "应实际调用一次 order_volume。"
    call = order_calls[0]

    assert call["order_type"] == mock_gm_api.OrderType_Market, "回测应使用市价单。"
    assert call["price"] == 0, "回测市价单应传 price=0 交由撮合引擎决定。"
    assert call["volume"] == 1000, "资金充足场景不应降仓。"


def test_gm_secondary_downsize_updates_active_buy_and_virtual_ledger(monkeypatch):
    """
    回归测试:
    当 GM 在 _submit_order 内进行二次降仓时，基类应使用“真实受理数量”更新:
    - _active_buys[oid]['shares']
    - _virtual_spent_cash
    """
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []

    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    # 模拟柜台返回对象，携带最终受理 volume
    class SubmittedOrder(DummyGMOrder):
        def __init__(self, status, side, volume):
            super().__init__(status=status, side=side)
            self.volume = volume

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return [SubmittedOrder(status=mock_gm_api.OrderStatus_New, side=kwargs["side"], volume=kwargs["volume"])]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock(), slippage_override=0.01, commission_override=0.0003)
    broker.is_live = True
    # 关键构造:
    # - 基类 _smart_buy_value 看到 cash=10123.10 时不会先降仓
    # - GM _submit_order 用更贴近实盘的 freeze_price 二次校验后，会把 1000 股降到 900
    monkeypatch.setattr(broker, "_fetch_real_cash", lambda: 10123.10)
    monkeypatch.setattr(broker, "get_current_price", lambda data: 10.0)
    monkeypatch.setattr(broker, "get_pending_orders", lambda: [])

    data = SimpleNamespace(_name="SHSE.600000")
    proxy = broker.order_target_value(data=data, target=10000.0)  # expected_shares=1000

    assert proxy is not None, "应成功提交降仓后的买单。"
    assert len(order_calls) == 1, "应实际触发一次 order_volume。"
    assert order_calls[0]["volume"] == 900, "GM 二次降仓后真实委托量应为 900。"

    tracked = broker._active_buys.get(proxy.id)
    assert tracked is not None, "_active_buys 应记录该订单。"
    assert tracked["shares"] == 900, "活跃买单跟踪应使用真实受理数量，而非降仓前数量。"

    expected_ledger = 900 * 10.0 * broker.safety_multiplier
    assert broker._virtual_spent_cash == pytest.approx(expected_ledger), (
        "虚拟账本占资应基于真实受理数量计算。"
    )


def test_gm_sellable_position_prefers_available_now(monkeypatch):
    """
    持仓字段优先级:
    有 available_now 时，应优先使用 available_now 作为可卖仓位。
    """
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    pos = SimpleNamespace(
        symbol="SHSE.600000",
        volume=1000,
        vwap=10.0,
        available_now=300,
        available=900,
        volume_today=100,
    )
    ctx = SimpleNamespace(account=lambda: SimpleNamespace(positions=lambda: [pos]))
    broker = GmBrokerAdapter(context=ctx)

    data = SimpleNamespace(_name="SHSE.600000")
    got = broker.get_position(data)

    assert got.size == 1000, "持仓数量读取错误。"
    assert got.sellable == 300, "应优先使用 available_now 作为可卖仓位。"
    assert broker.get_sellable_position(data) == 300, "get_sellable_position 应与 get_position.sellable 一致。"


def test_gm_sellable_position_fallback_to_available_then_volume_today(monkeypatch):
    """
    持仓字段兜底:
    - available_now 缺失 -> 使用 available
    - available/available_now 都缺失 -> 使用 volume - volume_today
    """
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    p1 = SimpleNamespace(
        symbol="SHSE.600001",
        volume=1000,
        vwap=10.0,
        available=None,
        available_now=None,
        volume_today=200,
    )
    p2 = SimpleNamespace(
        symbol="SHSE.600002",
        volume=1000,
        vwap=10.0,
        available=650,
        available_now=None,
        volume_today=200,
    )
    ctx = SimpleNamespace(account=lambda: SimpleNamespace(positions=lambda: [p1, p2]))
    broker = GmBrokerAdapter(context=ctx)

    d1 = SimpleNamespace(_name="SHSE.600001")
    d2 = SimpleNamespace(_name="SHSE.600002")

    assert broker.get_sellable_position(d1) == 800, "回测兜底应使用 volume - volume_today。"
    assert broker.get_sellable_position(d2) == 650, "available_now 缺失时应使用 available。"


def test_gm_pending_order_contract_includes_id(monkeypatch):
    """
    最小契约:
    get_pending_orders 返回项必须包含 id，供基础层隔夜清理协议使用。
    """
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    pending = SimpleNamespace(
        cl_ord_id="GM_OID_001",
        symbol="SHSE.600000",
        side=mock_gm_api.OrderSide_Buy,
        volume=1000,
        filled_volume=200,
    )
    monkeypatch.setattr(mock_gm_api, "get_unfinished_orders", lambda: [pending], raising=False)

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True

    got = broker.get_pending_orders()
    assert len(got) == 1, "应返回 1 笔在途单。"
    assert got[0]["id"] == "GM_OID_001", "在途单契约缺失 id。"
    assert got[0]["symbol"] == "SHSE.600000"
    assert got[0]["direction"] == "BUY"
    assert got[0]["size"] == 800


def test_gm_pending_order_fetch_failure_is_marked(monkeypatch):
    """
    空列表语义回归:
    get_pending_orders 查询失败时仍可安全返回 []，但必须标记结果不可信。
    """
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    def _raise_pending_error():
        raise RuntimeError("gm pending unavailable")

    monkeypatch.setattr(mock_gm_api, "get_unfinished_orders", _raise_pending_error, raising=False)

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True

    got = broker.get_pending_orders()

    assert got == []
    assert broker._last_pending_orders_fetch_failed is True
    assert "gm pending unavailable" in str(broker._last_pending_orders_fetch_error)


def test_gm_pending_order_fetch_success_clears_failure_flag(monkeypatch):
    """
    成功语义回归:
    当 get_pending_orders 正常返回时，应清除上一轮失败标记。
    """
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    pending = SimpleNamespace(
        cl_ord_id="GM_OID_010",
        symbol="SHSE.600000",
        side=mock_gm_api.OrderSide_Sell,
        volume=1000,
        filled_volume=0,
    )
    monkeypatch.setattr(mock_gm_api, "get_unfinished_orders", lambda: [pending], raising=False)

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    broker._last_pending_orders_fetch_failed = True
    broker._last_pending_orders_fetch_error = "old error"

    got = broker.get_pending_orders()

    assert len(got) == 1
    assert broker._last_pending_orders_fetch_failed is False
    assert broker._last_pending_orders_fetch_error is None


def test_gm_cancel_pending_order_by_id(monkeypatch):
    """
    最小契约:
    cancel_pending_order(order_id) 应能根据 id 定位并发起撤单。
    """
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    pending = SimpleNamespace(
        cl_ord_id="GM_OID_002",
        symbol="SHSE.600000",
        side=mock_gm_api.OrderSide_Buy,
        volume=1000,
        filled_volume=0,
    )
    monkeypatch.setattr(mock_gm_api, "get_unfinished_orders", lambda: [pending], raising=False)
    # 兼容 `import gm.api as gm_api` 的导入路径，确保拿到同一 mock 对象
    mock_gm.api = mock_gm_api

    cancel_calls = []

    def _fake_order_cancel(arg):
        cancel_calls.append(arg)

    monkeypatch.setattr(mock_gm_api, "order_cancel", _fake_order_cancel, raising=False)
    monkeypatch.setattr(mock_gm_api, "cancel_order", None, raising=False)

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True

    ok = broker.cancel_pending_order("GM_OID_002")
    assert ok is True, "按 id 撤单应返回 True。"
    assert len(cancel_calls) == 1, "应至少发起一次撤单调用。"


def test_gm_schedule_prewarm_time_rule_uses_common_live_helper():
    """
    通用预热调度回归:
    GM 也应复用通用 fixed-slot prewarm 时间计算逻辑。
    """
    assert SchedulePlanner.build_schedule_prewarm_time_rule("1d:14:45:00", 60.0) == "14:44:00"
    assert SchedulePlanner.build_schedule_prewarm_time_rule("1d:09:30", 300.0) == "09:25:00"
    assert SchedulePlanner.build_schedule_prewarm_time_rule("5m:09:30:00", 60.0) == "09:29:00"
    assert SchedulePlanner.build_schedule_prewarm_time_rule("1h:09:30:00", 300.0) == "09:25:00"
    assert SchedulePlanner.build_schedule_prewarm_time_rule("1d:14:45:00", 0.0) is None
    assert SchedulePlanner.build_schedule_prewarm_time_rule("5m:14:45:00", 300.0) is None


def test_gm_schedule_preview_uses_common_live_helper():
    parsed = SchedulePlanner.parse_schedule_rule("1h:09:30:00")

    previews = SchedulePlanner.build_schedule_preview(
        now=datetime.datetime(2026, 4, 11, 10, 12, 0),
        parsed_schedule=parsed,
        prewarm_lead_seconds=300.0,
        count=3,
    )

    assert [item["slot_dt"].strftime("%Y-%m-%d %H:%M:%S") for item in previews] == [
        "2026-04-11 10:30:00",
        "2026-04-11 11:30:00",
        "2026-04-11 12:30:00",
    ]
    assert [item["prewarm_dt"].strftime("%Y-%m-%d %H:%M:%S") for item in previews] == [
        "2026-04-11 10:25:00",
        "2026-04-11 11:25:00",
        "2026-04-11 12:25:00",
    ]


def test_gm_run_schedule_prewarm_is_non_blocking_and_pushes_warning(monkeypatch):
    """
    通用预热执行回归:
    GM broker 走通用预热能力时，异常应只报警，不应抛出中断。
    """
    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.data_bridge.data_warm as data_warm_module
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(data_warm_module, "AlarmManager", lambda: DummyAlarm())
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    broker = GmBrokerAdapter(context=MagicMock())
    broker.datas = [SimpleNamespace(_name="SHSE.600000")]
    monkeypatch.setattr(
        broker,
        "get_current_price",
        lambda data: (_ for _ in ()).throw(RuntimeError("gm cold connection")),
    )

    summary = broker.run_schedule_prewarm(
        schedule_rule="1d:14:45:00",
        data_provider=None,
        symbols=["SHSE.600000"],
        timeframe="Days",
        compression=1,
    )

    assert summary["source"] == "broker"
    assert summary["symbol"] == "SHSE.600000"
    assert summary["errors"] == ["broker:gm cold connection"]
    assert len(pushed) == 1
    assert pushed[0]["level"] == "WARNING"
    assert "Schedule prewarm finished with errors before 1d:14:45:00" in pushed[0]["content"]
    assert "Normal schedule will continue." in pushed[0]["content"]


def test_gm_launch_restarts_when_shutdown_callback_fires(monkeypatch, capsys):
    """
    GM 实盘 session 自愈:
    SDK 触发 on_shutdown 后，应退出当前 session 并交给 Phoenix 外层重启，
    不能把进程当作正常结束。
    """
    import live_trader.adapters.gm_broker as gm_module

    statuses = []
    fake_context = SimpleNamespace()

    class DummyAlarm:
        def push_status(self, status, detail=''):
            statuses.append((status, detail))

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    def _poll_once_then_shutdown():
        fake_context.on_shutdown_fun(fake_context)
        return 0

    def _sleep(seconds):
        if seconds >= 10:
            raise KeyboardInterrupt()

    monkeypatch.setattr(gm_module, "MODE_LIVE", "live", raising=False)
    monkeypatch.setattr(gm_module, "MODE_BACKTEST", "backtest", raising=False)
    monkeypatch.setattr(gm_module, "context", fake_context, raising=False)
    monkeypatch.setattr(gm_module, "set_token", lambda token: None, raising=False)
    monkeypatch.setattr(gm_module, "set_serv_addr", lambda addr: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_strategy_id", lambda strategy_id: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_set_mode", lambda mode: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_data_callback", lambda callback: None, raising=False)
    monkeypatch.setattr(gm_module, "callback_controller", object(), raising=False)
    monkeypatch.setattr(gm_module, "gmi_init", lambda: 0, raising=False)
    monkeypatch.setattr(gm_module, "check_gm_status", lambda status: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_once_then_shutdown, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr("time.sleep", _sleep)

    GmBrokerAdapter.launch(
        {"token": "token", "strategy_id": "strategy-id"},
        strategy_path="sample_strategy",
        params={},
    )

    captured = capsys.readouterr()
    assert "[System] Strategy Shutdown" in captured.out
    assert "GM shutdown callback received. Restarting session" in captured.out
    assert "[Phoenix] Waiting 10s before restart" in captured.out
    assert ("INFO", "GM Session Shutdown (Preparing to Restart)") in statuses


def test_gm_launch_converts_sdk_system_exit_to_restart(monkeypatch, capsys):
    """
    GM SDK 可能在底层 shutdown 时抛 SystemExit。launch 必须拦截并重启
    当前 session，避免策略进程被 SDK 直接带退出。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    def _poll_raises_system_exit():
        fake_context.on_shutdown_fun(fake_context)
        raise SystemExit(0)

    def _sleep(seconds):
        if seconds >= 10:
            raise KeyboardInterrupt()

    monkeypatch.setattr(gm_module, "MODE_LIVE", "live", raising=False)
    monkeypatch.setattr(gm_module, "MODE_BACKTEST", "backtest", raising=False)
    monkeypatch.setattr(gm_module, "context", fake_context, raising=False)
    monkeypatch.setattr(gm_module, "set_token", lambda token: None, raising=False)
    monkeypatch.setattr(gm_module, "set_serv_addr", lambda addr: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_strategy_id", lambda strategy_id: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_set_mode", lambda mode: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_data_callback", lambda callback: None, raising=False)
    monkeypatch.setattr(gm_module, "callback_controller", object(), raising=False)
    monkeypatch.setattr(gm_module, "gmi_init", lambda: 0, raising=False)
    monkeypatch.setattr(gm_module, "check_gm_status", lambda status: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_raises_system_exit, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr("time.sleep", _sleep)

    GmBrokerAdapter.launch(
        {"token": "token", "strategy_id": "strategy-id"},
        strategy_path="sample_strategy",
        params={},
    )

    captured = capsys.readouterr()
    assert "GM SDK requested process exit" in captured.out
    assert "[Phoenix] Waiting 10s before restart" in captured.out


def test_gm_launch_does_not_swallow_plain_system_exit(monkeypatch):
    """
    普通 SystemExit 可能来自进程信号处理或上层主动退出；没有 GM shutdown
    回调证据时，不能被 Phoenix 误判为 SDK 断线并重启。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    def _poll_raises_system_exit():
        raise SystemExit(0)

    monkeypatch.setattr(gm_module, "MODE_LIVE", "live", raising=False)
    monkeypatch.setattr(gm_module, "MODE_BACKTEST", "backtest", raising=False)
    monkeypatch.setattr(gm_module, "context", fake_context, raising=False)
    monkeypatch.setattr(gm_module, "set_token", lambda token: None, raising=False)
    monkeypatch.setattr(gm_module, "set_serv_addr", lambda addr: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_strategy_id", lambda strategy_id: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_set_mode", lambda mode: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_data_callback", lambda callback: None, raising=False)
    monkeypatch.setattr(gm_module, "callback_controller", object(), raising=False)
    monkeypatch.setattr(gm_module, "gmi_init", lambda: 0, raising=False)
    monkeypatch.setattr(gm_module, "check_gm_status", lambda status: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_raises_system_exit, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)

    with pytest.raises(SystemExit):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )
