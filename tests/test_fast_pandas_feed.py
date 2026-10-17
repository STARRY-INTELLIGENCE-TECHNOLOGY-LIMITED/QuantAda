import numpy as np
import pandas as pd
import backtrader as bt

from backtest.backtester import Backtester
from backtest.fast_feed import FastPandasData
from strategies.base_strategy import BaseStrategy


class _CountNextStrategy(BaseStrategy):
    def init(self):
        self.n = 0
        self.first_close = None

    def next(self):
        self.n += 1
        if self.first_close is None:
            self.first_close = float(self.broker.datas[0].close[0])


class _Hold(bt.Strategy):
    def next(self):
        return


def _frame(index, close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.arange(1, len(index) + 1, dtype=float),
            "open_interest": np.full(len(index), 9.0),
        },
        index=index,
    )


def _loaded_lines(feed_cls, frame, start, end):
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(
        feed_cls(
            dataname=frame,
            fromdate=pd.Timestamp(start),
            todate=pd.Timestamp(end),
            name="US.SPY",
        )
    )
    cerebro.addstrategy(_Hold)
    cerebro.run()
    data = cerebro.datas[0]
    values = {}
    for alias in data.getlinealiases():
        values[alias] = [float(item) for item in getattr(data.lines, alias).array]
    return values


def test_fast_feed_matches_pandas_data_window_and_values():
    index = pd.bdate_range("2024-01-02", periods=8)
    frame = _frame(index, [10, 11, 12, 13, 14, 15, 16, 17])
    start, end = "2024-01-03", "2024-01-09"
    reference = _loaded_lines(bt.feeds.PandasData, frame, start, end)
    fast = _loaded_lines(FastPandasData, frame, start, end)

    assert fast["datetime"] == reference["datetime"]
    assert fast["open"] == reference["open"]
    assert fast["high"] == reference["high"]
    assert fast["low"] == reference["low"]
    assert fast["close"] == reference["close"]
    assert fast["volume"] == reference["volume"]
    assert all(np.isnan(value) for value in fast["openinterest"])
    assert all(np.isnan(value) for value in reference["openinterest"])
    assert fast["close"][0] == 11.0
    assert fast["close"][-1] == 15.0
    assert len(fast["close"]) == 5


def test_fast_feed_keeps_dataframe_and_backtest_clock():
    index = pd.bdate_range("2024-01-02", periods=6)
    datas = {"US.SPY": _frame(index, [100, 101, 102, 103, 104, 105])}
    engine = Backtester(
        datas=datas,
        strategy_class=_CountNextStrategy,
        start_date="20240103",
        end_date="20240108",
        cash=100000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    engine.run()

    feed = engine.cerebro.datas[0]
    assert isinstance(feed, FastPandasData)
    assert isinstance(feed.p.dataname, pd.DataFrame)
    assert list(feed.p.dataname.columns) == list(datas["US.SPY"].columns)
    assert engine.results[0].strategy.n == 4
    assert engine.results[0].strategy.first_close == 101.0


def test_fast_feed_bulk_path_is_used(monkeypatch):
    def fail_slow(self):
        raise AssertionError("slow pandas preload")

    monkeypatch.setattr(bt.feeds.PandasData, "preload", fail_slow)
    index = pd.bdate_range("2024-01-02", periods=4)
    values = _loaded_lines(FastPandasData, _frame(index, [1, 2, 3, 4]), "2024-01-02", "2024-01-04")
    assert values["close"] == [1.0, 2.0, 3.0]


def test_fast_feed_falls_back_for_non_monotonic_index():
    index = pd.to_datetime(["2024-01-04", "2024-01-02", "2024-01-03"])
    frame = _frame(index, [1, 2, 3])
    reference = _loaded_lines(bt.feeds.PandasData, frame, "2024-01-02", "2024-01-04")
    fast = _loaded_lines(FastPandasData, frame, "2024-01-02", "2024-01-04")
    assert fast["close"] == reference["close"]
    assert fast["datetime"] == reference["datetime"]


def test_fast_feed_matches_intraday_timestamps():
    index = pd.date_range("2024-01-02 09:30", periods=5, freq="15min")
    frame = _frame(index, [1.5, 1.6, 1.7, 1.8, 1.9])
    start, end = "2024-01-02 09:45", "2024-01-02 10:15"
    reference = _loaded_lines(bt.feeds.PandasData, frame, start, end)
    fast = _loaded_lines(FastPandasData, frame, start, end)
    assert fast["datetime"] == reference["datetime"]
    assert fast["close"] == reference["close"] == [1.6, 1.7, 1.8]


def test_datetime_nums_follow_index_resolution():
    from backtest.fast_feed import _datetime_nums

    daily = pd.bdate_range("2024-01-02", periods=3)
    intraday = pd.date_range("2024-01-02 09:30", periods=3, freq="15min")
    indexes = [daily, intraday]
    for unit in ("s", "ms", "us", "ns"):
        indexes.append(daily.as_unit(unit))
        indexes.append(intraday.as_unit(unit))
    for index in indexes:
        got = _datetime_nums(index)
        expected = [bt.date2num(ts.to_pydatetime()) for ts in index]
        assert got is not None
        assert got.tolist() == expected


def test_fast_feed_matches_microsecond_window():
    index = pd.bdate_range("2024-01-02", periods=8).as_unit("us")
    frame = _frame(index, [10, 11, 12, 13, 14, 15, 16, 17])
    start, end = "2024-01-03", "2024-01-09"
    reference = _loaded_lines(bt.feeds.PandasData, frame, start, end)
    fast = _loaded_lines(FastPandasData, frame, start, end)
    assert fast["datetime"] == reference["datetime"]
    assert fast["close"] == reference["close"] == [11.0, 12.0, 13.0, 14.0, 15.0]
