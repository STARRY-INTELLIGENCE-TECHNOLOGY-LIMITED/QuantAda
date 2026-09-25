import pandas as pd

from backtest.backtester import Backtester
from strategies.base_strategy import BaseStrategy
from common.data_view import visible_row
import strategies.options.support as option_support
from strategies.options.support import iter_option_rows


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


def test_visible_row_cache_keeps_intraday_rows_distinct():
    index = pd.to_datetime(["2024-01-03 10:00", "2024-01-03 11:00"])
    frame = _option_frame(index)
    frame.loc[index[0], "close"] = 1.0
    frame.loc[index[1], "close"] = 2.0
    data = type("Feed", (), {})()
    data.p = type("Params", (), {"dataname": frame})()
    owner = type("Broker", (), {"is_live": False})()

    first = visible_row(data, pd.Timestamp("2024-01-03 10:30"), cache_owner=owner)
    second = visible_row(data, pd.Timestamp("2024-01-03 11:30"), cache_owner=owner)

    assert float(first["close"]) == 1.0
    assert float(second["close"]) == 2.0


def test_option_row_cache_reuses_only_the_same_offline_bar():
    index = pd.to_datetime(["2024-01-03", "2024-01-04"])
    frame = _option_frame(index)
    data = type("Feed", (), {"_name": "US.SPY240119P00400000"})()
    data.p = type("Params", (), {"dataname": frame})()
    broker = type("Broker", (), {"is_live": False, "datas": [data]})()

    first = list(iter_option_rows(broker, index[0], {"PUT"}))
    second = list(iter_option_rows(broker, index[0], {"PUT"}))
    later = list(iter_option_rows(broker, index[1], {"PUT"}))

    assert len(first) == len(second) == len(later) == 1
    assert first[0][2] == second[0][2]
    assert first[0][1] is second[0][1]


def test_live_option_rows_refresh_quotes_at_the_same_timestamp():
    index = pd.to_datetime(["2024-01-03"])
    frame = _option_frame(index)
    data = type("Feed", (), {"_name": "US.SPY240119P00400000"})()
    data.p = type("Params", (), {"dataname": frame})()
    broker = type("Broker", (), {"is_live": True, "datas": [data]})()

    first = list(iter_option_rows(broker, index[0], {"PUT"}))
    replacement = frame.copy()
    replacement.loc[index[0], ["close", "bid", "ask"]] = [3.0, 3.0, 3.1]
    data.p.dataname = replacement
    second = list(iter_option_rows(broker, index[0], {"PUT"}))

    assert float(first[0][1]["close"]) == 2.0
    assert float(second[0][1]["close"]) == 3.0
    assert not hasattr(broker, "_visible_row_cache")
    assert not hasattr(broker, "_option_rows_cache")
    assert not hasattr(broker, "_option_snapshot_datas_by_day")


def test_option_snapshot_index_skips_feeds_without_a_valid_quote(monkeypatch):
    index = pd.to_datetime(["2024-01-03", "2024-01-04"])
    active = _option_frame(index)
    inactive = active.copy()
    inactive["close"] = 0.0
    inactive["bid"] = 0.0
    inactive["ask"] = 0.0

    def feed(symbol, frame):
        data = type("Feed", (), {"_name": symbol})()
        data.p = type("Params", (), {"dataname": frame})()
        return data

    active_data = feed("US.SPY240119P00400000", active)
    inactive_data = feed("US.SPY240119P00410000", inactive)
    broker = type(
        "Broker",
        (),
        {"is_live": False, "datas": [active_data, inactive_data]},
    )()
    calls = {"count": 0}
    original = option_support.visible_row

    def counted(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(option_support, "visible_row", counted)
    rows = list(option_support.iter_option_rows(broker, index[0], {"PUT"}))

    assert len(rows) == 1
    assert rows[0][0] is active_data
    assert calls["count"] == 1


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


def test_option_feed_alignment_is_reused_when_clock_signature_matches():
    clock = pd.bdate_range("2024-01-02", "2024-01-10")
    datas = {
        "US.SPY": _ohlcv(clock),
        "US.SPY240119P00400000": _option_frame(clock[[0, 2, 4]]),
    }
    first = _run(datas)
    prepared = first._prepared_datas
    assert prepared is not None
    option = prepared["US.SPY240119P00400000"]
    assert option.index.equals(clock)
    assert "_quantada_aligned_clock" in option.attrs

    second = _run(prepared)
    second_option = next(
        data for data in second.cerebro.datas
        if getattr(data, "_name", "") == "US.SPY240119P00400000"
    )
    assert second_option.p.dataname.equals(option)


def test_stock_feed_preparation_reuse_keeps_the_same_clock_and_rows():
    clock = pd.bdate_range("2024-01-02", "2024-01-10")
    datas = {"US.SPY": _ohlcv(clock), "US.QQQ": _ohlcv(clock, close=200.0)}

    first = _run(datas)
    second = _run(first._prepared_datas)

    assert first.results[0].strategy.n == second.results[0].strategy.n == len(clock)
    assert second.cerebro.datas[0].p.dataname.equals(
        first._prepared_datas["US.SPY"]
    )

