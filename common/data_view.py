"""通用行情、仓位和在途订单只读视图工具。"""

from __future__ import annotations

import math

import pandas as pd


def bar_datetime(data, broker=None):
    """获取当前 K 线时间；实盘优先使用 broker 时钟。"""
    live_now = getattr(broker, "_datetime", None) if broker is not None else None
    accessor = getattr(getattr(data, "datetime", None), "datetime", None)
    if callable(accessor):
        current = accessor(0)
    else:
        dataframe = getattr(getattr(data, "p", None), "dataname", None)
        if not isinstance(dataframe, pd.DataFrame) or dataframe.empty:
            current = live_now
        else:
            current = dataframe.index[-1]
    timestamp = None if current is None else pd.Timestamp(current)
    if timestamp is not None and timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    if live_now is not None:
        stamp = pd.Timestamp(live_now)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_localize(None)
        if timestamp is None or stamp > timestamp:
            return stamp
    return timestamp


def visible_row(data, current_dt, require_current_quote=False, cache_owner=None):
    """返回不超过当前时间的最后一行有效行情。"""
    dataframe = getattr(getattr(data, "p", None), "dataname", None)
    if not isinstance(dataframe, pd.DataFrame) or dataframe.empty or current_dt is None:
        return None
    cache = None
    cache_key = None
    if cache_owner is not None and not getattr(cache_owner, "is_live", False):
        normalized_dt = pd.Timestamp(current_dt)
        if getattr(normalized_dt, "tzinfo", None) is not None:
            normalized_dt = normalized_dt.tz_localize(None)
        if getattr(cache_owner, "_visible_row_cache_dt", None) != normalized_dt:
            cache_owner._visible_row_cache_dt = normalized_dt
            cache_owner._visible_row_cache = {}
        cache = getattr(cache_owner, "_visible_row_cache", None)
        if isinstance(cache, dict):
            cache_key = (id(data), bool(require_current_quote))
            if cache_key in cache:
                return cache[cache_key]
    index = None
    if cache_owner is not None and not getattr(cache_owner, "is_live", False):
        index_cache = getattr(cache_owner, "_visible_index_cache", None)
        if not isinstance(index_cache, dict):
            index_cache = {}
            cache_owner._visible_index_cache = index_cache
        cached_index = index_cache.get(id(dataframe))
        if cached_index is not None and cached_index[0] is dataframe:
            index = cached_index[1]
        else:
            index = pd.to_datetime(dataframe.index, errors="coerce")
            if getattr(index, "tz", None) is not None:
                index = index.tz_localize(None)
            index_cache[id(dataframe)] = (dataframe, index)
    if index is None:
        index = pd.to_datetime(dataframe.index, errors="coerce")
        if getattr(index, "tz", None) is not None:
            index = index.tz_localize(None)
    current_timestamp = pd.Timestamp(current_dt)
    if current_timestamp.tzinfo is not None:
        current_timestamp = current_timestamp.tz_localize(None)
    if require_current_quote:
        latest = None
        latest_dt = None
        if index.is_monotonic_increasing and not index.hasnans:
            position = int(index.searchsorted(current_timestamp, side="right")) - 1
            if position >= 0:
                latest = dataframe.iloc[position]
                latest_dt = pd.Timestamp(index[position]).normalize()
        else:
            visible_mask = index <= current_timestamp
            visible = dataframe.loc[visible_mask]
            if not visible.empty:
                latest = visible.iloc[-1]
                latest_dt = pd.Timestamp(index[visible_mask][-1]).normalize()
        if latest is None:
            if cache is not None:
                cache[cache_key] = None
            return None
        current_day = current_timestamp.normalize()
        bid = _safe_number(latest.get("bid", latest.get("bid_price")), 0.0)
        ask = _safe_number(latest.get("ask", latest.get("ask_price")), 0.0)
        close = _safe_number(latest.get("close"), 0.0)
        if latest_dt != current_day:
            if cache is not None:
                cache[cache_key] = None
            return None
        if close <= 0 and not (bid > 0 and ask >= bid):
            if cache is not None:
                cache[cache_key] = None
            return None
        if cache is not None:
            cache[cache_key] = latest
        return latest
    visible_mask = index <= current_timestamp
    visible = dataframe.loc[visible_mask]
    if visible.empty:
        if cache is not None:
            cache[cache_key] = None
        return None
    if "close" in visible.columns:
        closes = pd.to_numeric(visible["close"], errors="coerce")
        visible = visible.loc[closes.notna() & (closes > 0)]
    result = visible.iloc[-1] if not visible.empty else None
    if cache is not None:
        cache[cache_key] = result
    return result


def _safe_number(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def position_size(broker, data):
    """读取已确认仓位数量；没有持仓接口时视为 0。"""
    getter = getattr(broker, "get_position", None) or getattr(broker, "getposition", None)
    if not callable(getter):
        return 0.0
    position = getter(data)
    return _safe_number(getattr(position, "size", 0), 0.0)


def position_price(broker, data):
    """读取持仓成本价。"""
    getter = getattr(broker, "get_position", None) or getattr(broker, "getposition", None)
    if not callable(getter):
        return 0.0
    position = getter(data)
    return abs(_safe_number(getattr(position, "price", 0), 0.0))


def pending_symbols(broker):
    """读取当前可信在途订单的标的代码。"""
    if not getattr(broker, "is_live", False):
        return set()
    getter = getattr(broker, "get_pending_orders", None)
    if not callable(getter):
        return set()
    if getattr(broker, "_last_pending_orders_fetch_failed", False):
        raise RuntimeError("pending snapshot is untrusted")
    symbols = set()
    for item in getter() or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "") or "").strip().upper()
        if symbol:
            symbols.add(symbol)
    return symbols


def require_close_column(broker):
    """初始化时确认每个行情 feed 都提供 close 列。"""
    for data in getattr(broker, "datas", []) or []:
        dataframe = getattr(getattr(data, "p", None), "dataname", None)
        name = getattr(data, "_name", data)
        if not isinstance(dataframe, pd.DataFrame) or "close" not in dataframe.columns:
            raise TypeError(f"行情 feed {name!r} 要求提供 close 列。")


__all__ = [
    "bar_datetime",
    "pending_symbols",
    "position_price",
    "position_size",
    "require_close_column",
    "visible_row",
]
