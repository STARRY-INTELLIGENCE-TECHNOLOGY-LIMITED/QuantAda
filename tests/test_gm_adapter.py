import sys
import datetime
import threading
import time
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
mock_gm_api.PositionEffect_Open = 1
mock_gm_api.PositionEffect_Close = 2
mock_gm_api.OrderType_Market = 11
mock_gm_api.OrderType_Limit = 12

sys.modules["gm"] = mock_gm
sys.modules["gm.api"] = mock_gm_api

# 延迟导入被测模块，确保它使用上述 Mock 的 gm.api
sys.modules.pop("live_trader.adapters.gm_broker", None)
from live_trader.adapters.gm_broker import GmOrderProxy, GmBrokerAdapter
from live_trader.data_bridge.data_warm import SchedulePlanner


@pytest.fixture(autouse=True)
def _isolate_gm_order_config(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module

    # GM 用例验证 A 股适配语义，不依赖开源仓库面向国际市场的中性默认值。
    monkeypatch.setattr(gm_module.config, "LOT_SIZE", 100)
    monkeypatch.setattr(gm_module.config, "BROKER_LOT_LIMITS", 0)


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


def test_gm_order_proxy_reads_dict_order_volume():
    """轻量 SDK 替身返回普通字典时，基础层仍能按真实委托量记账。"""
    order = {
        "cl_ord_id": "GM_DICT_001",
        "status": mock_gm_api.OrderStatus_New,
        "side": mock_gm_api.OrderSide_Buy,
        "volume": 300,
    }

    proxy = GmOrderProxy(order, is_live=True)

    assert proxy.id == "GM_DICT_001"
    assert proxy.is_buy()
    assert proxy.is_accepted()
    assert proxy.submitted_size == 300
    assert proxy.requested_size == 300


def test_gm_get_current_price_returns_zero_on_quote_failure(monkeypatch):
    """行情接口异常只能阻止本次下单，不能把异常抛进调度主循环。"""
    import live_trader.adapters.gm_broker as gm_module

    def _raise_current(**kwargs):
        raise RuntimeError("quote unavailable")

    monkeypatch.setattr(gm_module, "current", _raise_current)
    broker = GmBrokerAdapter(context=MagicMock())

    assert broker.get_current_price(SimpleNamespace(_name="SHSE.518880")) == 0.0


def test_gm_submit_order_live_market_with_auto_downsize(monkeypatch):
    """
    实盘分支测试:
    - BUY 使用市价单，并以保护价估算冻结资金
    - 资金不足时自动降仓并按整手取整
    """
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []

    # 覆盖导入降级路径下被置空的常量，确保测试聚焦交易逻辑本身。
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)
    monkeypatch.setattr(gm_module, "current", lambda symbols: [{"price": 10.0, "quotes": []}])

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

    expected_freeze_price = round(10.0 * (1 + 0.01), 4)  # 国内市场市价 BUY 保护价
    expected_cost_multiplier = 1.0 + 0.0003
    expected_volume = int(20300.0 / (expected_freeze_price * expected_cost_multiplier) // 100) * 100

    assert call["order_type"] == mock_gm_api.OrderType_Market, "实盘 BUY 应使用市价单。"
    assert call["side"] == mock_gm_api.OrderSide_Buy
    assert call["position_effect"] == mock_gm_api.PositionEffect_Open
    assert call["price"] == pytest.approx(expected_freeze_price), "实盘 BUY 保护价计算不正确。"
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
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "current", lambda symbols: [{"price": 10.0, "quotes": []}])
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
    assert order_calls[0]["order_type"] == mock_gm_api.OrderType_Market
    assert order_calls[0]["price"] == pytest.approx(10.001), "GM 实盘默认保护价应体现 0.0001 滑点。"


def test_gm_submit_order_logs_when_cash_fit_falls_below_min_lot(monkeypatch, capsys):
    """
    小资金可观测性:
    二次降仓后若仍不足一手，不应静默返回 None，必须打印明确日志。
    """
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []

    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "current", lambda symbols: [{"price": 10.0, "quotes": []}])
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


def test_gm_shse_market_buy_uses_best_ask_as_protection_price(monkeypatch):
    """沪市 BUY 的市价保护价必须覆盖实时最优卖价，避免伪限价滞留。"""
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)
    monkeypatch.setattr(
        gm_module,
        "current",
        lambda symbols: [{"price": 9.24, "quotes": [{"ask_p": 9.25, "ask_v": 1000}]}],
    )
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        order = DummyGMOrder(status=mock_gm_api.OrderStatus_New, side=kwargs["side"])
        order.volume = kwargs["volume"]
        order.cl_ord_id = "GM_BUY_MARKET_ASK"
        return [order]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock(), commission_override=0.0003)
    broker.is_live = True
    monkeypatch.setattr(broker, "_fetch_real_cash", lambda: 100_000.0)

    proxy = broker._submit_order(
        data=SimpleNamespace(_name="SHSE.518880"),
        volume=1000,
        side="BUY",
        price=9.24,
    )

    assert proxy is not None
    assert order_calls[0]["order_type"] == mock_gm_api.OrderType_Market
    assert order_calls[0]["price"] == pytest.approx(9.2509)
    assert proxy.reserved_cash == pytest.approx(1000 * 9.2509 * 1.0003)


def test_gm_szse_market_buy_uses_best_ask_as_protection_price(monkeypatch):
    """深市 BUY 与沪市使用同一市价保护价逻辑。"""
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(
        gm_module,
        "current",
        lambda symbols: [{"price": 10.0, "quotes": [{"ask_p": 10.05, "ask_v": 1000}]}],
    )
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        order = DummyGMOrder(status=mock_gm_api.OrderStatus_New, side=kwargs["side"])
        order.volume = kwargs["volume"]
        order.cl_ord_id = "GM_SZSE_BUY_MARKET"
        return [order]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock(), commission_override=0.0003)
    broker.is_live = True
    monkeypatch.setattr(broker, "_fetch_real_cash", lambda: 100_000.0)

    proxy = broker._submit_order(
        data=SimpleNamespace(_name="SZSE.159915"),
        volume=1000,
        side="BUY",
        price=10.0,
    )

    assert proxy is not None
    assert order_calls[0]["order_type"] == mock_gm_api.OrderType_Market
    assert order_calls[0]["price"] == pytest.approx(10.051)
    assert proxy.reserved_cash == pytest.approx(1000 * 10.051 * 1.0003)


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
        def __init__(self, status, side, volume, order_id):
            super().__init__(status=status, side=side)
            self.volume = volume
            self.cl_ord_id = order_id

    available_cash = {"value": 10103.01}

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        available_cash["value"] = 0.0
        return [SubmittedOrder(
            status=mock_gm_api.OrderStatus_New,
            side=kwargs["side"],
            volume=kwargs["volume"],
            order_id=f"GM_TEST_{len(order_calls):03d}",
        )]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock(), slippage_override=0.01, commission_override=0.0003)
    broker.is_live = True
    # 关键构造:
    # - 基类 _smart_buy_value 看到 cash=10103.01 时不会先降仓
    # - GM _submit_order 用更贴近实盘的 freeze_price 二次校验后，会把 1000 股降到 900
    monkeypatch.setattr(broker, "_fetch_real_cash", lambda: available_cash["value"])
    monkeypatch.setattr(broker, "get_current_price", lambda data: 10.0)
    monkeypatch.setattr(broker, "get_pending_orders", lambda: [])

    data = SimpleNamespace(_name="SHSE.600000")
    proxy = broker.order_target_value(data=data, target=10000.0)  # expected_shares=1000

    assert proxy is not None, "应成功提交降仓后的买单。"
    assert [call["volume"] for call in order_calls] == [900], (
        "保护价口径下剩余现金不足一手时，不得为了凑目标而超预算补单。"
    )

    assert [item["shares"] for item in broker._active_buys.values()] == [900]

    expected_ledger = 900 * 10.1 * (1.0 + 0.0003)
    assert broker._virtual_spent_cash == pytest.approx(expected_ledger), (
        "虚拟账本占资应基于真实受理数量计算。"
    )


def test_gm_live_buy_split_uses_single_batch_cash_budget(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module.config, "LOT_SIZE", 100)
    monkeypatch.setattr(gm_module.config, "BROKER_LOT_LIMITS", 1000)
    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "current", lambda symbols: [{"price": 10.0, "quotes": []}])
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    order_calls = []

    class SubmittedOrder(DummyGMOrder):
        def __init__(self, status, side, volume, oid):
            super().__init__(status=status, side=side)
            self.volume = volume
            self.cl_ord_id = oid

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return [SubmittedOrder(
            status=mock_gm_api.OrderStatus_New,
            side=kwargs["side"],
            volume=kwargs["volume"],
            oid=f"GM_SPLIT_{len(order_calls)}",
        )]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    cash_reads = []

    def _fetch_cash():
        cash_reads.append(True)
        # 若子单重新读取柜台现金，会模拟首单冻结后的低余额；正确实现应使用批次预算。
        return 20_000.0 if len(cash_reads) == 1 else 1_000.0

    monkeypatch.setattr(broker, "_fetch_real_cash", _fetch_cash)
    monkeypatch.setattr(broker, "get_position", lambda data: SimpleNamespace(size=0))
    monkeypatch.setattr(broker, "get_pending_orders", lambda: [])
    monkeypatch.setattr(broker, "get_current_price", lambda data: 10.0)

    proxy = broker.order_target_value(
        data=SimpleNamespace(_name="SHSE.512010"),
        target=14_000.0,
    )

    assert proxy is not None
    assert [call["volume"] for call in order_calls] == [1000, 400]
    assert len(cash_reads) == 1, "同一拆单批次不应因柜台冻结而重复扣减可用资金。"
    assert set(broker._active_buys) == {"GM_SPLIT_1", "GM_SPLIT_2"}


def test_gm_live_sell_splits_by_broker_lot_limit(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module.config, "LOT_SIZE", 100)
    monkeypatch.setattr(gm_module.config, "BROKER_LOT_LIMITS", 1_000_000)
    monkeypatch.setattr(gm_module, "OrderType_Market", mock_gm_api.OrderType_Market, raising=False)
    monkeypatch.setattr(gm_module, "OrderType_Limit", mock_gm_api.OrderType_Limit, raising=False)
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    order_calls = []

    class SubmittedOrder(DummyGMOrder):
        def __init__(self, status, side, volume, oid):
            super().__init__(status=status, side=side)
            self.volume = volume
            self.cl_ord_id = oid

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return [SubmittedOrder(
            status=mock_gm_api.OrderStatus_New,
            side=kwargs["side"],
            volume=kwargs["volume"],
            oid=f"GM_SELL_SPLIT_{len(order_calls)}",
        )]

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    monkeypatch.setattr(
        broker,
        "get_position",
        lambda data: SimpleNamespace(size=1_486_700, sellable=1_486_700),
    )
    monkeypatch.setattr(broker, "get_pending_orders", lambda: [])
    monkeypatch.setattr(broker, "get_current_price", lambda data: 0.37)

    proxy = broker.order_target_value(
        data=SimpleNamespace(_name="SZSE.159915"),
        target=0.0,
    )

    assert proxy is not None
    assert [call["volume"] for call in order_calls] == [1_000_000, 486_700]
    assert all(call["side"] == mock_gm_api.OrderSide_Sell for call in order_calls)
    assert all(call["position_effect"] == mock_gm_api.PositionEffect_Close for call in order_calls)
    assert proxy.batch_order_ids == ("GM_SELL_SPLIT_1", "GM_SELL_SPLIT_2")
    assert proxy.batch_submitted_size == 1_486_700
    assert proxy.batch_submit_failed is False
    assert broker._pending_sells == {"GM_SELL_SPLIT_1", "GM_SELL_SPLIT_2"}
    assert all(call["order_type"] == mock_gm_api.OrderType_Market for call in order_calls)
    assert all(call["price"] == pytest.approx(0.37) for call in order_calls), (
        "国内市场实盘市价 SELL 必须把实时价格作为 GM 要求的保护价传入，"
        "但 order_type 仍应是 OrderType_Market。"
    )


def test_gm_cn_market_sell_skips_without_protection_price(monkeypatch):
    """国内市场市价单没有有效保护价时不得提交必然被柜台拒绝的委托。"""
    import live_trader.adapters.gm_broker as gm_module

    order_calls = []

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return []

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    result = broker._submit_order(
        SimpleNamespace(_name="SZSE.159915"),
        100,
        "SELL",
        0.0,
    )

    assert result is None
    assert order_calls == []


def test_gm_adapter_final_guard_blocks_oversized_direct_submission(monkeypatch):
    """即使绕过 BaseLiveBroker，GM 适配器也不能发送超单笔上限的委托。"""
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module.config, "LOT_SIZE", 100)
    monkeypatch.setattr(gm_module.config, "BROKER_LOT_LIMITS", 1_000_000)
    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=10_000_000, nav=10_000_000))

    order_calls = []

    def _fake_order_volume(**kwargs):
        order_calls.append(kwargs)
        return []

    monkeypatch.setattr(gm_module, "order_volume", _fake_order_volume)

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    broker._runtime_config = {"LOT_SIZE": 100, "BROKER_LOT_LIMITS": 1_000_000}

    result = broker._submit_order(
        SimpleNamespace(_name="SHSE.512010"),
        1_378_500,
        "SELL",
        0.40,
    )

    assert result is None
    assert order_calls == [], "超限委托不得抵达 GM order_volume。"


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


def test_gm_live_position_uses_synchronous_counter_snapshot(monkeypatch):
    """实盘 SELL 对账不能依赖被当前 schedule 回调阻塞的 context 持仓缓存。"""
    import live_trader.adapters.gm_broker as gm_module

    cached_pos = SimpleNamespace(
        symbol="SHSE.513050",
        volume=488000,
        vwap=1.14,
        available_now=488000,
    )
    ctx = SimpleNamespace(account=lambda: SimpleNamespace(positions=lambda: [cached_pos]))
    broker = GmBrokerAdapter(context=ctx)
    broker.is_live = True

    counter_queries = []
    monkeypatch.setattr(
        gm_module,
        "gm_get_position",
        lambda: counter_queries.append(True) or [],
    )

    got = broker.get_position(SimpleNamespace(_name="SHSE.513050"))

    assert counter_queries == [True]
    assert got.size == 0, "柜台已空仓时不得继续读取 schedule 进入前的 488000 缓存。"
    assert broker._last_position_snapshot_fetch_failed is False


def test_gm_live_position_query_failure_is_not_silent_empty_position(monkeypatch):
    """同步持仓查询失败必须向执行器暴露为未知状态，不能伪装成空仓并放行 BUY。"""
    import live_trader.adapters.gm_broker as gm_module

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True

    def _raise_position_error():
        raise RuntimeError("trade service unavailable")

    monkeypatch.setattr(gm_module, "gm_get_position", _raise_position_error)

    with pytest.raises(RuntimeError, match="GM live position query failed"):
        broker.get_position(SimpleNamespace(_name="SHSE.513050"))

    assert broker._last_position_snapshot_fetch_failed is True
    assert "trade service unavailable" in str(broker._last_position_snapshot_fetch_error)


def test_gm_live_account_snapshot_probe_accepts_flat_account(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    cash_calls = []
    position_calls = []
    monkeypatch.setattr(
        gm_module,
        "get_cash",
        lambda *args, **kwargs: cash_calls.append((args, kwargs))
        or SimpleNamespace(available=123.45),
    )
    monkeypatch.setattr(
        gm_module,
        "gm_get_position",
        lambda *args, **kwargs: position_calls.append((args, kwargs)) or [],
    )

    assert broker.is_account_snapshot_trusted() is True
    assert broker._last_account_snapshot_fetch_failed is False
    assert cash_calls == [((), {})]
    assert position_calls == [((), {})]


def test_gm_live_account_snapshot_probe_rejects_missing_cash(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    monkeypatch.setattr(gm_module, "get_cash", lambda: {})
    monkeypatch.setattr(gm_module, "gm_get_position", lambda: [])

    assert broker.is_account_snapshot_trusted() is False
    assert broker._last_account_snapshot_fetch_failed is True
    assert "available field" in str(broker._last_account_snapshot_fetch_error)


@pytest.mark.parametrize("available", ["N/A", float("nan"), float("inf")])
def test_gm_live_account_snapshot_probe_rejects_invalid_cash(monkeypatch, available):
    import live_trader.adapters.gm_broker as gm_module

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    monkeypatch.setattr(
        gm_module,
        "get_cash",
        lambda: SimpleNamespace(available=available),
    )
    monkeypatch.setattr(gm_module, "gm_get_position", lambda: [])

    assert broker.is_account_snapshot_trusted() is False
    assert broker._last_account_snapshot_fetch_failed is True


def test_gm_backtest_position_keeps_context_snapshot_fast_path(monkeypatch):
    """同步柜台查询仅属于实盘；GM 回测继续使用 context 内存持仓。"""
    import live_trader.adapters.gm_broker as gm_module

    pos = SimpleNamespace(
        symbol="SHSE.600000",
        volume=1000,
        vwap=10.0,
        available_now=None,
        available=1000,
    )
    ctx = SimpleNamespace(account=lambda: SimpleNamespace(positions=lambda: [pos]))
    broker = GmBrokerAdapter(context=ctx)
    broker.is_live = False
    monkeypatch.setattr(
        gm_module,
        "gm_get_position",
        lambda: (_ for _ in ()).throw(AssertionError("backtest must not query live counter")),
    )

    got = broker.get_position(SimpleNamespace(_name="SHSE.600000"))

    assert got.size == 1000
    assert got.sellable == 1000


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


@pytest.mark.parametrize(
    "pending",
    [
        SimpleNamespace(
            cl_ord_id="",
            symbol="SHSE.600000",
            side=mock_gm_api.OrderSide_Buy,
            volume=1000,
            filled_volume=0,
        ),
        SimpleNamespace(
            cl_ord_id="GM_NO_SYMBOL",
            symbol="",
            side=mock_gm_api.OrderSide_Buy,
            volume=1000,
            filled_volume=0,
        ),
        SimpleNamespace(
            cl_ord_id="GM_UNKNOWN_SIDE",
            symbol="SHSE.600000",
            side=999,
            volume=1000,
            filled_volume=0,
        ),
        SimpleNamespace(
            cl_ord_id="GM_ZERO_REMAINING",
            symbol="SHSE.600000",
            side=mock_gm_api.OrderSide_Buy,
            volume=1000,
            filled_volume=1000,
        ),
        SimpleNamespace(
            cl_ord_id="GM_BAD_SIZE",
            symbol="SHSE.600000",
            side=mock_gm_api.OrderSide_Buy,
            volume="not-a-number",
            filled_volume=0,
        ),
    ],
)
def test_gm_pending_order_malformed_record_marks_snapshot_untrusted(monkeypatch, pending):
    """格式错误的在途订单必须使整份 GM 快照失效。"""
    import live_trader.adapters.gm_broker as gm_module

    monkeypatch.setattr(gm_module, "get_cash", lambda: SimpleNamespace(available=0.0, nav=0.0))
    monkeypatch.setattr(mock_gm_api, "get_unfinished_orders", lambda: [pending], raising=False)
    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True

    assert broker.get_pending_orders() == []
    assert broker._last_pending_orders_fetch_failed is True
    assert broker._last_pending_orders_fetch_error is not None


def test_gm_pending_order_partial_snapshot_is_never_returned_after_bad_record(monkeypatch):
    """坏记录出现在后半段时，也不能泄漏此前收集的部分在途订单。"""
    import live_trader.adapters.gm_broker as gm_module

    valid = SimpleNamespace(
        cl_ord_id="GM_VALID_FIRST",
        symbol="SHSE.600000",
        side=mock_gm_api.OrderSide_Buy,
        volume=1000,
        filled_volume=0,
    )
    malformed = SimpleNamespace(
        cl_ord_id="",
        symbol="SHSE.600001",
        side=mock_gm_api.OrderSide_Buy,
        volume=1000,
        filled_volume=0,
    )
    monkeypatch.setattr(mock_gm_api, "get_unfinished_orders", lambda: [valid, malformed], raising=False)
    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True

    assert broker.get_pending_orders() == []
    assert broker._last_pending_orders_fetch_failed is True


def test_gm_pending_query_keeps_account_id_for_cancel(monkeypatch):
    """GM 原生 pending 记录的 account_id 必须原样用于撤单。"""
    import live_trader.adapters.gm_broker as gm_module

    broker = GmBrokerAdapter(context=MagicMock())
    broker.is_live = True
    pending = {
        "cl_ord_id": "GM_ACCOUNT_OID",
        "account_id": "A001",
        "symbol": "SHSE.600000",
        "side": mock_gm_api.OrderSide_Buy,
        "volume": 1000,
        "filled_volume": 0,
    }
    monkeypatch.setattr(mock_gm_api, "get_unfinished_orders", lambda: [pending], raising=False)
    mock_gm.api = mock_gm_api

    cancel_calls = []
    monkeypatch.setattr(
        mock_gm_api,
        "order_cancel",
        lambda payload: cancel_calls.append(payload),
        raising=False,
    )
    monkeypatch.setattr(mock_gm_api, "cancel_order", None, raising=False)

    assert broker.cancel_pending_order("GM_ACCOUNT_OID") is True
    assert cancel_calls == [{"cl_ord_id": "GM_ACCOUNT_OID", "account_id": "A001"}]


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


def test_gm_launch_rejects_second_schedule_before_sdk_start(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module

    sdk_calls = []
    monkeypatch.setattr(gm_module, "set_token", lambda token: sdk_calls.append(("set_token", token)))
    monkeypatch.setattr(gm_module, "gmi_init", lambda: sdk_calls.append(("gmi_init", None)))

    with pytest.raises(ValueError, match="Second-level schedule.*not supported") as exc_info:
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id", "schedule": "5s:00:00:00"},
            strategy_path="sample_strategy",
            params={},
        )

    assert "timeframe='Seconds'" in str(exc_info.value)
    assert sdk_calls == []


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


def test_gm_daily_connectivity_recovery_reuses_alive_boundary_and_prewarm():
    import live_trader.adapters.gm_broker as gm_module

    parsed = SchedulePlanner.parse_schedule_rule("1d:14:45:00")
    quiet, delay, wake_at = gm_module._resolve_gm_connectivity_retry(
        datetime.datetime(2026, 7, 30, 10, 0, 0),
        parsed,
        prewarm_lead_seconds=0,
        active_after_seconds=600,
    )

    assert quiet is True
    assert delay == pytest.approx(600.0)
    assert wake_at == datetime.datetime(2026, 7, 30, 14, 15, 0)

    quiet, delay, wake_at = gm_module._resolve_gm_connectivity_retry(
        datetime.datetime(2026, 7, 30, 13, 0, 0),
        parsed,
        prewarm_lead_seconds=3600,
        active_after_seconds=600,
    )
    assert quiet is True
    assert delay == pytest.approx(600.0)
    assert wake_at == datetime.datetime(2026, 7, 30, 13, 45, 0)

    assert gm_module._resolve_gm_connectivity_retry(
        datetime.datetime(2026, 7, 30, 14, 15, 0),
        parsed,
        prewarm_lead_seconds=0,
        active_after_seconds=600,
    ) == (False, gm_module._GM_AGGRESSIVE_RETRY_SECONDS, None)


def test_gm_interval_connectivity_wait_never_crosses_next_slot():
    import live_trader.adapters.gm_broker as gm_module

    parsed = SchedulePlanner.parse_schedule_rule("1m:14:45:00")
    quiet, delay, wake_at = gm_module._resolve_gm_connectivity_retry(
        datetime.datetime(2026, 7, 30, 14, 44, 40),
        parsed,
        prewarm_lead_seconds=0,
        active_after_seconds=48,
    )

    assert quiet is True
    assert delay == pytest.approx(8.0)
    assert wake_at == datetime.datetime(2026, 7, 30, 14, 44, 48)
    assert gm_module._resolve_gm_connectivity_retry(
        datetime.datetime(2026, 7, 30, 14, 45, 49),
        parsed,
        prewarm_lead_seconds=0,
        active_after_seconds=48,
    ) == (False, gm_module._GM_AGGRESSIVE_RETRY_SECONDS, None)
    assert gm_module._resolve_gm_connectivity_retry(
        datetime.datetime(2026, 7, 30, 14, 45, 49),
        None,
    ) == (False, gm_module._GM_AGGRESSIVE_RETRY_SECONDS, None)


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

    monkeypatch.setattr(data_warm_module.runtime_notifications, "push_text", DummyAlarm().push_text)
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

    class StopPhoenix(BaseException):
        pass

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
            raise StopPhoenix()

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

    with pytest.raises(StopPhoenix):
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


def test_gm_launch_continues_on_transient_poll_minus_one(monkeypatch, capsys):
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    poll_count = {"value": 0}
    sleep_calls = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    def _poll_twice_then_stop():
        poll_count["value"] += 1
        if poll_count["value"] <= 2:
            return -1
        raise StopPhoenix()

    def _check_status(status):
        if status != 0:
            raise AssertionError("gmi_poll return values must follow the official ignored-status loop")

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
    monkeypatch.setattr(gm_module, "check_gm_status", _check_status, raising=False)
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_twice_then_stop, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert poll_count["value"] == 3
    assert captured.out.count("gmi_poll returned transient status -1") == 1
    assert "Restarting session" not in captured.out
    assert sleep_calls == [0.05, 0.05]


def test_gm_daily_schedule_quiet_window_suppresses_maintenance_logs_without_stopping_poll(monkeypatch, capsys):
    """日线恢复窗口外继续驱动 SDK，但不刷 -1/1200/1201/1100 维护日志。"""
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    poll_count = {"value": 0}
    health_states = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, *args, **kwargs):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            raise AssertionError("quiet maintenance window must not push schedule alarm")

        def push_exception(self, *args, **kwargs):
            raise AssertionError("known maintenance statuses must not push exception")

    def _poll_maintenance_then_stop():
        poll_count["value"] += 1
        if poll_count["value"] == 1:
            return -1
        if poll_count["value"] == 2:
            fake_context.on_error_fun(fake_context, 1201, "实时行情服务连接断开")
            fake_context.on_error_fun(fake_context, 1200, "实时行情服务连接失败")
            fake_context.on_error_fun(fake_context, 1100, "交易消息服务连接失败")
            return -1
        raise StopPhoenix()

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
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_maintenance_then_stop, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr(
        gm_module,
        "_resolve_gm_connectivity_retry",
        lambda *args, **kwargs: (
            True,
            600.0,
            datetime.datetime(2026, 8, 13, 14, 15, 0),
        ),
    )
    monkeypatch.setattr(
        gm_module,
        "report_live_worker_state",
        lambda state, **kwargs: health_states.append((state, kwargs)) or True,
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {
                "token": "token",
                "strategy_id": "strategy-id",
                "schedule": "1d:14:45:00",
            },
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert poll_count["value"] == 3, "日志降噪不能停止 gmi_poll 探测。"
    assert "gmi_poll returned transient status" not in captured.out
    assert "Code: 1200" not in captured.out
    assert "Code: 1201" not in captured.out
    assert "Code: 1100" not in captured.out
    assert any(state == "gm_connectivity_quiet_wait" for state, _ in health_states)


def test_gm_launch_converts_sdk_system_exit_to_restart(monkeypatch, capsys):
    """
    GM SDK 可能在底层 shutdown 时抛 SystemExit。launch 必须拦截并重启
    当前 session，避免策略进程被 SDK 直接带退出。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    health_states = []
    monkeypatch.setattr(
        gm_module,
        "report_live_worker_state",
        lambda state, **kwargs: health_states.append((state, kwargs)) or True,
    )

    class StopPhoenix(BaseException):
        pass

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
            raise StopPhoenix()

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

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert "GM SDK requested process exit" in captured.out
    assert "[Phoenix] Waiting 10s before restart" in captured.out
    assert health_states[0] == (
        "gm_sdk_initializing",
        {
            "unhealthy_after_seconds": gm_module._GM_SDK_HEALTH_DEADLINE_SECONDS,
            "detail": "gmi_init has not completed",
            "failure_kind": gm_module.LiveWorkerFailureKind.CONNECTIVITY,
        },
    )
    assert health_states[1] == (
        "gm_sdk_running",
        {"detail": "gmi_init completed"},
    )


def test_gm_backtest_does_not_publish_live_worker_health(monkeypatch):
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    health_states = []
    backtest_configs = []

    monkeypatch.setattr(gm_module, "MODE_LIVE", "live", raising=False)
    monkeypatch.setattr(gm_module, "MODE_BACKTEST", "backtest", raising=False)
    monkeypatch.setattr(gm_module, "context", fake_context, raising=False)
    monkeypatch.setattr(gm_module, "set_token", lambda token: None, raising=False)
    monkeypatch.setattr(gm_module, "set_serv_addr", lambda addr: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_strategy_id", lambda strategy_id: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_set_mode", lambda mode: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_data_callback", lambda callback: None, raising=False)
    monkeypatch.setattr(gm_module, "callback_controller", object(), raising=False)
    monkeypatch.setattr(
        gm_module,
        "py_gmi_set_backtest_config",
        lambda **kwargs: backtest_configs.append(kwargs),
        raising=False,
    )
    monkeypatch.setattr(gm_module, "py_gmi_run", lambda: 0, raising=False)
    monkeypatch.setattr(gm_module, "check_gm_status", lambda status: None, raising=False)
    monkeypatch.setattr(
        gm_module,
        "report_live_worker_state",
        lambda state, **kwargs: health_states.append((state, kwargs)) or True,
    )

    GmBrokerAdapter.launch(
        {"token": "token", "strategy_id": "strategy-id"},
        strategy_path="sample_strategy",
        params={},
        start_date="20260101",
        end_date="20260105",
    )

    assert len(backtest_configs) == 1
    assert health_states == []


def test_gm_launch_restarts_unmarked_sdk_system_exit(monkeypatch, capsys):
    """
    GM SDK/底层事件循环可能直接抛 SystemExit 且没有先触发 on_shutdown。
    实盘 Phoenix 仍应把它视作 SDK session 退出并重启，避免 nohup 进程直接消失。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    pushed = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            pushed.append((args, kwargs))

    def _poll_raises_system_exit():
        raise SystemExit(0)

    def _sleep(seconds):
        if seconds >= 10:
            raise StopPhoenix()

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

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert "unmarked SDK SystemExit" in captured.out
    assert "[Phoenix] Waiting 10s before restart" in captured.out
    assert pushed, "未标记 SystemExit 应推送异常，便于定位 SDK 层直接退出。"


def test_gm_launch_soft_resets_sdk_after_init_failure(monkeypatch, capsys):
    """
    GM 终端未启动时 gmi_init 可能把失败状态留在 SDK 进程内。
    init 失败后必须重新绑定 token/server 并尝试 SDK soft reset，避免同进程永久 1001。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    token_calls = []
    serv_addr_calls = []
    reset_calls = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    def _sleep(seconds):
        if seconds >= 10:
            raise StopPhoenix()

    monkeypatch.setattr(gm_module, "MODE_LIVE", "live", raising=False)
    monkeypatch.setattr(gm_module, "MODE_BACKTEST", "backtest", raising=False)
    monkeypatch.setattr(gm_module, "context", fake_context, raising=False)
    monkeypatch.setattr(gm_module, "set_token", lambda token: token_calls.append(token), raising=False)
    monkeypatch.setattr(gm_module, "set_serv_addr", lambda addr: serv_addr_calls.append(addr), raising=False)
    monkeypatch.setattr(gm_module, "gmi_close", lambda: reset_calls.append("gmi_close"), raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_strategy_id", lambda strategy_id: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_set_mode", lambda mode: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_data_callback", lambda callback: None, raising=False)
    monkeypatch.setattr(gm_module, "callback_controller", object(), raising=False)
    monkeypatch.setattr(gm_module, "gmi_init", lambda: 1001, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr("time.sleep", _sleep)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "serv_addr": "127.0.0.1:7001", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert len(token_calls) >= 2, "外层启动和每轮 run_session 前都应重新绑定 token。"
    assert len(serv_addr_calls) >= 2, "外层启动和每轮 run_session 前都应重新绑定 serv_addr。"
    assert reset_calls == ["gmi_close"]
    assert "GM SDK soft reset after init failure. status=1001" in captured.out


def test_gm_supervised_init_failure_quietly_probes_until_recovery_window(monkeypatch, capsys):
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    health_states = []
    restart_requests = []
    sleep_calls = []
    wake_at = datetime.datetime.now() + datetime.timedelta(seconds=900)

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    def _sleep(seconds):
        sleep_calls.append(seconds)
        raise StopPhoenix()

    monkeypatch.setattr(gm_module, "MODE_LIVE", "live", raising=False)
    monkeypatch.setattr(gm_module, "MODE_BACKTEST", "backtest", raising=False)
    monkeypatch.setattr(gm_module, "context", fake_context, raising=False)
    monkeypatch.setattr(gm_module, "set_token", lambda token: None, raising=False)
    monkeypatch.setattr(gm_module, "set_serv_addr", lambda addr: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_close", lambda: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_strategy_id", lambda strategy_id: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_set_mode", lambda mode: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_data_callback", lambda callback: None, raising=False)
    monkeypatch.setattr(gm_module, "callback_controller", object(), raising=False)
    monkeypatch.setattr(gm_module, "gmi_init", lambda: 1001, raising=False)
    monkeypatch.setattr(gm_module, "is_live_worker_process", lambda: True)
    monkeypatch.setattr(
        gm_module,
        "request_live_worker_restart",
        lambda *args, **kwargs: restart_requests.append((args, kwargs)),
    )
    monkeypatch.setattr(
        gm_module,
        "report_live_worker_state",
        lambda state, **kwargs: health_states.append((state, kwargs)) or True,
    )
    monkeypatch.setattr(
        gm_module,
        "_resolve_gm_connectivity_retry",
        lambda *args, **kwargs: (
            True,
            123.0,
            wake_at,
        ),
    )
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr("time.sleep", _sleep)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {
                "token": "token",
                "strategy_id": "strategy-id",
                "schedule": "1d:14:45:00",
            },
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert sleep_calls == [123.0]
    assert restart_requests == []
    assert health_states[-1][0] == "gm_connectivity_quiet_wait"
    assert 895.0 <= health_states[-1][1].get("unhealthy_after_seconds", 0.0) <= 900.0
    assert health_states[-1][1].get("refresh_deadline") is True
    assert "quiet probe in 123s; aggressive recovery at" not in captured.out


def test_gm_launch_reexecs_after_repeated_init_failures(monkeypatch):
    """
    若 GM SDK 在同一进程中连续 init 失败，Phoenix 应执行进程级自重启，
    覆盖“人工 kill 后重跑即可成功”的恢复路径。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    pushed = []
    exec_calls = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            pushed.append((args, kwargs))

    def _execv(executable, argv):
        exec_calls.append((executable, argv))
        raise StopPhoenix()

    monkeypatch.setattr(gm_module, "MODE_LIVE", "live", raising=False)
    monkeypatch.setattr(gm_module, "MODE_BACKTEST", "backtest", raising=False)
    monkeypatch.setattr(gm_module, "context", fake_context, raising=False)
    monkeypatch.setattr(gm_module, "set_token", lambda token: None, raising=False)
    monkeypatch.setattr(gm_module, "set_serv_addr", lambda addr: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_close", lambda: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_strategy_id", lambda strategy_id: None, raising=False)
    monkeypatch.setattr(gm_module, "gmi_set_mode", lambda mode: None, raising=False)
    monkeypatch.setattr(gm_module, "py_gmi_set_data_callback", lambda callback: None, raising=False)
    monkeypatch.setattr(gm_module, "callback_controller", object(), raising=False)
    monkeypatch.setattr(gm_module, "gmi_init", lambda: 1001, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setattr(gm_module.os, "execv", _execv)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )

    assert exec_calls, "连续 init 失败后应触发 os.execv 自重启。"
    executable, argv = exec_calls[0]
    assert executable == sys.executable
    assert argv[0] == sys.executable
    assert argv[1] == "-u"
    assert pushed == [], "GM SDK init 连接维护失败应仅记录并自愈，不推异常 IM。"


def test_gm_duplicate_init_callback_is_ignored_in_same_session(monkeypatch, capsys):
    """
    GM SDK 可能在同一实盘 session 内重复触发 init 回调；第二次不能重复初始化
    LiveTrader，否则会再次推送 STARTED 生命周期消息。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    init_calls = []
    trader_instances = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    class DummyTrader:
        def __init__(self, engine_config):
            self.config = engine_config
            self.broker = SimpleNamespace(datas=[SimpleNamespace(_name="SHSE.600000")])
            trader_instances.append(self)

        def init(self, ctx):
            init_calls.append(ctx)

        def run(self, ctx):
            return None

    def _poll_duplicate_init_then_stop():
        fake_context.init_fun(fake_context)
        fake_context.init_fun(fake_context)
        raise StopPhoenix()

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
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_duplicate_init_then_stop, raising=False)
    monkeypatch.setattr(gm_module, "subscribe", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr(gm_module, "LiveTrader", DummyTrader, raising=False)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert len(trader_instances) == 1
    assert len(init_calls) == 1
    assert fake_context.strategy_instance is trader_instances[0]
    assert "Duplicate init callback ignored for current GM session" in captured.out


def test_gm_start_alarm_is_sent_once_across_phoenix_restarts(monkeypatch, capsys):
    """
    GM 夜间重连可能反复创建新的 run_session；同一 Python 进程内只应首次
    LiveTrader 初始化推 STARTED，后续 Phoenix 重启不再重复推启动消息。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    init_configs = []
    poll_count = {"value": 0}

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    class DummyTrader:
        def __init__(self, engine_config):
            self.config = engine_config
            self.broker = SimpleNamespace(datas=[SimpleNamespace(_name="SHSE.600000")])
            init_configs.append(engine_config.copy())

        def init(self, ctx):
            return None

        def run(self, ctx):
            return None

    def _poll_restart_then_stop():
        poll_count["value"] += 1
        fake_context.init_fun(fake_context)
        if poll_count["value"] == 1:
            fake_context.on_shutdown_fun(fake_context)
            return 0
        raise StopPhoenix()

    def _sleep(seconds):
        if seconds >= 10:
            return None

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
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_restart_then_stop, raising=False)
    monkeypatch.setattr(gm_module, "subscribe", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr(gm_module, "LiveTrader", DummyTrader, raising=False)
    monkeypatch.setattr("time.sleep", _sleep)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert len(init_configs) == 2
    assert init_configs[0].get('_suppress_start_alarm') is False
    assert init_configs[1].get('_suppress_start_alarm') is True
    assert "GM shutdown callback received. Restarting session" in captured.out


def test_gm_schedule_callback_runs_once_per_schedule_slot(monkeypatch, capsys):
    """
    GM schedule 在同一日线 slot 内可能重复回调；同一 slot 只能执行一次策略，
    防止调仓计划和通知按秒刷屏。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    run_times = []
    scheduled_callbacks = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    class DummyTrader:
        def __init__(self, engine_config):
            self.config = engine_config
            self.broker = SimpleNamespace(datas=[SimpleNamespace(_name="SHSE.600000")])

        def init(self, ctx):
            return None

        def run(self, ctx):
            run_times.append(ctx.now)

    def _schedule(schedule_func, date_rule, time_rule):
        scheduled_callbacks.append(schedule_func)

    def _poll_repeated_schedule_callbacks_then_stop():
        fake_context.init_fun(fake_context)
        schedule_run = scheduled_callbacks[-1]
        for second in (4, 5, 6):
            fake_context.now = datetime.datetime(2026, 6, 26, 14, 49, second)
            schedule_run(fake_context)
        raise StopPhoenix()

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
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_repeated_schedule_callbacks_then_stop, raising=False)
    monkeypatch.setattr(gm_module, "subscribe", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr(gm_module, "LiveTrader", DummyTrader, raising=False)

    fake_gm_api = SimpleNamespace(schedule=_schedule)
    monkeypatch.setitem(sys.modules, "gm.api", fake_gm_api)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id", "schedule": "1d:14:45:00"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert run_times == [datetime.datetime(2026, 6, 26, 14, 49, 4)]
    assert captured.out.count("Duplicate schedule callback ignored for slot 2026-06-26 14:45:00") == 1


def test_gm_live_schedule_run_is_offloaded_from_sdk_callback_thread(monkeypatch):
    """SELL 等待期间 GM 仍必须能及时处理拒单/成交回调。"""
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    scheduled_callbacks = []
    run_started = threading.Event()
    release_run = threading.Event()
    run_finished = threading.Event()
    callback_returned = threading.Event()
    run_times = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    class DummyBroker:
        is_live = True
        datas = [SimpleNamespace(_name="SHSE.600000")]

    class DummyTrader:
        def __init__(self, engine_config):
            self.config = engine_config
            self.broker = DummyBroker()

        def init(self, ctx):
            return None

        def run(self, ctx):
            run_started.set()
            try:
                run_times.append(ctx.now)
                # 同步执行 schedule 回调会阻塞这里，使模拟 GM 轮询无法返回
                # 事件分发器。
                release_run.wait(0.5)
            finally:
                run_finished.set()

    def _schedule(schedule_func, date_rule, time_rule):
        scheduled_callbacks.append(schedule_func)

    def _poll_once_then_stop():
        fake_context.init_fun(fake_context)
        fake_context.now = datetime.datetime(2026, 6, 26, 14, 49, 4)
        started_at = time.monotonic()
        scheduled_callbacks[-1](fake_context)
        callback_returned.set()
        # SDK 可能在回调返回后立即修改共享 context；工作线程必须保留
        # 原始 slot 时间戳。
        fake_context.now = datetime.datetime(2026, 6, 26, 14, 50, 0)
        assert time.monotonic() - started_at < 0.25, (
            "实盘 schedule 回调不能同步阻塞在 LiveTrader.run 的 SELL 等待中。"
        )
        assert run_started.wait(0.5), "后台调仓 worker 未启动。"
        raise StopPhoenix()

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
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_once_then_stop, raising=False)
    monkeypatch.setattr(gm_module, "subscribe", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr(gm_module, "LiveTrader", DummyTrader, raising=False)

    fake_gm_api = SimpleNamespace(schedule=_schedule)
    monkeypatch.setitem(sys.modules, "gm.api", fake_gm_api)

    try:
        with pytest.raises(StopPhoenix):
            GmBrokerAdapter.launch(
                {"token": "token", "strategy_id": "strategy-id", "schedule": "1d:14:45:00"},
                strategy_path="sample_strategy",
                params={},
            )
    finally:
        release_run.set()
        assert callback_returned.is_set()
        assert run_finished.wait(1.0), "后台调仓 worker 未正常收尾。"
        assert run_times == [datetime.datetime(2026, 6, 26, 14, 49, 4)]


def test_gm_prewarm_callback_runs_once_per_schedule_slot(monkeypatch, capsys):
    """
    GM prewarm schedule 也可能在同一 slot 重复回调；同一目标运行 slot
    只能执行一次预热，避免夜间持续刷 Prewarm Finished。
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    prewarm_times = []
    scheduled_callbacks = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_schedule_api_unavailable(self, *args, **kwargs):
            return []

        def push_exception(self, *args, **kwargs):
            return None

    class DummyBroker:
        datas = [SimpleNamespace(_name="SHSE.600000")]

        def run_schedule_prewarm(self, **kwargs):
            prewarm_times.append(fake_context.now)
            return {
                "source": "broker",
                "symbol": "SHSE.600000",
                "extras": [],
                "errors": [],
            }

    class DummyTrader:
        def __init__(self, engine_config):
            self.config = engine_config
            self.broker = DummyBroker()
            self.data_provider = object()

        def init(self, ctx):
            return None

        def run(self, ctx):
            return None

    def _schedule(schedule_func, date_rule, time_rule):
        scheduled_callbacks.append((time_rule, schedule_func))

    def _poll_repeated_prewarm_callbacks_then_stop():
        fake_context.init_fun(fake_context)
        prewarm_run = scheduled_callbacks[0][1]
        for second in (0, 1, 2):
            fake_context.now = datetime.datetime(2026, 6, 26, 14, 44, second)
            prewarm_run(fake_context)
        raise StopPhoenix()

    monkeypatch.setattr(gm_module.config, "LIVE_SCHEDULE_PREWARM_LEAD", "60s", raising=False)
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
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_repeated_prewarm_callbacks_then_stop, raising=False)
    monkeypatch.setattr(gm_module, "subscribe", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr(gm_module, "LiveTrader", DummyTrader, raising=False)

    fake_gm_api = SimpleNamespace(schedule=_schedule)
    monkeypatch.setitem(sys.modules, "gm.api", fake_gm_api)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id", "schedule": "1d:14:45:00"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert prewarm_times == [datetime.datetime(2026, 6, 26, 14, 44, 0)]
    assert captured.out.count("Prewarm Finished") == 1
    assert captured.out.count("Duplicate/early prewarm callback ignored for slot 2026-06-26 14:45:00") == 1


def test_gm_temporary_connection_errors_do_not_push_exception(monkeypatch, capsys):
    """
    GM 夜间非交易时段的行情/交易服务连接失败应降噪:
    - 允许打印有限 warning
    - 不推 GM Kernel Error 异常
    - 重复错误不刷屏、不触发 Phoenix 重启
    """
    import live_trader.adapters.gm_broker as gm_module

    fake_context = SimpleNamespace()
    poll_count = {"value": 0}
    health_states = []
    schedule_unavailable = []

    class StopPhoenix(BaseException):
        pass

    class DummyAlarm:
        def push_status(self, status, detail=''):
            return None

        def push_text(self, content, level='INFO'):
            raise AssertionError("temporary GM connection error should not push text alarm")

        def push_schedule_api_unavailable(self, *args, **kwargs):
            schedule_unavailable.append((args, kwargs))
            return []

        def push_exception(self, *args, **kwargs):
            raise AssertionError("temporary GM connection error should not push exception")

    def _poll_market_data_errors_then_stop():
        poll_count["value"] += 1
        if poll_count["value"] == 1:
            return 1200
        fake_context.on_error_fun(fake_context, 1200, "实时行情服务连接失败")
        fake_context.on_error_fun(fake_context, 1200, "实时行情服务连接失败")
        fake_context.on_error_fun(fake_context, 1100, "交易消息服务连接失败")
        fake_context.on_error_fun(fake_context, 1100, "交易消息服务连接失败")
        fake_context.on_bar_fun(fake_context, [])
        raise StopPhoenix()

    def _sleep(seconds):
        return None

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
    monkeypatch.setattr(gm_module, "gmi_poll", _poll_market_data_errors_then_stop, raising=False)
    monkeypatch.setattr(
        gm_module,
        "report_live_worker_state",
        lambda state, **kwargs: health_states.append((state, kwargs)) or True,
    )
    monkeypatch.setattr(gm_module, "AlarmManager", lambda: DummyAlarm(), raising=False)
    monkeypatch.setattr("time.sleep", _sleep)

    with pytest.raises(StopPhoenix):
        GmBrokerAdapter.launch(
            {"token": "token", "strategy_id": "strategy-id"},
            strategy_path="sample_strategy",
            params={},
        )

    captured = capsys.readouterr()
    assert captured.out.count("[GM Warning] Code: 1200, Msg: 实时行情服务连接失败") == 1
    assert captured.out.count("[GM Warning] Code: 1100, Msg: 交易消息服务连接失败") == 1
    assert [state for state, _ in health_states].count("gm_trade_service_unavailable") == 2
    assert health_states[-1][0] == "gm_trade_service_unavailable"
    assert all(
        kwargs.get("failure_kind") is gm_module.LiveWorkerFailureKind.CONNECTIVITY
        for state, kwargs in health_states
        if state == "gm_trade_service_unavailable"
    )
    assert len(schedule_unavailable) == 1
    assert "Waiting 10s before restart" not in captured.out
