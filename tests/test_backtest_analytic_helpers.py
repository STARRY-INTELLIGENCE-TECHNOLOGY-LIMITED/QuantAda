"""解析解回测测试的共用脚本化策略与合成行情工具。"""

import backtrader as bt
import pandas as pd

import config
from backtest.backtester import Backtester
from strategies.base_strategy import BaseStrategy


def make_df(closes, opens=None, highs=None, lows=None, start="2024-01-01"):
    """按给定序列构造日线 OHLCV；未给出的 open/high/low 取 close 的合理包络。"""
    closes = [float(x) for x in closes]
    opens = [float(x) for x in (opens if opens is not None else closes)]
    highs = [float(x) for x in (highs if highs is not None else [max(o, c) * 1.5 for o, c in zip(opens, closes)])]
    lows = [float(x) for x in (lows if lows is not None else [min(o, c) * 0.5 for o, c in zip(opens, closes)])]
    idx = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000.0}, index=idx,
    )


class ScriptedStrategy(BaseStrategy):
    """按 K 线序号（从 1 开始）执行脚本：(op, 标的序号, 数值)，并记录每笔订单事件。"""

    script = {}
    events = []

    def init(self):
        pass

    def next(self):
        bar = len(self.broker.datas[0])
        for op, idx, value in self.script.get(bar, []):
            data = self.broker.datas[idx]
            if op == "tv":
                self.broker.order_target_value(data=data, target=value)
            elif op == "tp":
                self.broker.order_target_percent(data=data, target=value)
            elif op == "buy":
                self.broker.buy(data=data, size=value)
            elif op == "sell":
                self.broker.sell(data=data, size=value)

    def notify_order(self, order):
        raw = order._order
        executed_dt = bt.num2date(raw.executed.dt).date() if raw.executed.dt else None
        self.events.append({
            "status": order.getstatusname(),
            "symbol": order.data._name,
            "buy": order.is_buy(),
            "size": order.executed.size,
            "price": order.executed.price,
            "comm": order.executed.comm,
            "fill_date": executed_dt,
            "notify_bar": len(self.broker.datas[0]),
        })


def run_script(datas, script, *, cash=10000.0, commission=0.0, slippage=0.0, lot_size=1,
               execution_price="close", monkeypatch=None, strategy=ScriptedStrategy, **kwargs):
    """运行回测并返回 (engine, 已完成/其他状态事件列表)。每次使用独立子类，避免事件串扰。"""
    if monkeypatch is not None:
        monkeypatch.setattr(config, "LOT_SIZE", lot_size)
        monkeypatch.setattr(config, "PRINT_PLAN", False)
        monkeypatch.setattr(config, "LOG", False)
    cls = type("RunScript", (strategy,), {"script": script, "events": []})
    engine = Backtester(
        datas=datas, strategy_class=cls, cash=cash, commission=commission, slippage=slippage,
        execution_price=execution_price, enable_plot=False, verbose=False, **kwargs,
    )
    engine.run()
    return engine, cls.events


def completed(events):
    return [e for e in events if e["status"] == "Completed"]
