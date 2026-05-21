import sys
import types
import datetime

import pandas as pd
import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace


# 拦截 ib_insync 导入，避免任何真实 TWS/Gateway 连接依赖
mock_ib_insync = types.ModuleType("ib_insync")
mock_ib_insync.IB = MagicMock(name="IB")
mock_ib_insync.Stock = MagicMock(name="Stock")
mock_ib_insync.MarketOrder = MagicMock(name="MarketOrder")
mock_ib_insync.Trade = MagicMock(name="Trade")
mock_ib_insync.Forex = MagicMock(name="Forex")
mock_ib_insync.Contract = MagicMock(name="Contract")
mock_ib_insync.Crypto = MagicMock(name="Crypto")
sys.modules["ib_insync"] = mock_ib_insync

# 重新导入被测模块，确保使用上面的 mock ib_insync
sys.modules.pop("live_trader.adapters.ib_broker", None)
from live_trader.adapters.ib_broker import IBBrokerAdapter, IBOrderProxy
from live_trader.data_bridge.data_warm import SchedulePlanner
from data_providers.ibkr_provider import IbkrDataProvider


class DummyIBOrderStatus:
    def __init__(self, status, filled=0, avg_fill_price=0.0):
        self.status = status
        self.filled = filled
        self.avgFillPrice = avg_fill_price


class DummyIBOrder:
    def __init__(self, order_id=1, action="BUY", total_quantity=0):
        self.orderId = order_id
        self.action = action
        self.totalQuantity = total_quantity


class DummyCommissionReport:
    def __init__(self, commission=0.0):
        self.commission = commission


class DummyFill:
    def __init__(self, commission=None, fill_time=None, execution_time=None):
        self.commissionReport = None if commission is None else DummyCommissionReport(commission=commission)
        self.time = fill_time
        self.execution = SimpleNamespace(time=execution_time) if execution_time is not None else None


class DummyIBTrade:
    def __init__(
        self,
        status,
        action="BUY",
        order_id=1,
        total_quantity=0,
        filled=0,
        avg_fill_price=0.0,
        commissions=None,
        fill_times=None,
        execution_times=None,
    ):
        self.order = DummyIBOrder(order_id=order_id, action=action, total_quantity=total_quantity)
        self.orderStatus = DummyIBOrderStatus(status=status, filled=filled, avg_fill_price=avg_fill_price)
        fill_times = list(fill_times or [])
        execution_times = list(execution_times or [])
        commissions = list(commissions or [])
        fill_count = max(len(commissions), len(fill_times), len(execution_times))
        self.fills = []
        for idx in range(fill_count):
            commission = commissions[idx] if idx < len(commissions) else None
            fill_time = fill_times[idx] if idx < len(fill_times) else None
            execution_time = execution_times[idx] if idx < len(execution_times) else None
            self.fills.append(DummyFill(commission=commission, fill_time=fill_time, execution_time=execution_time))


class DummyAccountValue:
    def __init__(self, tag, currency, value, account=""):
        self.tag = tag
        self.currency = currency
        self.value = value
        self.account = account


class DummyIBForCash:
    def __init__(self, cash_usd):
        self.cash_usd = cash_usd

    def isConnected(self):
        return True

    def accountSummary(self):
        return [DummyAccountValue(tag="TotalCashValue", currency="USD", value=str(self.cash_usd))]

    def accountValues(self):
        return self.accountSummary()

    def openTrades(self):
        return []

    def qualifyContracts(self, contract):
        return [contract]

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False):
        return DummyTicker(float("nan"))

    def reqHistoricalData(self, *args, **kwargs):
        return []


class DummyIBForCashWithOpenTrades(DummyIBForCash):
    def __init__(self, cash_usd, open_trades):
        super().__init__(cash_usd=cash_usd)
        self._open_trades = open_trades

    def openTrades(self):
        return self._open_trades


class DummyIBForAllOpenOrders(DummyIBForCashWithOpenTrades):
    def __init__(
        self,
        cash_usd,
        open_trades=None,
        all_open_trades=None,
        on_req_open_orders=None,
        client_id=999,
        cancel_effective=True,
    ):
        super().__init__(cash_usd=cash_usd, open_trades=open_trades or [])
        self._all_open_trades = all_open_trades or []
        self.req_all_calls = 0
        self.req_open_calls = 0
        self.cancel_calls = []
        self._on_req_open_orders = on_req_open_orders
        self.client = SimpleNamespace(clientId=client_id)
        self._cancel_effective = cancel_effective

    def reqAllOpenOrders(self):
        self.req_all_calls += 1
        return self._all_open_trades

    def reqOpenOrders(self):
        self.req_open_calls += 1
        if callable(self._on_req_open_orders):
            self._on_req_open_orders()
        return self._all_open_trades

    def reqAutoOpenOrders(self, auto_bind):
        return auto_bind

    def trades(self):
        return self._all_open_trades

    def cancelOrder(self, order):
        self.cancel_calls.append(order)
        if not self._cancel_effective:
            return
        for t in list(self._open_trades) + list(self._all_open_trades):
            if getattr(t, "order", None) is order:
                status = getattr(t, "orderStatus", None)
                if status is not None:
                    setattr(status, "status", "Cancelled")
                    setattr(status, "remaining", 0)


class DummyIBForPositions(DummyIBForCash):
    def __init__(self, cash_usd, positions):
        super().__init__(cash_usd=cash_usd)
        self._positions = positions

    def positions(self):
        return self._positions


class DummyTicker:
    def __init__(self, price):
        self._price = price
        self.close = None
        self.last = None
        self.bid = None
        self.ask = None

    def marketPrice(self):
        return self._price


class DummyIBForMissingFx(DummyIBForCash):
    def __init__(self):
        super().__init__(cash_usd=0.0)

    def accountSummary(self):
        return [
            DummyAccountValue(tag="TotalCashValue", currency="USD", value="1000"),
            DummyAccountValue(tag="TotalCashValue", currency="HKD", value="7800"),
            DummyAccountValue(tag="TotalCashValue", currency="BASE", value="2000"),
        ]


class DummyIBForAvailableFundsDual(DummyIBForCash):
    def __init__(self):
        super().__init__(cash_usd=0.0)

    def accountSummary(self):
        return [
            DummyAccountValue(tag="AvailableFunds", currency="USD", value="9200"),
            DummyAccountValue(tag="AvailableFunds", currency="HKD", value="780"),
            DummyAccountValue(tag="AvailableFunds", currency="BASE", value="9000"),
            DummyAccountValue(tag="TotalCashValue", currency="USD", value="10000"),
            DummyAccountValue(tag="TotalCashValue", currency="BASE", value="10000"),
        ]

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False):
        return DummyTicker(7.8)


class DummyIBForDelayedQuote(DummyIBForCash):
    def __init__(self):
        super().__init__(cash_usd=0.0)
        self.market_data_types = []
        self.req_mkt_calls = 0

    def reqMarketDataType(self, market_data_type: int):
        self.market_data_types.append(market_data_type)

    def sleep(self, seconds):
        return None

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False):
        self.req_mkt_calls += 1
        ticker = DummyTicker(float("nan"))
        if self.market_data_types and self.market_data_types[-1] == 3:
            ticker.close = 98.76
        return ticker


class DummyIBForAccountScopedFunds(DummyIBForCash):
    def __init__(self):
        super().__init__(cash_usd=0.0)

    def accountSummary(self):
        return [
            DummyAccountValue(tag="AvailableFunds", currency="BASE", value="9000", account="U2222222"),
            DummyAccountValue(tag="AvailableFunds", currency="USD", value="9000", account="U2222222"),
            DummyAccountValue(tag="AvailableFunds", currency="BASE", value="2000", account="U1111111"),
            DummyAccountValue(tag="AvailableFunds", currency="USD", value="2000", account="U1111111"),
            DummyAccountValue(tag="TotalCashValue", currency="BASE", value="9000", account="U2222222"),
            DummyAccountValue(tag="TotalCashValue", currency="BASE", value="2000", account="U1111111"),
        ]

    def accountValues(self):
        return self.accountSummary()


class DummyIBForZeroFunds(DummyIBForCash):
    def __init__(self):
        super().__init__(cash_usd=0.0)

    def accountSummary(self):
        return [
            DummyAccountValue(tag="AvailableFunds", currency="BASE", value="0"),
            DummyAccountValue(tag="AvailableFunds", currency="USD", value="0"),
            DummyAccountValue(tag="TotalCashValue", currency="BASE", value="0"),
        ]


class DummyIBDisconnected(DummyIBForCash):
    def __init__(self):
        super().__init__(cash_usd=0.0)

    def isConnected(self):
        return False

    def accountSummary(self, *args, **kwargs):
        return []

    def accountValues(self, *args, **kwargs):
        return []


class DummyIBForManagedAccountNoSnapshot(DummyIBForCash):
    def __init__(self, managed_accounts):
        super().__init__(cash_usd=0.0)
        self._managed_accounts = managed_accounts

    def managedAccounts(self):
        return list(self._managed_accounts)

    def accountSummary(self, *args, **kwargs):
        return []

    def accountValues(self, *args, **kwargs):
        return []


class DummyIBForAggregateAllSummary(DummyIBForCash):
    def __init__(self, managed_accounts):
        super().__init__(cash_usd=0.0)
        self._managed_accounts = managed_accounts

    def managedAccounts(self):
        return list(self._managed_accounts)

    def accountSummary(self, *args, **kwargs):
        return [
            DummyAccountValue(tag="AvailableFunds", currency="BASE", value="7777", account="All"),
            DummyAccountValue(tag="AvailableFunds", currency="USD", value="7777", account="All"),
            DummyAccountValue(tag="TotalCashValue", currency="BASE", value="7777", account="All"),
        ]

    def accountValues(self, *args, **kwargs):
        return self.accountSummary()


class DummyIBForSubmit(DummyIBForCash):
    def __init__(self, managed_accounts=None):
        super().__init__(cash_usd=10000.0)
        self.last_contract = None
        self.last_order = None
        self._oid = 900
        self._managed_accounts = managed_accounts or ["U1234567"]

    def managedAccounts(self):
        return list(self._managed_accounts)

    def placeOrder(self, contract, order):
        self._oid += 1
        self.last_contract = contract
        self.last_order = order
        return DummyIBTrade(
            status="Submitted",
            action=getattr(order, "action", "BUY"),
            order_id=self._oid,
            total_quantity=getattr(order, "totalQuantity", 0),
        )


def test_ib_status_translation_accuracy():
    """
    Red Team Test:
    覆盖 IB 关键状态映射，确保不会把在途误判为完成，或把终态误判为 pending。
    """
    pre_submitted = IBOrderProxy(DummyIBTrade(status="PreSubmitted"), data=None)
    submitted = IBOrderProxy(DummyIBTrade(status="Submitted"), data=None)
    api_pending = IBOrderProxy(DummyIBTrade(status="ApiPending"), data=None)
    filled = IBOrderProxy(DummyIBTrade(status="Filled"), data=None)
    cancelled = IBOrderProxy(DummyIBTrade(status="Cancelled"), data=None)
    api_cancelled = IBOrderProxy(DummyIBTrade(status="ApiCancelled"), data=None)
    inactive = IBOrderProxy(DummyIBTrade(status="Inactive"), data=None)

    assert pre_submitted.is_pending(), "状态映射错误：PreSubmitted 必须是 pending，避免漏管在途单！"
    assert not pre_submitted.is_completed(), "状态映射错误：PreSubmitted 不能被视为 completed！"
    assert submitted.is_pending(), "状态映射错误：Submitted 必须是 pending，订单仍在排队！"
    assert not submitted.is_completed(), "状态映射错误：Submitted 不能被视为 completed！"
    assert api_pending.is_pending(), "状态映射错误：ApiPending 必须是 pending，订单仍在 API 侧等待提交！"
    assert not api_pending.is_completed(), "状态映射错误：ApiPending 不能被视为 completed！"
    assert api_pending.is_accepted(), "状态映射错误：ApiPending 必须被视为活跃订单，避免风控重复发单！"

    assert filled.is_completed(), "状态映射错误：Filled 必须映射为 completed！"
    assert not filled.is_pending(), "状态映射错误：Filled 不应继续处于 pending！"
    assert not filled.is_accepted(), "状态映射错误：Filled 已是终态，不能继续被视为 accepted！"

    assert cancelled.is_canceled(), "状态映射错误：Cancelled 必须映射为 canceled！"
    assert not cancelled.is_pending(), "状态映射错误：Cancelled 不应继续 pending，避免死锁！"
    assert api_cancelled.is_canceled(), "状态映射错误：ApiCancelled 必须映射为 canceled！"
    assert not api_cancelled.is_pending(), "状态映射错误：ApiCancelled 不应继续 pending，避免死锁！"

    # Inactive 在当前实现中归类为 rejected，不允许停留在 pending
    assert inactive.is_rejected(), "状态映射错误：Inactive 必须被安全映射为 rejected/canceled 终态！"
    assert not inactive.is_pending(), "致命错误：Inactive 不能被视为 pending，否则可能导致状态机卡死！"


def test_ib_partial_fill_handling():
    """
    Red Team Test:
    海外部成防线：状态仍 Submitted 时，不可提前向引擎报告 completed。
    """
    trade = DummyIBTrade(
        status="Submitted",
        total_quantity=1000,
        filled=200,
        avg_fill_price=151.8,
        commissions=[1.23],
    )
    proxy = IBOrderProxy(trade, data=None)
    executed = proxy.executed

    assert executed.size == 200, "部成提取错误：executed.size 必须精准等于已成交 200！"
    assert not proxy.is_completed(), "核心防御失效：Submitted(部成) 绝不能被视为 completed！"
    assert proxy.is_pending(), "状态映射错误：Submitted(部成) 应保持 pending 直至 Filled/Cancelled！"


def test_ib_executed_stats_extraction():
    """
    Red Team Test:
    成交均价/金额/手续费提取校验，确保适配层统计结果可直接用于风控与记账。
    """
    trade = DummyIBTrade(
        status="Filled",
        action="BUY",
        total_quantity=500,
        filled=500,
        avg_fill_price=150.25,
        commissions=[1.2, 0.8],
    )
    proxy = IBOrderProxy(trade, data=None)
    executed = proxy.executed

    assert executed.price == pytest.approx(150.25), "成交均价提取错误：executed.price 应为 150.25！"
    assert executed.value == pytest.approx(75125.0), "成交金额计算错误：executed.value 应为 500*150.25=75125.0！"
    assert executed.comm == pytest.approx(2.0), "手续费提取错误：executed.comm 应等于 commissionReport 合计 2.0！"


def test_ib_executed_stats_exposes_last_fill_time():
    """
    成交时间回归:
    IBOrderProxy.executed.dt 应优先使用最后一笔 fill 的实际时间。
    """
    trade = DummyIBTrade(
        status="Filled",
        action="BUY",
        total_quantity=500,
        filled=500,
        avg_fill_price=150.25,
        commissions=[1.2, 0.8],
        fill_times=[
            datetime.datetime(2026, 4, 8, 21, 45, 30),
            datetime.datetime(2026, 4, 8, 21, 45, 32),
        ],
    )
    proxy = IBOrderProxy(trade, data=None)

    assert proxy.executed.dt.isoformat() == "2026-04-08T21:45:32", "成交时间应取最后一笔 fill 的实际时间。"


def test_ib_get_cash_respects_virtual_ledger():
    """
    资金口径回归:
    get_cash 必须扣减 _virtual_spent_cash，防止 openTrades 回报延迟窗口内重复下单。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=10000.0))
    broker = IBBrokerAdapter(context=context)
    broker._virtual_spent_cash = 2500.0

    assert broker.get_cash() == pytest.approx(7500.0), (
        "IB get_cash 未扣减虚拟账本，占资保护失效。"
    )


def test_ib_safety_multiplier_floor_is_1_05():
    """
    安全垫回归:
    IB 适配器买入估算安全垫应至少为 1.05，降低 Error 201 概率。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=10000.0))
    broker = IBBrokerAdapter(context=context)

    assert broker.safety_multiplier == pytest.approx(1.05), (
        "IB safety_multiplier 下限应为 1.05。"
    )


def test_ib_fetch_real_cash_uses_conservative_available_funds_dual_view():
    """
    资金口径回归:
    _fetch_real_cash 应使用 AvailableFunds 的 BASE/FX 双口径并取更保守值。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForAvailableFundsDual())
    broker = IBBrokerAdapter(context=context)

    # FX 聚合: 9200 + 780/7.8 = 9300；BASE=9000 => 应取 9000
    assert broker._fetch_real_cash() == pytest.approx(9000.0), (
        "_fetch_real_cash 应取 AvailableFunds 双口径中的更保守值。"
    )


def test_ib_fetch_real_cash_filters_by_configured_order_account(monkeypatch):
    """
    账户隔离回归:
    配置了 IBKR_ORDER_ACCOUNT 时，现金口径应只读取该账户。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForAccountScopedFunds())
    broker = IBBrokerAdapter(context=context)

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "U1111111",
        raising=False,
    )

    assert broker._fetch_real_cash() == pytest.approx(2000.0), (
        "配置子账户时，应按该账户过滤 accountSummary/accountValues。"
    )


def test_ib_fetch_real_cash_zero_pushes_account_hint_alarm_once(monkeypatch):
    """
    0 资金提示回归:
    配置了 IBKR_ORDER_ACCOUNT 且过滤后金额为 0 时，应推送“账号可能配置错误”提示，且去重。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForAccountScopedFunds())
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "DUO936692",
        raising=False,
    )

    got1 = broker._fetch_real_cash()
    got2 = broker._fetch_real_cash()

    assert got1 == pytest.approx(0.0)
    assert got2 == pytest.approx(0.0)
    assert len(pushed) == 1, "零资金提示应去重，避免高频路径刷屏。"
    assert pushed[0]["level"] == "ERROR"
    assert "IBKR_ORDER_ACCOUNT='DUO936692'" in pushed[0]["content"]
    assert "账号ID很可能填写错误" in pushed[0]["content"]


def test_ib_fetch_real_cash_zero_without_configured_account_no_alarm(monkeypatch):
    """
    0 资金提示回归:
    未配置 IBKR_ORDER_ACCOUNT 时，不应推送账号ID错误提示。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForZeroFunds())
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "   ",
        raising=False,
    )

    got = broker._fetch_real_cash()

    assert got == pytest.approx(0.0)
    assert pushed == [], "未配置账户时不应推送账号ID错误提示。"


def test_ib_fetch_real_cash_zero_when_ib_disconnected_no_alarm(monkeypatch):
    """
    启动时序回归:
    IB 未连接时，0资金不应触发账号错误告警，避免初始化误报。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBDisconnected())
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "DUO932692",
        raising=False,
    )

    got = broker._fetch_real_cash()

    assert got == pytest.approx(0.0)
    assert pushed == [], "IB 未连接时不应推送0资金账号告警。"


def test_ib_fetch_real_cash_zero_with_known_managed_account_no_id_error_alarm(monkeypatch):
    """
    0 资金提示回归:
    当账号已在 managedAccounts 中时，不应再推送“账号ID可能错误”告警。
    """
    context = types.SimpleNamespace(
        ib_instance=DummyIBForManagedAccountNoSnapshot(managed_accounts=["DUO932692"])
    )
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "DUO932692",
        raising=False,
    )

    got = broker._fetch_real_cash()

    assert got == pytest.approx(0.0)
    assert pushed == [], "账号已在 managedAccounts 时不应误报账号ID错误。"


def test_ib_fetch_real_cash_uses_aggregate_all_snapshot_when_account_specific_missing(monkeypatch):
    """
    账户快照兼容回归:
    accountSummary 仅返回 All 聚合账号时，不应被账户过滤误清空。
    """
    context = types.SimpleNamespace(
        ib_instance=DummyIBForAggregateAllSummary(managed_accounts=["DUO932692"])
    )
    broker = IBBrokerAdapter(context=context)

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "DUO932692",
        raising=False,
    )

    got = broker._fetch_real_cash()

    assert got == pytest.approx(7777.0), "All 聚合快照应作为可用兜底，避免误判 0 资金。"


def test_ib_get_cash_dedupes_open_orders_and_local_ledger():
    """
    占资去重回归:
    openTrades 已覆盖的本地订单不应重复扣减，但本地额外订单仍应继续扣减。
    """
    open_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=1, action="BUY"),
        contract=SimpleNamespace(symbol="AAPL"),
        orderStatus=SimpleNamespace(remaining=10),
    )
    context = types.SimpleNamespace(ib_instance=DummyIBForCashWithOpenTrades(cash_usd=10000.0, open_trades=[open_trade]))
    broker = IBBrokerAdapter(context=context)

    broker._tickers = {"AAPL": DummyTicker(100.0)}
    broker._active_buys = {
        "1": {"data": None, "shares": 10, "price": 100.0, "lot_size": 1, "retries": 0},
        "2": {"data": None, "shares": 5, "price": 200.0, "lot_size": 1, "retries": 0},
    }
    broker._virtual_spent_cash = (
        10 * 100.0 * broker.safety_multiplier
        + 5 * 200.0 * broker.safety_multiplier
    )

    expected_reserved = (
        10 * 100.0 * broker.safety_multiplier  # openTrades 估算冻结 (id=1)
        + 5 * 200.0 * broker.safety_multiplier  # 本地额外订单 (id=2)
    )
    expected_cash = 10000.0 - expected_reserved

    assert broker.get_cash() == pytest.approx(expected_cash), (
        "IB get_cash 占资去重异常：应扣减 openTrades 冻结 + 本地额外订单占资。"
    )


def test_ib_get_current_price_falls_back_to_provider_price_when_no_market_data(monkeypatch):
    """
    行情兜底回归:
    无实时订阅时应回退到非 CSV 数据源价格，保证定时实盘可计算下单量。"""
    import live_trader.adapters.ib_broker as ib_module

    class DummyProvider:
        def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                     timeframe: str = "Days", compression: int = 1):
            return pd.DataFrame(
                {"close": [123.4]},
                index=[pd.Timestamp("2026-03-10")],
            )

    class DummyDataManager:
        def __init__(self):
            self.providers = [DummyProvider()]

    monkeypatch.setattr(ib_module, "DataManager", DummyDataManager)

    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=0.0))
    broker = IBBrokerAdapter(context=context)
    broker._tickers = {"AAPL": DummyTicker(float("nan"))}

    data = SimpleNamespace(_name="AAPL")
    price = broker.get_current_price(data)

    assert price == pytest.approx(123.4), "无行情订阅时应回退到非 CSV 数据源价格。"


def test_ib_get_current_price_no_provider_price_alarms_and_blocks(monkeypatch):
    """
    行情兜底回归:
    无实时价且所有非 CSV 数据源均失败时，应报警并禁止下单。"""
    import live_trader.adapters.ib_broker as ib_module

    class DummyProvider:
        def get_data(self, *args, **kwargs):
            return pd.DataFrame()

    class DummyDataManager:
        def __init__(self):
            self.providers = [DummyProvider()]

    calls = []

    def _capture_alarm(self, content, level="INFO"):
        calls.append((content, level))

    monkeypatch.setattr(ib_module, "DataManager", DummyDataManager)
    monkeypatch.setattr(ib_module.AlarmManager, "push_text", _capture_alarm, raising=False)

    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=0.0))
    broker = IBBrokerAdapter(context=context)
    broker._tickers = {"AAPL": DummyTicker(float("nan"))}

    data = SimpleNamespace(_name="AAPL")
    price = broker.get_current_price(data)

    assert price == 0.0, "无有效价格时应返回 0，阻止下单。"
    assert calls, "无有效价格时应触发报警。"


def test_ib_get_current_price_switches_to_delayed_quote_when_realtime_missing():
    """
    行情自愈回归:
    实时报价无效时，应自动切换 delayed 行情模式并重取报价。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForDelayedQuote())
    broker = IBBrokerAdapter(context=context)
    broker._tickers = {"AAPL": DummyTicker(float("nan"))}

    data = SimpleNamespace(_name="AAPL")
    price = broker.get_current_price(data)
    price2 = broker.get_current_price(data)

    assert price == pytest.approx(98.76), "应在实时价缺失时自动回退 delayed 报价。"
    assert price2 == pytest.approx(98.76), "后续同标的报价应继续可用。"
    assert broker._delayed_market_data_enabled is True, "应记录 delayed 模式已启用。"
    assert context.ib_instance.market_data_types == [3], "应仅触发一次 delayed 模式切换请求。"
    assert context.ib_instance.req_mkt_calls == 1, "delayed 模式已生效后不应重复订阅同标的行情。"


def test_ib_get_current_price_uses_refreshed_ticker_after_delayed_switch():
    """
    行情自愈回归:
    delayed 重订阅后若首轮仍无价，应使用刷新后的 ticker 继续 close/last 兜底。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=0.0))
    broker = IBBrokerAdapter(context=context)
    broker._tickers = {"AAPL": DummyTicker(float("nan"))}

    def _fake_try_get_delayed_quote(symbol: str):
        refreshed = DummyTicker(float("nan"))
        refreshed.last = 66.6
        broker._tickers[symbol] = refreshed
        return 0.0

    broker._try_get_delayed_quote = _fake_try_get_delayed_quote

    price = broker.get_current_price(SimpleNamespace(_name="AAPL"))
    assert price == pytest.approx(66.6)


def test_ib_augment_live_data_source_appends_ibkr_fallback():
    """
    IB 启动配置回归:
    非 ib 单源应在 adapter 层自动补齐 ibkr 末位兜底。
    """
    assert IBBrokerAdapter._augment_live_data_source("tiingo") == "tiingo,ibkr"
    assert IBBrokerAdapter._augment_live_data_source("tiingo, ibkr") == "tiingo,ibkr"
    assert IBBrokerAdapter._augment_live_data_source("ibkr") == "ibkr"


def test_ib_fetch_smart_value_falls_back_to_base_when_fx_missing():
    """
    汇率兜底回归:
    若非 USD 货币无法换汇，但券商提供 BASE 汇总，必须回退 BASE，避免低估资金。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForMissingFx())
    broker = IBBrokerAdapter(context=context)

    val = broker._fetch_smart_value(["TotalCashValue"])

    assert val == pytest.approx(2000.0), "汇率缺口存在时应回退 BASE 汇总口径。"


def test_ib_get_rebalance_cash_uses_conservative_cash_floor():
    """
    调仓资金口径回归:
    get_rebalance_cash 应使用 min(get_cash, TotalCashValue)，避免计划层误用杠杆。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=10000.0))
    broker = IBBrokerAdapter(context=context)

    # spendable 口径 = 9000, TotalCashValue 口径 = 7000 => 调仓应取 7000
    broker.get_cash = lambda: 9000.0
    broker._fetch_smart_value = lambda tags=None: 7000.0 if tags == ["TotalCashValue"] else 9000.0

    assert broker.get_rebalance_cash() == pytest.approx(7000.0), (
        "调仓现金口径应取更保守值。"
    )


def test_ib_parse_contract_accepts_smart_suffix():
    """
    EWJ.SMART 这类符号必须拆分为 ticker + SMART 路由，不能把 '.SMART' 当作 ticker 一部分。
    """
    mock_ib_insync.Stock.reset_mock()

    IBBrokerAdapter.parse_contract("EWJ.SMART")

    mock_ib_insync.Stock.assert_called_with("EWJ", "SMART", "USD")


def test_ib_parse_contract_supports_generic_exchange_suffix():
    """
    任意常见交易所后缀应可直接识别，无需每次扩白名单。
    """
    mock_ib_insync.Stock.reset_mock()

    IBBrokerAdapter.parse_contract("AAPL.IEX")

    mock_ib_insync.Stock.assert_called_with("AAPL", "SMART", "USD", primaryExchange="IEX")


def test_ib_parse_contract_supports_exchange_prefix_variant():
    """
    兼容 Exchange.Ticker 写法（如 NASDAQ.AAPL）。
    """
    mock_ib_insync.Stock.reset_mock()

    IBBrokerAdapter.parse_contract("NASDAQ.AAPL")

    mock_ib_insync.Stock.assert_called_with("AAPL", "SMART", "USD", primaryExchange="NASDAQ")


def test_ib_parse_contract_keeps_share_class_symbols():
    """
    BRK.B 这类一位后缀是股票代码本体，不应误判为交易所。
    """
    mock_ib_insync.Stock.reset_mock()

    IBBrokerAdapter.parse_contract("BRK.B")

    mock_ib_insync.Stock.assert_called_with("BRK.B", "SMART", "USD")


def test_ib_get_position_matches_symbol_suffix_variants():
    """
    持仓匹配回归:
    data._name='EWJ.SMART' 时，应能命中 IB 返回的 symbol='EWJ' 持仓。
    """
    ib_positions = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="EWJ", secType="STK", localSymbol="EWJ"),
            position=140,
            avgCost=92.01,
        )
    ]
    context = types.SimpleNamespace(ib_instance=DummyIBForPositions(cash_usd=10000.0, positions=ib_positions))
    broker = IBBrokerAdapter(context=context)

    data = SimpleNamespace(_name="EWJ.SMART")
    pos = broker.get_position(data)

    assert pos.size == pytest.approx(140), "后缀容错失效：EWJ.SMART 必须匹配到 EWJ 持仓。"
    assert pos.price == pytest.approx(92.01), "持仓成本提取异常：应返回 IB 的 avgCost。"


def test_ib_get_position_filters_by_configured_order_account(monkeypatch):
    """
    账户隔离回归:
    配置了 IBKR_ORDER_ACCOUNT 时，持仓匹配应只在该账户范围内执行。
    """
    ib_positions = [
        SimpleNamespace(
            account="U2222222",
            contract=SimpleNamespace(symbol="EWJ", secType="STK", localSymbol="EWJ"),
            position=999,
            avgCost=88.01,
        ),
        SimpleNamespace(
            account="U1111111",
            contract=SimpleNamespace(symbol="EWJ", secType="STK", localSymbol="EWJ"),
            position=140,
            avgCost=92.01,
        ),
    ]
    context = types.SimpleNamespace(ib_instance=DummyIBForPositions(cash_usd=10000.0, positions=ib_positions))
    broker = IBBrokerAdapter(context=context)

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "U1111111",
        raising=False,
    )

    data = SimpleNamespace(_name="EWJ.SMART")
    pos = broker.get_position(data)

    assert pos.size == pytest.approx(140), "应过滤掉非目标账户持仓。"
    assert pos.price == pytest.approx(92.01), "应返回目标账户持仓成本。"


def test_ib_provider_parse_contract_supports_generic_exchange_suffix():
    """
    数据源与实盘 Broker 必须共享同一解析语义，避免一边能下单一边拉不到行情。
    """
    provider = IbkrDataProvider(ib_instance=MagicMock())
    mock_ib_insync.Stock.reset_mock()

    provider._parse_contract("MSFT.MEMX")

    mock_ib_insync.Stock.assert_called_with("MSFT", "SMART", "USD", primaryExchange="MEMX")


def test_ib_daily_schedule_parse_and_validation():
    """
    调度解析回归:
    - 支持 1d:HH:MM(:SS)
    - 非法时间抛出异常
    """
    parsed = SchedulePlanner.parse_daily_schedule("1d:15:45:00")
    assert parsed == (15, 45, 0, "15:45:00"), "应正确解析带秒的 daily schedule。"

    parsed_no_sec = SchedulePlanner.parse_daily_schedule("1d:09:30")
    assert parsed_no_sec == (9, 30, 0, "09:30"), "应兼容不带秒的 daily schedule。"

    with pytest.raises(ValueError):
        SchedulePlanner.parse_daily_schedule("1d:25:61:00")


def test_ib_daily_schedule_should_only_trigger_in_tolerance_window():
    """
    调度触发回归:
    - 到点后短窗口内可触发
    - 错过窗口后同日不补跑
    - 同日已跑过不再重复触发
    """
    now = datetime.datetime(2026, 2, 27, 15, 45, 2)
    should_run, delta, day_str = IBBrokerAdapter._should_trigger_daily_schedule(
        now=now,
        target_h=15,
        target_m=45,
        target_s=0,
        last_schedule_run_date=None,
    )
    assert should_run, "目标时间后的容忍窗口内应触发。"
    assert delta == pytest.approx(2.0), "delta 应为当前与目标时刻的秒差。"
    assert day_str == "2026-02-27"

    late_now = datetime.datetime(2026, 2, 27, 15, 50, 0)
    should_run_late, _, _ = IBBrokerAdapter._should_trigger_daily_schedule(
        now=late_now,
        target_h=15,
        target_m=45,
        target_s=0,
        last_schedule_run_date=None,
    )
    assert not should_run_late, "错过容忍窗口后不应补跑。"

    should_run_again, _, _ = IBBrokerAdapter._should_trigger_daily_schedule(
        now=now,
        target_h=15,
        target_m=45,
        target_s=0,
        last_schedule_run_date="2026-02-27",
    )
    assert not should_run_again, "同一自然日只允许运行一次。"


def test_ib_interval_schedule_parse_and_trigger_inside_slot_window():
    """
    固定频率调度回归:
    - 支持 5m:HH:MM:SS 解析
    - 仅在 slot 后短窗口内触发
    - 同一 slot 不重复触发
    """
    parsed = SchedulePlanner.parse_schedule_rule("5m:09:30:00")
    assert parsed is not None
    assert parsed["freq_n"] == 5
    assert parsed["freq_unit"] == "m"
    assert parsed["interval_seconds"] == pytest.approx(300.0)

    now = datetime.datetime(2026, 2, 27, 9, 35, 2)
    should_run, delta, slot_key = SchedulePlanner.should_trigger_schedule(
        now=now,
        parsed_schedule=parsed,
        last_schedule_run_key=None,
    )
    assert should_run, "进入 09:35 slot 的容忍窗口后应触发。"
    assert delta == pytest.approx(2.0)
    assert slot_key == "2026-02-27 09:35:00"

    should_repeat, _, _ = SchedulePlanner.should_trigger_schedule(
        now=now,
        parsed_schedule=parsed,
        last_schedule_run_key="2026-02-27 09:35:00",
    )
    assert not should_repeat, "同一 slot 不应重复运行。"

    late_now = datetime.datetime(2026, 2, 27, 9, 35, 6)
    should_late, _, _ = SchedulePlanner.should_trigger_schedule(
        now=late_now,
        parsed_schedule=parsed,
        last_schedule_run_key=None,
    )
    assert not should_late, "错过 slot 容忍窗口后不应补跑。"

    before_anchor = datetime.datetime(2026, 2, 27, 9, 29, 59)
    should_before_anchor, delta_before_anchor, slot_key_before_anchor = SchedulePlanner.should_trigger_schedule(
        now=before_anchor,
        parsed_schedule=parsed,
        last_schedule_run_key=None,
    )
    assert not should_before_anchor
    assert delta_before_anchor == pytest.approx(-1.0)
    assert slot_key_before_anchor is None


def test_ib_schedule_prewarm_lead_parse_and_validation():
    """
    预热提前量解析回归:
    - 支持 0 / 秒数 / 1s|1m|1h
    - 非法格式抛出异常
    """
    assert SchedulePlanner.parse_schedule_prewarm_lead(0) == pytest.approx(0.0)
    assert SchedulePlanner.parse_schedule_prewarm_lead("1s") == pytest.approx(1.0)
    assert SchedulePlanner.parse_schedule_prewarm_lead("5m") == pytest.approx(300.0)
    assert SchedulePlanner.parse_schedule_prewarm_lead("1h") == pytest.approx(3600.0)
    assert SchedulePlanner.parse_schedule_prewarm_lead(15) == pytest.approx(15.0)

    with pytest.raises(ValueError):
        SchedulePlanner.parse_schedule_prewarm_lead("tomorrow")


def test_ib_schedule_prewarm_should_only_trigger_inside_lead_window():
    """
    预热触发回归:
    - 仅在 schedule 前 lead 窗口内触发
    - 同一自然日只预热一次
    - schedule 已执行时不再预热
    """
    now = datetime.datetime(2026, 2, 27, 15, 44, 30)
    should_prewarm, seconds_to_schedule, day_str = SchedulePlanner.should_trigger_schedule_prewarm(
        now=now,
        target_h=15,
        target_m=45,
        target_s=0,
        lead_seconds=60.0,
        last_prewarm_run_date=None,
        last_schedule_run_date=None,
    )
    assert should_prewarm, "schedule 前 lead 窗口内应触发预热。"
    assert seconds_to_schedule == pytest.approx(30.0)
    assert day_str == "2026-02-27"

    early_now = datetime.datetime(2026, 2, 27, 15, 43, 30)
    should_early, _, _ = SchedulePlanner.should_trigger_schedule_prewarm(
        now=early_now,
        target_h=15,
        target_m=45,
        target_s=0,
        lead_seconds=60.0,
        last_prewarm_run_date=None,
        last_schedule_run_date=None,
    )
    assert not should_early, "尚未进入 lead 窗口时不应触发预热。"

    late_now = datetime.datetime(2026, 2, 27, 15, 45, 1)
    should_late, _, _ = SchedulePlanner.should_trigger_schedule_prewarm(
        now=late_now,
        target_h=15,
        target_m=45,
        target_s=0,
        lead_seconds=60.0,
        last_prewarm_run_date=None,
        last_schedule_run_date=None,
    )
    assert not should_late, "schedule 到点后不应再触发预热。"

    should_repeat, _, _ = SchedulePlanner.should_trigger_schedule_prewarm(
        now=now,
        target_h=15,
        target_m=45,
        target_s=0,
        lead_seconds=60.0,
        last_prewarm_run_date="2026-02-27",
        last_schedule_run_date=None,
    )
    assert not should_repeat, "同一自然日不应重复预热。"

    should_after_run, _, _ = SchedulePlanner.should_trigger_schedule_prewarm(
        now=now,
        target_h=15,
        target_m=45,
        target_s=0,
        lead_seconds=60.0,
        last_prewarm_run_date=None,
        last_schedule_run_date="2026-02-27",
    )
    assert not should_after_run, "schedule 已执行后不应再触发预热。"


def test_ib_interval_schedule_prewarm_is_reversed_from_next_slot():
    """
    固定频率预热回归:
    prewarm 必须基于“下一个正式 slot”逆推，而不是独立 cron。
    """
    parsed = SchedulePlanner.parse_schedule_rule("5m:09:30:00")

    now = datetime.datetime(2026, 2, 27, 9, 34, 1)
    should_prewarm, seconds_to_schedule, slot_key = SchedulePlanner.should_trigger_schedule_prewarm_for_rule(
        now=now,
        parsed_schedule=parsed,
        lead_seconds=60.0,
        last_prewarm_run_key=None,
        last_schedule_run_key=None,
    )
    assert should_prewarm, "应在 09:35 slot 前 60s 窗口内触发预热。"
    assert seconds_to_schedule == pytest.approx(59.0)
    assert slot_key == "2026-02-27 09:35:00"

    before_window = datetime.datetime(2026, 2, 27, 9, 33, 58)
    should_early, _, _ = SchedulePlanner.should_trigger_schedule_prewarm_for_rule(
        now=before_window,
        parsed_schedule=parsed,
        lead_seconds=60.0,
        last_prewarm_run_key=None,
        last_schedule_run_key=None,
    )
    assert not should_early, "尚未进入下一个 slot 的逆推窗口时不应预热。"

    should_repeat, _, _ = SchedulePlanner.should_trigger_schedule_prewarm_for_rule(
        now=now,
        parsed_schedule=parsed,
        lead_seconds=60.0,
        last_prewarm_run_key="2026-02-27 09:35:00",
        last_schedule_run_key=None,
    )
    assert not should_repeat, "同一 slot 不应重复预热。"

    should_after_run, _, _ = SchedulePlanner.should_trigger_schedule_prewarm_for_rule(
        now=now,
        parsed_schedule=parsed,
        lead_seconds=60.0,
        last_prewarm_run_key=None,
        last_schedule_run_key="2026-02-27 09:35:00",
    )
    assert not should_after_run, "正式 slot 已执行后不应再为该 slot 预热。"


def test_ib_schedule_prewarm_disabled_when_lead_is_zero():
    """
    默认配置回归:
    lead=0 时必须完全关闭预热。
    """
    now = datetime.datetime(2026, 2, 27, 15, 44, 30)
    should_prewarm, seconds_to_schedule, day_str = SchedulePlanner.should_trigger_schedule_prewarm(
        now=now,
        target_h=15,
        target_m=45,
        target_s=0,
        lead_seconds=0.0,
        last_prewarm_run_date=None,
        last_schedule_run_date=None,
    )
    assert not should_prewarm, "lead=0 时不应触发任何预热。"
    assert seconds_to_schedule == pytest.approx(30.0)
    assert day_str == "2026-02-27"


def test_ib_schedule_preview_builds_next_three_daily_slots_with_prewarm():
    parsed = SchedulePlanner.parse_schedule_rule("1d:15:45:00")

    previews = SchedulePlanner.build_schedule_preview(
        now=datetime.datetime(2026, 4, 11, 15, 44, 10),
        parsed_schedule=parsed,
        prewarm_lead_seconds=60.0,
        count=3,
    )

    assert [item["slot_dt"].strftime("%Y-%m-%d %H:%M:%S") for item in previews] == [
        "2026-04-11 15:45:00",
        "2026-04-12 15:45:00",
        "2026-04-13 15:45:00",
    ]
    assert [item["prewarm_dt"].strftime("%Y-%m-%d %H:%M:%S") for item in previews] == [
        "2026-04-11 15:44:00",
        "2026-04-12 15:44:00",
        "2026-04-13 15:44:00",
    ]


def test_ib_schedule_preview_builds_next_three_interval_slots_with_prewarm():
    parsed = SchedulePlanner.parse_schedule_rule("5m:09:30:00")

    previews = SchedulePlanner.build_schedule_preview(
        now=datetime.datetime(2026, 2, 27, 9, 34, 10),
        parsed_schedule=parsed,
        prewarm_lead_seconds=60.0,
        count=3,
    )

    assert [item["slot_dt"].strftime("%Y-%m-%d %H:%M:%S") for item in previews] == [
        "2026-02-27 09:35:00",
        "2026-02-27 09:40:00",
        "2026-02-27 09:45:00",
    ]
    assert [item["prewarm_dt"].strftime("%Y-%m-%d %H:%M:%S") for item in previews] == [
        "2026-02-27 09:34:00",
        "2026-02-27 09:39:00",
        "2026-02-27 09:44:00",
    ]


def test_ib_prewarm_before_schedule_uses_first_data_and_fx(monkeypatch):
    """
    预热执行回归:
    优先使用 broker 第一个 data 的当前价路径，并额外预热 USDHKD。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=0.0))
    broker = IBBrokerAdapter(context=context)
    broker.datas = [SimpleNamespace(_name="QQQ.SMART")]

    seen = {"price": [], "fx": []}

    def _fake_price(data):
        seen["price"].append(data._name)
        return 123.4

    def _fake_fx(pair_symbol, in_loop=None):
        seen["fx"].append(pair_symbol)
        return 7.8

    monkeypatch.setattr(broker, "get_current_price", _fake_price)
    monkeypatch.setattr(broker, "_load_fx_rate", _fake_fx)

    summary = broker.prewarm_before_schedule(
        data_provider=None,
        symbols=["QQQ.SMART"],
        timeframe="Days",
        compression=1,
        now=datetime.datetime(2026, 4, 11, 15, 44, 0),
    )

    assert summary["source"] == "broker"
    assert summary["symbol"] == "QQQ.SMART"
    assert summary["price"] == pytest.approx(123.4)
    assert summary["extras"] == ["USDHKD"]
    assert seen["price"] == ["QQQ.SMART"]
    assert seen["fx"] == ["USDHKD"]


def test_ib_prewarm_before_schedule_falls_back_to_data_provider_when_no_datas(monkeypatch):
    """
    预热执行回归:
    若 broker 尚无 datas，应回退到 data_provider 的首标的最小窗口请求。
    """
    class StubProvider:
        def __init__(self):
            self.calls = []

        def get_history(self, symbol: str, start_date: str, end_date: str,
                        timeframe: str = "Days", compression: int = 1):
            self.calls.append(
                {
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "timeframe": timeframe,
                    "compression": compression,
                }
            )
            return pd.DataFrame({"close": [1.0]}, index=[pd.Timestamp("2026-04-11 15:44:00")])

    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=0.0))
    broker = IBBrokerAdapter(context=context)
    provider = StubProvider()
    monkeypatch.setattr(broker, "_load_fx_rate", lambda pair_symbol, in_loop=None: 0.0)

    summary = broker.prewarm_before_schedule(
        data_provider=provider,
        symbols=["QQQ.SMART", "SPY.ARCA"],
        timeframe="Minutes",
        compression=5,
        now=datetime.datetime(2026, 4, 11, 15, 44, 0),
    )

    assert summary["source"] == "data_provider"
    assert summary["symbol"] == "QQQ.SMART"
    assert summary["history_rows"] == 1
    assert provider.calls[0]["symbol"] == "QQQ.SMART"
    assert provider.calls[0]["start_date"] == "2026-04-11 15:29:00"
    assert provider.calls[0]["end_date"] == "2026-04-11 15:44:00"


def test_ib_schedule_prewarm_summary_error_pushes_warning_alarm_once(monkeypatch):
    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.data_bridge.data_warm as data_warm_module

    monkeypatch.setattr(data_warm_module, "AlarmManager", lambda: DummyAlarm())

    now = datetime.datetime(2026, 4, 11, 15, 44, 0)
    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=0.0), now=now)
    broker = IBBrokerAdapter(context=context)

    sent = broker.alarm_schedule_prewarm_issue_once(
        schedule_rule="1d:15:45:00",
        now=now,
        summary={
            "source": "broker",
            "symbol": "QQQ.SMART",
            "extras": [],
            "errors": ["broker:no price"],
        },
        level='WARNING',
    )
    sent_repeat = broker.alarm_schedule_prewarm_issue_once(
        schedule_rule="1d:15:45:00",
        now=now,
        summary={
            "source": "broker",
            "symbol": "QQQ.SMART",
            "extras": [],
            "errors": ["broker:no price"],
        },
        level='WARNING',
    )

    assert sent is True
    assert sent_repeat is False, "同一自然日同一 schedule 的预热异常告警应去重。"
    assert len(pushed) == 1
    assert pushed[0]["level"] == "WARNING"
    assert "Schedule prewarm finished with errors before 1d:15:45:00" in pushed[0]["content"]
    assert "QQQ.SMART" in pushed[0]["content"]
    assert "Normal schedule will continue." in pushed[0]["content"]


def test_ib_schedule_prewarm_exception_pushes_error_alarm_without_raising(monkeypatch):
    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.data_bridge.data_warm as data_warm_module

    monkeypatch.setattr(data_warm_module, "AlarmManager", lambda: DummyAlarm())

    now = datetime.datetime(2026, 4, 11, 15, 44, 0)
    context = types.SimpleNamespace(ib_instance=DummyIBForCash(cash_usd=0.0), now=now)
    broker = IBBrokerAdapter(context=context)

    sent = broker.alarm_schedule_prewarm_issue_once(
        schedule_rule="1d:15:45:00",
        now=now,
        error=RuntimeError("boom"),
        level='ERROR',
    )

    assert sent is True
    assert len(pushed) == 1
    assert pushed[0]["level"] == "ERROR"
    assert "Schedule prewarm failed before 1d:15:45:00: boom" in pushed[0]["content"]
    assert "Normal schedule will continue." in pushed[0]["content"]


def test_ib_submit_order_allows_fractional_sell_for_full_close(monkeypatch):
    """
    清仓碎股回归:
    SELL 委托在存在小数股时应允许透传小数，避免尾仓永远无法清掉。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForSubmit())
    broker = IBBrokerAdapter(context=context)

    monkeypatch.setattr(
        mock_ib_insync,
        "MarketOrder",
        lambda action, qty: SimpleNamespace(action=action, totalQuantity=qty),
    )
    # 同步替换模块内引用，确保 _submit_order 使用到 patched 构造器
    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module,
        "MarketOrder",
        lambda action, qty: SimpleNamespace(action=action, totalQuantity=qty),
    )
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ALLOW_FRACTIONAL_SELL",
        True,
        raising=False,
    )

    data = SimpleNamespace(_name="AAPL.SMART")
    proxy = broker._submit_order(data=data, volume=0.6441, side="SELL", price=100.0)

    assert proxy is not None, "分数股卖出应提交订单，不应被 <1 直接跳过。"
    assert context.ib_instance.last_order.totalQuantity == pytest.approx(0.6441, rel=1e-6), (
        "SELL 小数股应按原始数量提交，避免残留尾仓。"
    )


def test_ib_submit_order_fractional_sell_disabled_by_default(monkeypatch):
    """
    安全默认值回归:
    未显式开启时，SELL 小数股应向下取整为整数，避免触发 IB API 10243。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForSubmit())
    broker = IBBrokerAdapter(context=context)

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module,
        "MarketOrder",
        lambda action, qty: SimpleNamespace(action=action, totalQuantity=qty),
    )
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ALLOW_FRACTIONAL_SELL",
        False,
        raising=False,
    )

    data = SimpleNamespace(_name="AAPL.SMART")
    proxy = broker._submit_order(data=data, volume=104.617, side="SELL", price=100.0)

    assert proxy is not None, "禁用小数股时也应提交整数卖单。"
    assert context.ib_instance.last_order.totalQuantity == 104, (
        "禁用小数股时，SELL 数量应向下取整为整数。"
    )


def test_ib_submit_order_uses_configured_order_account(monkeypatch):
    """
    子账户路由回归:
    配置了 IBKR_ORDER_ACCOUNT 时，应将其写入 MarketOrder.account。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForSubmit())
    broker = IBBrokerAdapter(context=context)

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module,
        "MarketOrder",
        lambda action, qty: SimpleNamespace(action=action, totalQuantity=qty, account=""),
    )
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "U1234567",
        raising=False,
    )

    data = SimpleNamespace(_name="AAPL.SMART")
    proxy = broker._submit_order(data=data, volume=10, side="BUY", price=100.0)

    assert proxy is not None
    assert context.ib_instance.last_order.account == "U1234567", (
        "配置子账户时，应透传到 MarketOrder.account。"
    )


def test_ib_submit_order_uses_default_account_when_config_empty(monkeypatch):
    """
    默认主账户回归:
    IBKR_ORDER_ACCOUNT 留空时，不应覆盖 MarketOrder.account。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForSubmit())
    broker = IBBrokerAdapter(context=context)

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module,
        "MarketOrder",
        lambda action, qty: SimpleNamespace(action=action, totalQuantity=qty, account=""),
    )
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "   ",
        raising=False,
    )

    data = SimpleNamespace(_name="AAPL.SMART")
    proxy = broker._submit_order(data=data, volume=10, side="BUY", price=100.0)

    assert proxy is not None
    assert context.ib_instance.last_order.account == "", (
        "配置留空时，应维持 IB 默认主账户路由。"
    )


def test_ib_submit_order_blocks_when_multiple_accounts_without_config(monkeypatch):
    """
    多账户防误路由回归:
    未配置 IBKR_ORDER_ACCOUNT 且存在多个账户时，应阻止下单并提示配置。"""
    context = types.SimpleNamespace(
        ib_instance=DummyIBForSubmit(managed_accounts=["U1111111", "U2222222"])
    )
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module,
        "MarketOrder",
        lambda action, qty: SimpleNamespace(action=action, totalQuantity=qty, account=""),
    )
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "   ",
        raising=False,
    )
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())

    data = SimpleNamespace(_name="AAPL.SMART")
    proxy = broker._submit_order(data=data, volume=10, side="BUY", price=100.0)

    assert proxy is None, "多账户且未配置下单账户时应阻止发单。"
    assert context.ib_instance.last_order is None, "阻止发单时不应调用 placeOrder。"
    assert len(pushed) == 1, "应推送一次配置提示告警。"
    assert pushed[0]["level"] == "ERROR"
    assert "IBKR_ORDER_ACCOUNT" in pushed[0]["content"]
    assert "U1111111" in pushed[0]["content"]
    assert "U2222222" in pushed[0]["content"]


def test_ib_missing_order_account_warning_repeats_by_daily_schedule_slot(monkeypatch):
    """
    多账户阻断告警按 schedule slot 去重:
    1d schedule 应同一天只报一次，跨天重新报警。
    """
    context = types.SimpleNamespace(
        ib_instance=DummyIBForSubmit(managed_accounts=["U1111111", "U2222222"]),
        schedule_rule="1d:15:45:00",
        now=pd.Timestamp("2026-05-05 15:45:00"),
    )
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "   ",
        raising=False,
    )
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())

    data = SimpleNamespace(_name="AAPL.SMART")
    assert broker._submit_order(data=data, volume=10, side="BUY", price=100.0) is None
    assert broker._submit_order(data=data, volume=10, side="BUY", price=100.0) is None

    context.now = pd.Timestamp("2026-05-06 15:45:00")
    assert broker._submit_order(data=data, volume=10, side="BUY", price=100.0) is None

    assert len(pushed) == 2, "1d schedule 应跨自然日重新报警。"
    assert all(item["level"] == "ERROR" for item in pushed)


def test_ib_missing_order_account_warning_repeats_by_interval_schedule_slot(monkeypatch):
    """
    多账户阻断告警按 schedule 前缀去重:
    5m schedule 应同一 slot 只报一次，进入下一个 5m slot 后重新报警。
    """
    context = types.SimpleNamespace(
        ib_instance=DummyIBForSubmit(managed_accounts=["U1111111", "U2222222"]),
        schedule_rule="5m:09:30:00",
        now=pd.Timestamp("2026-05-05 09:35:00"),
    )
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "   ",
        raising=False,
    )
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())

    data = SimpleNamespace(_name="AAPL.SMART")
    assert broker._submit_order(data=data, volume=10, side="BUY", price=100.0) is None
    context.now = pd.Timestamp("2026-05-05 09:35:04")
    assert broker._submit_order(data=data, volume=10, side="BUY", price=100.0) is None

    context.now = pd.Timestamp("2026-05-05 09:40:00")
    assert broker._submit_order(data=data, volume=10, side="BUY", price=100.0) is None

    assert len(pushed) == 2, "5m schedule 应在下一个 slot 重新报警。"
    assert all(item["level"] == "ERROR" for item in pushed)


def test_ib_submit_order_rejects_unknown_configured_order_account(monkeypatch):
    """
    子账户校验回归:
    配置账户不在 managedAccounts 内时，应拒绝发单。
    """
    context = types.SimpleNamespace(ib_instance=DummyIBForSubmit(managed_accounts=["U1234567"]))
    broker = IBBrokerAdapter(context=context)

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module,
        "MarketOrder",
        lambda action, qty: SimpleNamespace(action=action, totalQuantity=qty, account=""),
    )
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "DUO936692",
        raising=False,
    )

    data = SimpleNamespace(_name="AAPL.SMART")
    proxy = broker._submit_order(data=data, volume=10, side="BUY", price=100.0)

    assert proxy is None, "未知账户应拒绝发单，避免误路由到默认账户。"
    assert context.ib_instance.last_order is None, "拒绝发单时不应调用 placeOrder。"


def test_ib_pending_order_contract_includes_id():
    """
    最小契约:
    get_pending_orders 返回项必须包含 id，供基础层隔夜清理协议使用。
    """
    open_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=138, action="BUY"),
        contract=SimpleNamespace(symbol="EWJ"),
        orderStatus=SimpleNamespace(remaining=282),
    )
    context = types.SimpleNamespace(
        ib_instance=DummyIBForCashWithOpenTrades(cash_usd=10000.0, open_trades=[open_trade])
    )
    broker = IBBrokerAdapter(context=context)

    got = broker.get_pending_orders()
    assert len(got) == 1, "应返回 1 笔在途单。"
    assert got[0]["id"] == "138", "在途单契约缺失 id。"
    assert got[0]["symbol"] == "EWJ"
    assert got[0]["direction"] == "BUY"
    assert got[0]["size"] == 282


def test_ib_get_pending_orders_filters_by_configured_order_account(monkeypatch):
    """
    账户隔离回归:
    配置了 IBKR_ORDER_ACCOUNT 时，应只返回该账户在途单。
    """
    open_trade_keep = SimpleNamespace(
        order=SimpleNamespace(orderId=138, action="BUY", account="U1111111"),
        contract=SimpleNamespace(symbol="EWJ"),
        orderStatus=SimpleNamespace(status="Submitted", remaining=282),
    )
    open_trade_drop = SimpleNamespace(
        order=SimpleNamespace(orderId=139, action="BUY", account="U2222222"),
        contract=SimpleNamespace(symbol="EWJ"),
        orderStatus=SimpleNamespace(status="Submitted", remaining=111),
    )
    context = types.SimpleNamespace(
        ib_instance=DummyIBForCashWithOpenTrades(
            cash_usd=10000.0,
            open_trades=[open_trade_keep, open_trade_drop],
        )
    )
    broker = IBBrokerAdapter(context=context)

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(
        ib_module.config,
        "IBKR_ORDER_ACCOUNT",
        "U1111111",
        raising=False,
    )

    got = broker.get_pending_orders()
    assert len(got) == 1
    assert got[0]["id"] == "138"
    assert got[0]["size"] == 282


def test_ib_cancel_pending_order_by_id():
    """
    最小契约:
    cancel_pending_order(order_id) 应能根据 id 定位并发起撤单。
    """

    class DummyIBForCancel(DummyIBForCashWithOpenTrades):
        def __init__(self, cash_usd, open_trades):
            super().__init__(cash_usd=cash_usd, open_trades=open_trades)
            self.cancel_calls = []

        def cancelOrder(self, order):
            self.cancel_calls.append(order)
            for t in self._open_trades:
                if getattr(t, "order", None) is order and getattr(t, "orderStatus", None) is not None:
                    setattr(t.orderStatus, "status", "Cancelled")
                    setattr(t.orderStatus, "remaining", 0)

    order_obj = SimpleNamespace(orderId=313, action="BUY")
    open_trade = SimpleNamespace(
        order=order_obj,
        contract=SimpleNamespace(symbol="EWJ"),
        orderStatus=SimpleNamespace(remaining=281),
    )
    ib = DummyIBForCancel(cash_usd=10000.0, open_trades=[open_trade])
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    ok = broker.cancel_pending_order("313")
    assert ok is True, "按 id 撤单应返回 True。"
    assert ib.cancel_calls == [order_obj], "应调用 IB cancelOrder 并传入原始 order 对象。"


def test_ib_get_pending_orders_fallback_to_req_all_open_orders():
    """
    跨 client 视角回归:
    openTrades 为空时，仍应通过 reqAllOpenOrders 识别手动/其他 client 的在途单。
    """
    manual_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=901, action="BUY"),
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=77),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[],
        all_open_trades=[manual_trade],
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    got = broker.get_pending_orders()

    assert ib.req_all_calls == 1, "openTrades 为空时应拉取 reqAllOpenOrders 快照。"
    assert len(got) == 1, "应识别到 1 笔跨 client 在途单。"
    assert got[0]["id"] == "901"
    assert got[0]["symbol"] == "PSQ"
    assert got[0]["direction"] == "BUY"
    assert got[0]["size"] == 77


def test_ib_cancel_pending_order_uses_req_all_open_orders_fallback():
    """
    撤单回归:
    openTrades 为空时，cancel_pending_order 也应能基于 reqAllOpenOrders 定位并撤单。
    """
    order_obj = SimpleNamespace(orderId=902, action="BUY")
    manual_trade = SimpleNamespace(
        order=order_obj,
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=10),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[],
        all_open_trades=[manual_trade],
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    ok = broker.cancel_pending_order("902")

    assert ok is True, "应能通过 reqAllOpenOrders 识别并撤销在途单。"
    assert ib.cancel_calls == [order_obj], "撤单应透传原始 order 对象。"


def test_ib_pending_order_uses_perm_id_when_order_id_missing():
    """
    手动单兼容回归:
    若 orderId 缺失/无效，get_pending_orders 应回退到 permId，避免 cleanup skipped。
    """
    manual_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=0, permId=456789, action="BUY"),
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=278.9289, permId=456789),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[],
        all_open_trades=[manual_trade],
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    got = broker.get_pending_orders()

    assert len(got) == 1
    assert got[0]["id"] == "perm:456789", "缺失 orderId 时应回退使用 permId。"


def test_ib_cancel_pending_order_supports_perm_id():
    """
    手动单撤单回归:
    cleanup 传入 perm:xxx 时，cancel_pending_order 应能匹配并撤单。
    """
    order_obj = SimpleNamespace(orderId=905, permId=777001, action="BUY")
    manual_trade = SimpleNamespace(
        order=order_obj,
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=10, permId=777001),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[],
        all_open_trades=[manual_trade],
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    ok = broker.cancel_pending_order("perm:777001")

    assert ok is True, "permId 兜底 ID 也应支持撤单。"
    assert ib.cancel_calls == [order_obj]


def test_ib_cancel_pending_order_perm_id_returns_false_when_order_id_not_bindable():
    """
    手工单不可绑回归:
    仅有 permId 且 orderId=0 时，不应误报撤单成功。
    """
    order_obj = SimpleNamespace(orderId=0, permId=888001, action="BUY")
    manual_trade = SimpleNamespace(
        order=order_obj,
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=9, permId=888001),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[],
        all_open_trades=[manual_trade],
        client_id=999,
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    ok = broker.cancel_pending_order("perm:888001")

    assert ok is False, "orderId 无法绑定时应返回 False，避免假阳性。"
    assert ib.cancel_calls == [], "不可绑场景不应调用 cancelOrder(orderId=0)。"
    assert ib.req_open_calls >= 1, "应至少尝试一次 reqOpenOrders 绑定。"


def test_ib_cancel_pending_order_perm_id_succeeds_after_bind():
    """
    手工单绑定回归:
    reqOpenOrders 后若该 permId 获得有效 orderId，应继续完成撤单。
    """
    order_obj = SimpleNamespace(orderId=0, permId=888002, action="BUY")
    manual_trade = SimpleNamespace(
        order=order_obj,
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=9, permId=888002),
    )

    def _bind_order():
        order_obj.orderId = 906

    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[],
        all_open_trades=[manual_trade],
        on_req_open_orders=_bind_order,
        client_id=0,
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    ok = broker.cancel_pending_order("perm:888002")

    assert ok is True, "绑定成功后应可撤单。"
    assert ib.cancel_calls == [order_obj]
    assert ib.req_open_calls >= 1


def test_ib_pending_order_accepts_negative_order_id_after_manual_bind():
    """
    TWS 手工单绑定回归:
    绑定后若分配负数 orderId（IB 常见行为），应视为有效可撤单 ID。
    """
    manual_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=-42, permId=901001, action="BUY"),
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=3, permId=901001),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[manual_trade],
        all_open_trades=[manual_trade],
        client_id=0,
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    got = broker.get_pending_orders()
    ok = broker.cancel_pending_order("-42")

    assert len(got) == 1
    assert got[0]["id"] == "-42", "负数 orderId 应保留，不应误判为空。"
    assert ok is True, "负数 orderId 也应可正常撤单。"
    assert ib.cancel_calls == [manual_trade.order]


def test_ib_get_pending_orders_ignores_cancelled_even_if_remaining_positive():
    """
    终态过滤回归:
    即便 remaining 尚未归零，Cancelled 也不应再被识别为 pending。
    """
    cancelled_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=-2, action="BUY"),
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(status="Cancelled", remaining=278.9289),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[cancelled_trade],
        all_open_trades=[cancelled_trade],
        client_id=0,
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    got = broker.get_pending_orders()
    ok = broker.cancel_pending_order("-2")

    assert got == [], "Cancelled 不应继续出现在 pending 列表。"
    assert ok is False, "Cancelled 终态不应重复发起撤单。"
    assert ib.cancel_calls == []


def test_ib_get_pending_orders_does_not_use_trade_cache_by_default():
    """
    陈旧缓存防回归:
    trades() 里的历史终态单不应污染 pending 识别。
    """
    stale_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=-3, action="BUY"),
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(status="Cancelled", remaining=88),
    )

    class DummyIBWithStaleTradeCache(DummyIBForAllOpenOrders):
        def trades(self):
            return [stale_trade]

    ib = DummyIBWithStaleTradeCache(
        cash_usd=10000.0,
        open_trades=[],
        all_open_trades=[],
        client_id=0,
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    got = broker.get_pending_orders()
    assert got == [], "默认不应使用 trades() 作为 pending 来源。"


def test_ib_cancel_pending_order_unresolved_manual_order_pushes_clientid_hint_alarm_once(monkeypatch):
    """
    告警回归:
    当手工单仅有 permId 且 clientId!=0 无法撤单时，应提示改为 IBKR_CLIENT_ID=0，
    且同一订单键仅告警一次，避免重试刷屏。
    """
    order_obj = SimpleNamespace(orderId=0, permId=990001, action="BUY")
    manual_trade = SimpleNamespace(
        order=order_obj,
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=9, permId=990001),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[],
        all_open_trades=[manual_trade],
        client_id=999,
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())

    ok1 = broker.cancel_pending_order("perm:990001")
    ok2 = broker.cancel_pending_order("perm:990001")

    assert ok1 is False and ok2 is False
    assert len(pushed) == 1, "同一 unresolved 手工单应只告警一次。"
    assert pushed[0]["level"] == "ERROR"
    assert "IBKR_CLIENT_ID=0" in pushed[0]["content"], "告警应给出 clientId 修复建议。"


def test_ib_cancel_pending_order_not_confirmed_pushes_clientid_hint_when_nonzero(monkeypatch):
    """
    撤单确认回归:
    cancelOrder 调用后若短确认仍未离开 pending，应返回 False 并提示 clientId=0 风险。
    """
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=-8, permId=990101, action="BUY"),
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(status="Submitted", remaining=100),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[trade],
        all_open_trades=[trade],
        client_id=999,
        cancel_effective=False,  # 模拟 IB 侧未真正撤掉
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())
    monkeypatch.setattr(broker, "_sleep_ib", lambda _s: None)

    ok = broker.cancel_pending_order("-8")

    assert ok is False, "撤单短确认失败时不应记为成功。"
    assert ib.cancel_calls == [trade.order], "仍应发起一次 cancelOrder 请求。"
    assert len(pushed) == 1, "clientId!=0 且撤单未确认时应推送一次提示告警。"
    assert "IBKR_CLIENT_ID=0" in pushed[0]["content"]


def test_ib_cancel_pending_order_not_confirmed_pushes_bind_hint_when_clientid_zero(monkeypatch):
    """
    撤单确认回归:
    clientId=0 下若撤单仍未确认，也应推送“检查 TWS 绑定设置”的撤单失败告警。
    """
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=-9, permId=990102, action="BUY"),
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(status="Submitted", remaining=50),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[trade],
        all_open_trades=[trade],
        client_id=0,
        cancel_effective=False,
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    pushed = []

    class DummyAlarm:
        def push_text(self, content, level='INFO'):
            pushed.append({"content": content, "level": level})

    import live_trader.adapters.ib_broker as ib_module
    monkeypatch.setattr(ib_module, "AlarmManager", lambda: DummyAlarm())
    monkeypatch.setattr(broker, "_sleep_ib", lambda _s: None)

    ok = broker.cancel_pending_order("-9")

    assert ok is False
    assert len(pushed) == 1
    assert "clientId is already 0" in pushed[0]["content"]


def test_ib_collect_open_trades_skip_req_all_open_orders_in_async_task(monkeypatch):
    """
    回调线程安全回归:
    在异步任务上下文中应跳过 reqAllOpenOrders，避免 event-loop reentry 异常。
    """
    manual_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=903, action="BUY"),
        contract=SimpleNamespace(symbol="PSQ"),
        orderStatus=SimpleNamespace(remaining=1),
    )
    ib = DummyIBForAllOpenOrders(
        cash_usd=10000.0,
        open_trades=[manual_trade],
        all_open_trades=[manual_trade],
    )
    context = types.SimpleNamespace(ib_instance=ib)
    broker = IBBrokerAdapter(context=context)

    monkeypatch.setattr(IBBrokerAdapter, "_in_async_task", staticmethod(lambda: True))
    _ = broker.get_pending_orders()

    assert ib.req_all_calls == 0, "异步任务中不应触发 reqAllOpenOrders。"
