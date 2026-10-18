"""期权策略使用的确定性事件快照工具。"""

from __future__ import annotations

import pandas as pd

from common.options.analytics import parse_option_symbol, underlying_key


def _event_date(value):
    """把事件日期标准化为无时区交易日。"""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    stamp = pd.Timestamp(parsed)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.normalize()


def _symbol_key(value):
    """按底层代码生成跨 Provider 可比较的键。"""
    raw = str(value or "").strip().upper()
    parsed = parse_option_symbol(raw)
    if parsed.get("underlying"):
        raw = str(parsed["underlying"]).strip().upper()
    return underlying_key(raw)


def normalize_earnings_calendar(events, symbols=None, start_date=None, end_date=None):
    """统一 Provider 财报日字段并按底层代码和日期去重。"""
    if events is None:
        return None
    if not isinstance(events, pd.DataFrame) or events.empty:
        return pd.DataFrame(
            columns=["symbol", "event_date", "event_timestamp", "event_type", "period_text"]
        )

    frame = events.copy()
    columns = {str(column).strip().lower(): column for column in frame.columns}

    def column(*names):
        for name in names:
            selected = columns.get(name.lower())
            if selected is not None:
                return frame[selected]
        return pd.Series([None] * len(frame), index=frame.index)

    raw_symbols = column("symbol", "security", "code", "stock_code", "underlying")
    raw_dates = column("event_date", "earnings_date", "date", "report_date")
    raw_timestamps = column("event_timestamp", "earnings_timestamp", "timestamp")
    raw_periods = column("period_text", "period", "report_period")
    normalized = pd.DataFrame(
        {
            "symbol": raw_symbols.map(lambda value: str(value or "").strip().upper()),
            "event_date": raw_dates.map(_event_date),
            "event_timestamp": pd.to_datetime(raw_timestamps, errors="coerce"),
            "event_type": "earnings",
            "period_text": raw_periods.astype("string"),
        }
    )
    normalized["underlying_key"] = normalized["symbol"].map(_symbol_key)
    normalized = normalized[normalized["event_date"].notna()]

    if symbols:
        allowed = {_symbol_key(item) for item in symbols if _symbol_key(item)}
        normalized = normalized[normalized["underlying_key"].isin(allowed)]
    start = _event_date(start_date) if start_date else None
    end = _event_date(end_date) if end_date else None
    if start is not None and not pd.isna(start):
        normalized = normalized[normalized["event_date"] >= start]
    if end is not None and not pd.isna(end):
        normalized = normalized[normalized["event_date"] <= end]

    normalized = normalized.drop_duplicates(
        subset=["underlying_key", "event_date", "event_type"], keep="last"
    )
    normalized = normalized.sort_values(["event_date", "underlying_key"]).reset_index(drop=True)
    return normalized[["symbol", "underlying_key", "event_date", "event_timestamp", "event_type", "period_text"]]


def earnings_events_by_underlying(events):
    """把统一事件表转成可写入行情 attrs 的轻量字典。"""
    normalized = normalize_earnings_calendar(events)
    result = {}
    if normalized is None or normalized.empty:
        return result
    for key, group in normalized.groupby("underlying_key", sort=False):
        rows = []
        for row in group.itertuples(index=False):
            date = _event_date(row.event_date)
            if pd.isna(date):
                continue
            period = row.period_text
            try:
                if pd.isna(period):
                    period = ""
            except (TypeError, ValueError):
                period = ""
            rows.append(
                {
                    "symbol": str(row.symbol or ""),
                    "event_date": date.strftime("%Y-%m-%d"),
                    "event_type": "earnings",
                    "period_text": str(period or ""),
                }
            )
        if rows:
            result[str(key)] = tuple(rows)
    return result


def attach_earnings_calendar(datas, events):
    """把事件快照附加到行情 DataFrame，不改变 OHLCV 列和数据内容。"""
    by_underlying = earnings_events_by_underlying(events)
    as_of_supported = bool(getattr(events, "attrs", {}).get("as_of_supported", True)) if events is not None else False
    source = str(getattr(events, "attrs", {}).get("source", "data_source") or "data_source") if events is not None else "data_source"
    for symbol, dataframe in (datas or {}).items():
        if not isinstance(dataframe, pd.DataFrame):
            continue
        key = _symbol_key(symbol)
        attrs = dict(getattr(dataframe, "attrs", {}) or {})
        attrs["earnings_calendar"] = tuple(by_underlying.get(key, ()))
        attrs["earnings_calendar_available"] = events is not None
        attrs["earnings_calendar_source"] = source
        attrs["earnings_calendar_as_of_supported"] = as_of_supported
        dataframe.attrs = attrs
    return datas


def earnings_blackout(event_rows, current_dt, before_days=7, after_days=2):
    """判断当前交易日是否处于财报前后禁开仓窗口。"""
    current = _event_date(current_dt)
    if current is None or pd.isna(current):
        return False
    try:
        before = max(0, int(before_days))
        after = max(0, int(after_days))
    except (TypeError, ValueError, OverflowError):
        before, after = 7, 2
    for row in event_rows or ():
        value = row.get("event_date") if isinstance(row, dict) else row
        event_day = _event_date(value)
        if pd.isna(event_day):
            continue
        if event_day - pd.Timedelta(days=before) <= current <= event_day + pd.Timedelta(days=after):
            return True
    return False


__all__ = [
    "attach_earnings_calendar",
    "earnings_blackout",
    "earnings_events_by_underlying",
    "normalize_earnings_calendar",
]
