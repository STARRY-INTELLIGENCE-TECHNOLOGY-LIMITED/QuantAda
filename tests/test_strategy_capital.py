from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from strategies.base_strategy import BaseStrategy


class DummyStrategy(BaseStrategy):
    def init(self):
        pass

    def next(self):
        pass


class DummyData:
    def __init__(self, name, index=None):
        self._name = name
        if index is None:
            index = [
                datetime(2026, 4, 20, 14, 45, 0),
                datetime(2026, 4, 21, 14, 45, 0),
            ]
        self.p = SimpleNamespace()
        self.p.dataname = pd.DataFrame(
            {"close": [100.0] * len(index)},
            index=index,
        )


class DummyBroker:
    def __init__(self, cash=0.0, rebalance_cash=None, datas=None):
        self.is_live = True
        self.datas = datas if datas is not None else [DummyData("AAPL.SMART")]
        self._cash = cash
        self._rebalance_cash = rebalance_cash

    def log(self, txt, dt=None):
        return None

    def get_pending_orders(self):
        return []

    def getposition(self, data):
        return SimpleNamespace(size=3.0, price=0.0)

    def get_current_price(self, data):
        return 100.0

    def get_cash(self):
        return self._cash

    def get_rebalance_cash(self):
        if self._rebalance_cash is None:
            raise RuntimeError("rebalance cash unavailable")
        return self._rebalance_cash


def test_strategy_isolated_capital_prefers_rebalance_cash(monkeypatch):
    """
    资金口径回归:
    策略分配资金应优先使用 broker.get_rebalance_cash，避免与下单资金语义撕裂。
    """
    broker = DummyBroker(cash=1000.0, rebalance_cash=200.0)
    strategy = DummyStrategy(broker=broker, params={})

    allocatable, current_positions = strategy.get_strategy_isolated_capital()

    # managed_market_value = 3 * 100 = 300
    assert allocatable == pytest.approx(500.0), "策略分配资金应采用 rebalance_cash + managed_value。"
    assert len(current_positions) == 1, "应识别到 1 个受管持仓。"


def test_strategy_isolated_capital_fallbacks_to_get_cash_on_rebalance_error(monkeypatch):
    """
    稳定性回归:
    get_rebalance_cash 异常时，策略层应回退 get_cash，不能中断调仓流程。
    """
    broker = DummyBroker(cash=1000.0, rebalance_cash=None)
    strategy = DummyStrategy(broker=broker, params={})

    allocatable, _ = strategy.get_strategy_isolated_capital()

    # fallback: get_cash + managed_market_value = 1000 + 300
    assert allocatable == pytest.approx(1300.0), "rebalance 口径异常时应回退到 get_cash 口径。"


def test_strategy_isolated_capital_backtest_ignores_pending_orders(monkeypatch):
    """
    回测路径回归:
    回测假定订单按计划执行，不应读取实盘 pending 订单修正仓位。
    """
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0)
    broker.is_live = False
    pending_calls = []

    def _raise_if_called():
        pending_calls.append(True)
        raise AssertionError("回测路径不应查询实盘在途订单。")

    broker.get_pending_orders = _raise_if_called
    strategy = DummyStrategy(broker=broker, params={})

    allocatable, current_positions = strategy.get_strategy_isolated_capital()

    assert pending_calls == []
    assert allocatable == pytest.approx(1300.0)
    assert len(current_positions) == 1


def test_notify_order_prefers_execution_dt_for_logs(monkeypatch):
    """
    成交日志时间回归:
    notify_order 应优先使用订单真实执行时间，而不是 broker 当前触发时间。
    """
    class RecordingBroker(DummyBroker):
        def __init__(self):
            super().__init__(cash=0.0, rebalance_cash=0.0)
            self.log_calls = []

        def log(self, txt, dt=None):
            self.log_calls.append({"txt": txt, "dt": dt})

    class DummyOrder:
        def __init__(self):
            self.executed = SimpleNamespace(
                size=100.0,
                price=10.0,
                value=1000.0,
                comm=0.0,
                dt=datetime(2026, 4, 8, 14, 45, 33),
            )

        def is_completed(self):
            return True

        def is_buy(self):
            return False

        def is_sell(self):
            return True

        def is_rejected(self):
            return False

    broker = RecordingBroker()
    strategy = DummyStrategy(broker=broker, params={})

    strategy.notify_order(DummyOrder())

    assert len(broker.log_calls) == 1, "成交日志应被记录一次。"
    assert broker.log_calls[0]["dt"].isoformat() == "2026-04-08T14:45:33", "成交日志应使用真实执行时间。"


def test_should_execute_rebalance_defaults_to_every_bar(monkeypatch):
    """
    兼容性回归:
    未配置 rebalance_when 时，execute_rebalance 仍应保持每个 bar 可执行。
    """
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0)
    strategy = DummyStrategy(broker=broker, params={})

    assert strategy.should_execute_rebalance(target_symbols=broker.datas), "默认行为应保持每个 bar 允许调仓。"


def test_should_execute_rebalance_weekly_blocks_same_week(monkeypatch):
    """
    调仓时点回归:
    weekly 模式下，同一自然周内的后续 bar 不应再次触发等权调仓。
    """
    data = DummyData(
        "AAPL.SMART",
        index=[
            datetime(2026, 4, 20, 14, 45, 0),  # Monday
            datetime(2026, 4, 21, 14, 45, 0),  # Tuesday
        ],
    )
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={"rebalance_when": "weekly"})

    assert not strategy.should_execute_rebalance(target_symbols=[data]), "同一周内不应重复触发 weekly 调仓。"


def test_should_execute_rebalance_weekly_allows_new_week(monkeypatch):
    """
    调仓时点回归:
    weekly 模式下，跨周后的首个 bar 应允许执行调仓。
    """
    data = DummyData(
        "AAPL.SMART",
        index=[
            datetime(2026, 4, 24, 14, 45, 0),  # Friday
            datetime(2026, 4, 27, 14, 45, 0),  # Next Monday
        ],
    )
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={"rebalance_when": "weekly"})

    assert strategy.should_execute_rebalance(target_symbols=[data]), "跨周首个 bar 应允许 weekly 调仓。"


def test_should_execute_rebalance_monthly_allows_new_month(monkeypatch):
    """
    调仓时点回归:
    monthly 模式下，跨月后的首个 bar 应允许执行调仓。
    """
    data = DummyData(
        "AAPL.SMART",
        index=[
            datetime(2026, 4, 30, 14, 45, 0),
            datetime(2026, 5, 4, 14, 45, 0),
        ],
    )
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={"rebalance_when": "monthly"})

    assert strategy.should_execute_rebalance(target_symbols=[data]), "跨月首个 bar 应允许 monthly 调仓。"


def test_execute_rebalance_skips_plan_when_weekly_gate_not_due(monkeypatch):
    """
    调仓主流程回归:
    非调仓周内不应生成计划，也不应进入执行器。
    """
    import common.rebalancer as rebalancer_module
    import common.order_executor as order_executor_module

    calculate_calls = []
    execute_calls = []

    def fake_calculate_plan(**kwargs):
        calculate_calls.append(kwargs)
        return {"sell_clear": [], "reduce": [], "increase": [], "target_per_stock": 0.0}

    class DummyExecutor:
        def __init__(self, broker):
            self.broker = broker

        def execute_plan(self, plan):
            execute_calls.append(plan)

    monkeypatch.setattr(
        rebalancer_module.PortfolioRebalancer,
        "calculate_plan",
        staticmethod(fake_calculate_plan),
    )
    monkeypatch.setattr(order_executor_module, "OrderExecutor", DummyExecutor)

    data = DummyData(
        "AAPL.SMART",
        index=[
            datetime(2026, 4, 20, 14, 45, 0),
            datetime(2026, 4, 21, 14, 45, 0),
        ],
    )
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={"rebalance_when": "weekly"})

    strategy.execute_rebalance(target_symbols=[data], top_k=1, rebalance_threshold=0.2)

    assert calculate_calls == [], "非调仓周内不应生成调仓计划。"
    assert execute_calls == [], "非调仓周内不应进入执行器。"


def test_execute_rebalance_runs_plan_when_weekly_gate_due(monkeypatch):
    """
    调仓主流程回归:
    到达新交易周时，应正常生成计划并交给执行器。
    """
    import common.rebalancer as rebalancer_module
    import common.order_executor as order_executor_module

    calculate_calls = []
    execute_calls = []

    def fake_calculate_plan(**kwargs):
        calculate_calls.append(kwargs)
        return {"sell_clear": [], "reduce": [], "increase": [], "target_per_stock": 0.0}

    class DummyExecutor:
        def __init__(self, broker):
            self.broker = broker

        def execute_plan(self, plan):
            execute_calls.append(plan)

    monkeypatch.setattr(
        rebalancer_module.PortfolioRebalancer,
        "calculate_plan",
        staticmethod(fake_calculate_plan),
    )
    monkeypatch.setattr(order_executor_module, "OrderExecutor", DummyExecutor)

    data = DummyData(
        "AAPL.SMART",
        index=[
            datetime(2026, 4, 24, 14, 45, 0),
            datetime(2026, 4, 27, 14, 45, 0),
        ],
    )
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={"rebalance_when": "weekly"})

    strategy.execute_rebalance(target_symbols=[data], top_k=1, rebalance_threshold=0.2)

    assert len(calculate_calls) == 1, "新交易周应正常生成调仓计划。"
    assert len(execute_calls) == 1, "新交易周应将计划交给执行器。"


def test_should_execute_rebalance_respects_rebalance_when_skip(monkeypatch):
    """
    显式调仓信号回归:
    rebalance_when='skip' 时，应跳过本次正式调仓，不受默认频率影响。
    """
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0)
    strategy = DummyStrategy(broker=broker, params={})

    assert not strategy.should_execute_rebalance(
        target_symbols=broker.datas,
        rebalance_when='skip',
    ), "显式 rebalance_when='skip' 时应跳过调仓。"


def test_should_execute_rebalance_respects_rebalance_when_next(monkeypatch):
    """
    显式调仓信号回归:
    rebalance_when='next' 时，应允许本次正式调仓，不受频率门控限制。
    """
    data = DummyData(
        "AAPL.SMART",
        index=[
            datetime(2026, 4, 20, 14, 45, 0),
            datetime(2026, 4, 21, 14, 45, 0),
        ],
    )
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={"rebalance_when": "weekly"})

    assert strategy.should_execute_rebalance(
        target_symbols=[data],
        rebalance_when='next',
    ), "显式 rebalance_when='next' 时应允许本次正式调仓。"


def test_execute_rebalance_skips_plan_when_rebalance_when_skip(monkeypatch):
    """
    显式调仓主流程回归:
    rebalance_when='skip' 时，即使默认频率是 bar，也不应生成计划。
    """
    import common.rebalancer as rebalancer_module
    import common.order_executor as order_executor_module

    calculate_calls = []
    execute_calls = []

    def fake_calculate_plan(**kwargs):
        calculate_calls.append(kwargs)
        return {"sell_clear": [], "reduce": [], "increase": [], "target_per_stock": 0.0}

    class DummyExecutor:
        def __init__(self, broker):
            self.broker = broker

        def execute_plan(self, plan):
            execute_calls.append(plan)

    monkeypatch.setattr(
        rebalancer_module.PortfolioRebalancer,
        "calculate_plan",
        staticmethod(fake_calculate_plan),
    )
    monkeypatch.setattr(order_executor_module, "OrderExecutor", DummyExecutor)

    data = DummyData("AAPL.SMART")
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={})

    strategy.execute_rebalance(
        target_symbols=[data],
        top_k=1,
        rebalance_threshold=0.2,
        rebalance_when='skip',
    )

    assert calculate_calls == [], "显式 rebalance_when='skip' 时不应生成调仓计划。"
    assert execute_calls == [], "显式 rebalance_when='skip' 时不应进入执行器。"


def test_execute_rebalance_runs_plan_when_rebalance_when_next(monkeypatch):
    """
    显式调仓主流程回归:
    rebalance_when='next' 时，应按本次正式调仓生成计划并执行。
    """
    import common.rebalancer as rebalancer_module
    import common.order_executor as order_executor_module

    calculate_calls = []
    execute_calls = []

    def fake_calculate_plan(**kwargs):
        calculate_calls.append(kwargs)
        return {"sell_clear": [], "reduce": [], "increase": [], "target_per_stock": 0.0}

    class DummyExecutor:
        def __init__(self, broker):
            self.broker = broker

        def execute_plan(self, plan):
            execute_calls.append(plan)

    monkeypatch.setattr(
        rebalancer_module.PortfolioRebalancer,
        "calculate_plan",
        staticmethod(fake_calculate_plan),
    )
    monkeypatch.setattr(order_executor_module, "OrderExecutor", DummyExecutor)

    data = DummyData(
        "AAPL.SMART",
        index=[
            datetime(2026, 4, 20, 14, 45, 0),
            datetime(2026, 4, 21, 14, 45, 0),
        ],
    )
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={"rebalance_when": "weekly"})

    strategy.execute_rebalance(
        target_symbols=[data],
        top_k=1,
        rebalance_threshold=0.2,
        rebalance_when='next',
    )

    assert len(calculate_calls) == 1, "显式 rebalance_when='next' 时应生成调仓计划。"
    assert len(execute_calls) == 1, "显式 rebalance_when='next' 时应进入执行器。"


def test_should_execute_rebalance_rejects_invalid_rebalance_when(monkeypatch):
    """
    参数校验回归:
    非法 rebalance_when 应直接报错，避免静默回退为 bar。
    """
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0)
    strategy = DummyStrategy(broker=broker, params={})

    with pytest.raises(ValueError, match="Invalid rebalance_when"):
        strategy.should_execute_rebalance(
            target_symbols=broker.datas,
            rebalance_when='every_bar',
        )


def test_execute_rebalance_skips_unknown_targets_and_pushes_warning(monkeypatch):
    import common.rebalancer as rebalancer_module
    import common.order_executor as order_executor_module

    pushed = []
    calculate_calls = []

    class DummyAlarmManager:
        def push_text(self, content, level="INFO"):
            pushed.append({"content": content, "level": level})

    def fake_calculate_plan(**kwargs):
        calculate_calls.append(kwargs)
        return {"sell_clear": [], "reduce": [], "increase": [], "target_per_stock": 0.0}

    class DummyExecutor:
        def __init__(self, broker):
            self.broker = broker

        def execute_plan(self, plan):
            return None

    monkeypatch.setattr(rebalancer_module.PortfolioRebalancer, "calculate_plan", staticmethod(fake_calculate_plan))
    monkeypatch.setattr(order_executor_module, "OrderExecutor", DummyExecutor)
    monkeypatch.setattr("strategies.base_strategy.AlarmManager", lambda: DummyAlarmManager())

    data = DummyData("AAPL.SMART")
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[data])
    strategy = DummyStrategy(broker=broker, params={})

    strategy.execute_rebalance(
        target_symbols=[SimpleNamespace(_name="MSFT.SMART"), data],
        top_k=1,
        rebalance_threshold=0.2,
    )

    assert len(calculate_calls) == 1
    assert calculate_calls[0]["target_symbols"] == [data], "未知目标应在进入 rebalancer 前被跳过。"
    assert len(pushed) == 1
    assert pushed[0]["level"] == "WARNING"
    assert "unknown_targets" in pushed[0]["content"]


def test_execute_rebalance_resolves_same_symbol_target_to_managed_data(monkeypatch):
    import common.rebalancer as rebalancer_module
    import common.order_executor as order_executor_module

    calculate_calls = []

    def fake_calculate_plan(**kwargs):
        calculate_calls.append(kwargs)
        return {"sell_clear": [], "reduce": [], "increase": [], "target_per_stock": 0.0}

    class DummyExecutor:
        def __init__(self, broker):
            self.broker = broker

        def execute_plan(self, plan):
            return None

    monkeypatch.setattr(rebalancer_module.PortfolioRebalancer, "calculate_plan", staticmethod(fake_calculate_plan))
    monkeypatch.setattr(order_executor_module, "OrderExecutor", DummyExecutor)

    managed = DummyData("AAPL.SMART")
    broker = DummyBroker(cash=1000.0, rebalance_cash=1000.0, datas=[managed])
    strategy = DummyStrategy(broker=broker, params={})
    alias_target = DummyData("AAPL")

    strategy.execute_rebalance(
        target_symbols=[alias_target],
        top_k=1,
        rebalance_threshold=0.2,
    )

    assert len(calculate_calls) == 1
    assert calculate_calls[0]["target_symbols"] == [managed]
