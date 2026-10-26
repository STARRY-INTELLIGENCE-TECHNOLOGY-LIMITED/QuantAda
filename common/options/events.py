"""期权策略使用的确定性事件快照工具。"""

from __future__ import annotations

import math

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
            columns=["symbol", "event_date", "event_timestamp", "event_type", "event_role", "period_text", "visible_from", "source"]
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
    raw_event_type = column("event_type", "event_kind", "kind")
    raw_event_role = column("event_role", "date_role", "timing", "earnings_role")
    raw_visible = column("visible_from", "snapshot_date", "as_of", "available_date")
    raw_source = column("source", "provider", "source_id")
    normalized = pd.DataFrame(
        {
            "symbol": raw_symbols.map(lambda value: str(value or "").strip().upper()),
            "event_date": raw_dates.map(_event_date),
            "event_timestamp": pd.to_datetime(raw_timestamps, errors="coerce"),
            "event_type": raw_event_type.map(
                lambda value: str(value or "").strip().lower() or "earnings"
            ),
            "event_role": raw_event_role.map(_normalise_event_role),
            "period_text": raw_periods.astype("string"),
            "visible_from": raw_visible.map(_event_date),
            "source": raw_source.astype("string"),
        }
    )
    normalized["underlying_key"] = normalized["symbol"].map(_symbol_key)
    normalized["event_role"] = normalized.apply(
        lambda row: row["event_role"] or _normalise_event_role(row["event_type"]), axis=1,
    )
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
        subset=["underlying_key", "event_date", "event_type", "visible_from"], keep="last"
    )
    normalized = normalized.sort_values(["event_date", "underlying_key"]).reset_index(drop=True)
    return normalized[[
        "symbol", "underlying_key", "event_date", "event_timestamp", "event_type", "event_role",
        "period_text", "visible_from", "source",
    ]]


def normalize_dividend_calendar(events, symbols=None, start_date=None, end_date=None, as_of=None):
    """统一带公告日、除息日和每股现金金额的分红事件。"""
    if events is None:
        return None
    if not isinstance(events, pd.DataFrame) or events.empty:
        return pd.DataFrame(
            columns=["symbol", "underlying_key", "ex_date", "announcement_date", "cash_per_share", "currency"]
        )

    frame = events.copy()
    columns = {str(column).strip().lower(): column for column in frame.columns}

    def column(*names):
        for name in names:
            selected = columns.get(name.lower())
            if selected is not None:
                return frame[selected]
        return pd.Series([None] * len(frame), index=frame.index)

    symbols_raw = column("symbol", "security", "code", "stock_code", "underlying")
    ex_dates = column("ex_date", "ex_div_date", "dividend_ex_date", "event_date", "date")
    announcement_dates = column(
        "announcement_date", "announce_date", "pub_date", "declaration_date", "publish_date"
    )
    cash_values = column(
        "cash_per_share", "per_cash_div", "dividend_per_share", "cash_dividend",
        "dividend_amount", "cash_amount", "amount",
    )
    currency = column("currency", "dividend_currency", "currency_code")
    normalized = pd.DataFrame(
        {
            "symbol": symbols_raw.map(lambda value: str(value or "").strip().upper()),
            "ex_date": ex_dates.map(_event_date),
            "announcement_date": announcement_dates.map(_event_date),
            "cash_per_share": pd.to_numeric(cash_values, errors="coerce"),
            "currency": currency.astype("string"),
        }
    )
    normalized["underlying_key"] = normalized["symbol"].map(_symbol_key)
    normalized = normalized[normalized["ex_date"].notna()]

    if symbols:
        allowed = {_symbol_key(item) for item in symbols if _symbol_key(item)}
        normalized = normalized[normalized["underlying_key"].isin(allowed)]
    start = _event_date(start_date) if start_date else None
    end = _event_date(end_date) if end_date else None
    cutoff = _event_date(as_of) if as_of else None
    if start is not None and not pd.isna(start):
        normalized = normalized[normalized["ex_date"] >= start]
    if end is not None and not pd.isna(end):
        normalized = normalized[normalized["ex_date"] <= end]
    if cutoff is not None and not pd.isna(cutoff):
        normalized = normalized[
            normalized["announcement_date"].isna()
            | (normalized["announcement_date"] < cutoff)
        ]

    normalized = normalized.drop_duplicates(
        subset=["underlying_key", "ex_date", "announcement_date"], keep="last"
    )
    normalized = normalized.sort_values(["ex_date", "underlying_key"]).reset_index(drop=True)
    return normalized[[
        "symbol", "underlying_key", "ex_date", "announcement_date", "cash_per_share", "currency"
    ]]


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
                    "event_type": (
                        "earnings" if pd.isna(row.event_type) else str(row.event_type or "earnings")
                    ),
                    "event_role": (
                        "expected" if pd.isna(row.event_role) else str(row.event_role or "expected")
                    ),
                    "period_text": str(period or ""),
                    "visible_from": (
                        None
                        if pd.isna(row.visible_from)
                        else _event_date(row.visible_from).strftime("%Y-%m-%d")
                    ),
                    "source": (
                        "" if pd.isna(row.source) else str(row.source or "")
                    ),
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


def dividend_events_by_underlying(events):
    """把统一分红事件表转成可写入行情 attrs 的轻量字典。"""
    normalized = normalize_dividend_calendar(events)
    result = {}
    if normalized is None or normalized.empty:
        return result
    for key, group in normalized.groupby("underlying_key", sort=False):
        rows = []
        for row in group.itertuples(index=False):
            ex_date = _event_date(row.ex_date)
            if pd.isna(ex_date):
                continue
            announcement_date = _event_date(row.announcement_date)
            try:
                cash = float(row.cash_per_share)
            except (TypeError, ValueError, OverflowError):
                cash = float("nan")
            currency = row.currency
            if pd.isna(currency):
                currency = ""
            rows.append(
                {
                    "symbol": str(row.symbol or ""),
                    "ex_date": ex_date.strftime("%Y-%m-%d"),
                    "announcement_date": (
                        announcement_date.strftime("%Y-%m-%d")
                        if not pd.isna(announcement_date) else None
                    ),
                    "cash_per_share": cash,
                    "currency": str(currency),
                }
            )
        if rows:
            result[str(key)] = tuple(rows)
    return result


def attach_dividend_calendar(datas, events):
    """把分红快照附加到行情 DataFrame，不改变 OHLCV 列和数据内容。"""
    by_underlying = dividend_events_by_underlying(events)
    attrs_source = getattr(events, "attrs", {}) or {} if events is not None else {}
    as_of_supported = bool(attrs_source.get("as_of_supported", False)) if events is not None else False
    current_supported = bool(
        attrs_source.get("current_snapshot_supported", attrs_source.get("as_of_supported", False))
    ) if events is not None else False
    source = str(attrs_source.get("source", "data_source") or "data_source") if events is not None else "data_source"
    for symbol, dataframe in (datas or {}).items():
        if not isinstance(dataframe, pd.DataFrame):
            continue
        key = _symbol_key(symbol)
        attrs = dict(getattr(dataframe, "attrs", {}) or {})
        attrs["dividend_calendar"] = tuple(by_underlying.get(key, ()))
        attrs["dividend_calendar_available"] = events is not None
        attrs["dividend_calendar_source"] = source
        attrs["dividend_calendar_as_of_supported"] = as_of_supported
        attrs["dividend_calendar_current_supported"] = current_supported
        dataframe.attrs = attrs
    return datas


def dividend_event_visible(event, current_dt):
    """仅在公告日期早于决策日时承认分红已知。"""
    current = _event_date(current_dt)
    announced = _event_date(event.get("announcement_date")) if isinstance(event, dict) else pd.NaT
    return bool(not pd.isna(current) and not pd.isna(announced) and announced < current)


def covered_call_early_assignment_risk(
    event_rows, current_dt, session_index, strike, spot, call_bid, expiry=None,
):
    """比较最后交易日的 Call 时间价值，缺少有效 Bid 时返回未知风险。"""
    current = _event_date(current_dt)
    try:
        strike_value, spot_value = float(strike), float(spot)
    except (TypeError, ValueError, OverflowError):
        strike_value = spot_value = float("nan")
    try:
        bid_value = float(call_bid)
    except (TypeError, ValueError, OverflowError):
        bid_value = float("nan")
    if pd.isna(current):
        return None
    index = pd.to_datetime(session_index, errors="coerce")
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    sessions = pd.DatetimeIndex(index[~index.isna()]).normalize().unique().sort_values()
    intrinsic = max(spot_value - strike_value, 0.0)
    extrinsic = max(bid_value - intrinsic, 0.0)
    for event in event_rows or ():
        if not isinstance(event, dict) or not dividend_event_visible(event, current):
            continue
        ex_date = _event_date(event.get("ex_date"))
        expiry_date = _event_date(expiry) if expiry is not None else pd.NaT
        try:
            amount = float(event.get("cash_per_share"))
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            pd.isna(ex_date) or not math.isfinite(amount) or amount <= 0 or ex_date <= current
            or (not pd.isna(expiry_date) and expiry_date < ex_date)
        ):
            continue
        prior_sessions = sessions[sessions < ex_date]
        if len(prior_sessions) == 0 or prior_sessions[-1] != current:
            continue
        if (
            not math.isfinite(strike_value) or not math.isfinite(spot_value)
            or not math.isfinite(bid_value)
            or strike_value <= 0 or spot_value <= 0 or bid_value <= 0
        ):
            announcement = _event_date(event.get("announcement_date"))
            return {
                "ex_date": ex_date.strftime("%Y-%m-%d"),
                "announcement_date": (
                    announcement.strftime("%Y-%m-%d") if not pd.isna(announcement) else None
                ),
                "cash_per_share": amount,
                "call_extrinsic_value": None,
                "risk_unknown": True,
            }
        if amount > extrinsic:
            announcement = _event_date(event.get("announcement_date"))
            return {
                "ex_date": ex_date.strftime("%Y-%m-%d"),
                "announcement_date": (
                    announcement.strftime("%Y-%m-%d") if not pd.isna(announcement) else None
                ),
                "cash_per_share": amount,
                "call_extrinsic_value": extrinsic,
                "risk_unknown": False,
            }
    return None


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
        if isinstance(row, dict):
            visible_from = _event_date(row.get("visible_from", row.get("snapshot_date")))
            if not pd.isna(visible_from) and visible_from >= current:
                continue
        value = row.get("event_date") if isinstance(row, dict) else row
        event_day = _event_date(value)
        if pd.isna(event_day):
            continue
        role = _normalise_event_role(row.get("event_role")) if isinstance(row, dict) else "expected"
        row_before = 0 if role == "filing" else before
        if event_day - pd.Timedelta(days=row_before) <= current <= event_day + pd.Timedelta(days=after):
            return True
    return False


def _normalise_event_role(value):
    """统一财报日期角色，区分预期日和实际 filing 日。"""
    raw = str(value or "").strip().lower()
    if raw in {"filing", "filed", "reported", "actual", "announcement", "公告", "已披露"}:
        return "filing"
    return "expected"


__all__ = [
    "attach_earnings_calendar",
    "attach_dividend_calendar",
    "covered_call_early_assignment_risk",
    "dividend_event_visible",
    "dividend_events_by_underlying",
    "earnings_blackout",
    "earnings_events_by_underlying",
    "normalize_dividend_calendar",
    "normalize_earnings_calendar",
]
