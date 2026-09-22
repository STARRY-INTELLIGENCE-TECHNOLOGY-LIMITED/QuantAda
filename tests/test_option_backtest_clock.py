import pandas as pd

from backtest.backtester import Backtester
from strategies.base_strategy import BaseStrategy
from common.data_view import visible_row


class _CountNextStrategy(BaseStrategy):
    def init(self):
        self.n = 0

    def next(self):
        self.n += 1


def _ohlcv(index, close=100.0):
    values = [float(close)] * len(index)
    return pd.DataFrame(
        {
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": [1.0] * len(index),
        },
        index=index,
    )


def _option_frame(index, close=2.0):
    frame = _ohlcv(index, close=close)
    frame["bid"] = close
    frame["ask"] = close + 0.05
    frame["delta"] = -0.15
    frame["contract_multiplier"] = 100.0
    frame["open_interest"] = 10.0
    return frame


def _run(datas, start="20240101", end="20240110"):
    engine = Backtester(
        datas=datas,
        strategy_class=_CountNextStrategy,
        start_date=start,
        end_date=end,
        cash=100000.0,
        commission=0.0,
        slippage=0.0,
        enable_plot=False,
        verbose=False,
    )
    engine.run()
    return engine


def test_short_lived_option_feed_does_not_stop_backtest_clock():
    clock = pd.bdate_range("2024-01-01", "2024-01-10")
    option = _option_frame(clock[:2])
    engine = _run({"US.SPY": _ohlcv(clock), "US.SPY240119P00400000": option})
    assert engine.results[0].strategy.n == len(clock)


def test_out_of_window_option_feed_is_skipped():
    clock = pd.bdate_range("2024-01-01", "2024-01-10")
    expired = _option_frame(pd.bdate_range("2022-01-03", "2022-01-07"))
    engine = _run(
        {
            "US.SPY": _ohlcv(clock),
            "US.SPY220107P00400000": expired,
        }
    )
    names = [getattr(data, "_name", "") for data in engine.cerebro.datas]
    assert "US.SPY" in names
    assert "US.SPY220107P00400000" not in names
    assert engine.results[0].strategy.n == len(clock)


def test_visible_row_ignores_zero_padded_option_bars():
    index = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    frame = _option_frame(index[:1])
    padded = frame.reindex(index)
    padded[["open", "high", "low", "close", "volume"]] = padded[["open", "high", "low", "close", "volume"]].fillna(0.0)
    data = type("Feed", (), {})()
    data.p = type("Params", (), {"dataname": padded})()
    row = visible_row(data, pd.Timestamp("2024-01-04"))
    assert float(row["close"]) == 2.0


def test_visible_row_strict_quote_uses_current_bid_ask_even_when_close_is_zero():
    index = pd.to_datetime(["2024-01-02", "2024-01-03"])
    frame = _option_frame(index)
    frame.loc[index[-1], "close"] = 0.0
    frame.loc[index[-1], "bid"] = 1.8
    frame.loc[index[-1], "ask"] = 1.9
    data = type("Feed", (), {})()
    data.p = type("Params", (), {"dataname": frame})()

    row = visible_row(data, pd.Timestamp("2024-01-03"), require_current_quote=True)
    assert float(row["bid"]) == 1.8


def test_visible_row_strict_quote_accepts_tz_aware_index():
    index = pd.date_range("2024-01-02", periods=2, freq="D", tz="UTC")
    frame = _option_frame(index)
    data = type("Feed", (), {})()
    data.p = type("Params", (), {"dataname": frame})()

    row = visible_row(data, pd.Timestamp("2024-01-03"), require_current_quote=True)

    assert row is not None
    assert float(row["close"]) == 2.0


def test_visible_row_strict_quote_rejects_stale_last_trade():
    index = pd.to_datetime(["2024-01-02", "2024-01-03"])
    frame = _option_frame(index[:1]).reindex(index)
    frame[["open", "high", "low", "close", "volume"]] = frame[
        ["open", "high", "low", "close", "volume"]
    ].fillna(0.0)
    data = type("Feed", (), {})()
    data.p = type("Params", (), {"dataname": frame})()

    assert visible_row(data, pd.Timestamp("2024-01-03"), require_current_quote=True) is None


def test_underlying_history_before_start_remains_on_dataname():
    history = pd.bdate_range("2023-12-01", "2024-01-10")
    option = _option_frame(pd.bdate_range("2024-01-08", "2024-01-10"))
    engine = _run(
        {"US.SPY": _ohlcv(history), "US.SPY240119P00400000": option},
        start="20240108",
        end="20240110",
    )
    spy = next(data for data in engine.cerebro.datas if getattr(data, "_name", "") == "US.SPY")
    assert len(spy.p.dataname) == len(history)


def test_missing_option_bars_stay_zero_until_expiry_then_zero():
    clock = pd.bdate_range("2024-01-02", "2024-01-12")
    hole = pd.Timestamp("2024-01-04")
    expiry = pd.Timestamp("2024-01-10")
    option_index = pd.DatetimeIndex([day for day in clock if day != hole and day <= expiry])
    engine = _run(
        {
            "US.SPY": _ohlcv(clock),
            "US.SPY240110P00400000": _option_frame(option_index, close=2.0),
        },
        start="20240102",
        end="20240112",
    )
    option = next(
        data for data in engine.cerebro.datas
        if getattr(data, "_name", "") == "US.SPY240110P00400000"
    )
    frame = option.p.dataname
    assert float(frame.loc[hole, "close"]) == 0.0
    after = pd.Timestamp("2024-01-12")
    if after in frame.index:
        assert float(frame.loc[after, "close"]) == 0.0
    data = type("Feed", (), {})()
    data.p = type("Params", (), {"dataname": frame})()
    assert visible_row(data, hole, require_current_quote=True) is None

