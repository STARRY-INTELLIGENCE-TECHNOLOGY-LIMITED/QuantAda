"""IBKR 历史行情的日内标签与纯日期终点（使用替身，不连接 TWS/Gateway）。"""

from types import SimpleNamespace

import pandas as pd

import data_providers.ibkr_provider as ibkr_module
from common.timeframe import aggregate_daily_cutoff
from datetime import time


def _provider(monkeypatch, bars_frame, calls):
    provider = object.__new__(ibkr_module.IbkrDataProvider)
    contract = SimpleNamespace(secType="STK", symbol="AAPL")
    monkeypatch.setattr(provider, "_connect", lambda: True, raising=False)
    monkeypatch.setattr(provider, "_parse_contract", lambda symbol: contract, raising=False)

    def call_ib(method_name, *args, **kwargs):
        calls.append((method_name, kwargs))
        if method_name == "reqContractDetails":
            return [SimpleNamespace(contract=contract)]
        return ["bar"]

    monkeypatch.setattr(provider, "_call_ib", call_ib, raising=False)
    monkeypatch.setattr(ibkr_module, "util", SimpleNamespace(df=lambda bars: bars_frame.copy()))
    return provider


def _minute_bars(day, starts):
    index = [pd.Timestamp(f"{day} {value}") for value in starts]
    return pd.DataFrame({
        "date": index,
        "open": [1.0 + i for i in range(len(index))],
        "high": [1.5 + i for i in range(len(index))],
        "low": [0.5 + i for i in range(len(index))],
        "close": [1.2 + i for i in range(len(index))],
        "volume": [100.0] * len(index),
    })


def test_intraday_bars_are_relabelled_to_bar_end(monkeypatch):
    calls = []
    bars = _minute_bars("2024-01-05", ["15:50:00", "15:55:00"])
    provider = _provider(monkeypatch, bars, calls)

    result = provider.get_data("AAPL", "2024-01-05", "2024-01-05 16:00:00", "Minutes", 5)

    assert list(result.index) == [pd.Timestamp("2024-01-05 15:55"), pd.Timestamp("2024-01-05 16:00")]
    # 15:55 截止只能包含 15:50-15:55 已结束的 K 线，不能带入 15:55-16:00。
    daily = aggregate_daily_cutoff(result, time(15, 55))
    assert daily["close"].iloc[0] == 1.2


def test_date_only_intraday_end_keeps_whole_end_day(monkeypatch):
    calls = []
    bars = _minute_bars("2024-01-05", ["09:30:00", "15:59:00"])
    provider = _provider(monkeypatch, bars, calls)

    result = provider.get_data("AAPL", "2024-01-05", "2024-01-05", "Minutes", 1)

    assert len(result) == 2
    assert result.index.max() == pd.Timestamp("2024-01-05 16:00")
    history = [kwargs for name, kwargs in calls if name == "reqHistoricalData"]
    assert history[0]["endDateTime"] == "20240105 23:59:59"


def test_daily_bars_keep_date_labels(monkeypatch):
    calls = []
    bars = pd.DataFrame({
        "date": [pd.Timestamp("2024-01-04"), pd.Timestamp("2024-01-05")],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [1.0, 2.0],
    })
    provider = _provider(monkeypatch, bars, calls)

    result = provider.get_data("AAPL", "2024-01-04", "2024-01-05", "Days", 1)

    assert list(result.index) == [pd.Timestamp("2024-01-04"), pd.Timestamp("2024-01-05")]
