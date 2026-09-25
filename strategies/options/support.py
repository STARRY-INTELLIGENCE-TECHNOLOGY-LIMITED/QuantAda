"""期权样例专用过滤工具；通用行情和仓位视图由 ``common.data_view`` 提供。"""

from __future__ import annotations

import math

import pandas as pd

from common.data_view import (
    bar_datetime,
    pending_symbols,
    position_price,
    position_size,
    require_close_column,
    visible_row,
)
from common.options.analytics import parse_option_symbol, safe_number, underlying_key


def option_contract(data, row):
    """解析期权合约元数据；行内字段缺失或为 NaN 时回退到代码解析。"""
    parsed = parse_option_symbol(getattr(data, "_name", ""))
    raw_type = None
    raw_strike = None
    raw_expiry = None
    if row is not None:
        try:
            raw_type = row.get("option_type")
            raw_strike = row.get("strike")
            raw_expiry = row.get("expiry")
        except Exception:
            raw_type = raw_strike = raw_expiry = None
    option_type = str(raw_type).strip().upper() if raw_type is not None else ""
    if option_type in {"NAN", "NONE", "N/A", ""}:
        option_type = str(parsed.get("option_type", "") or "").strip().upper()
    if option_type in {"P", "PUT"}:
        option_type = "PUT"
    elif option_type in {"C", "CALL"}:
        option_type = "CALL"
    else:
        return None
    strike = safe_number(raw_strike, parsed.get("strike"))
    if not math.isfinite(strike) or strike <= 0:
        strike = safe_number(parsed.get("strike"))
    expiry = pd.to_datetime(raw_expiry, errors="coerce")
    if pd.isna(expiry):
        expiry = pd.to_datetime(parsed.get("expiry"), errors="coerce")
    if pd.isna(expiry) or not math.isfinite(strike) or strike <= 0:
        return None
    return {
        "option_type": option_type,
        "strike": float(strike),
        "expiry": expiry.normalize(),
        "underlying_key": underlying_key(parsed.get("underlying", "")),
        "symbol": str(getattr(data, "_name", "") or ""),
    }



def finite_row_number(row, *names):
    """按别名读取第一个有限正值或有限值；全缺失时返回 NaN。"""
    for name in names:
        try:
            value = row.get(name)
        except Exception:
            continue
        number = safe_number(value)
        if math.isfinite(number):
            return number
    return float("nan")


def quote_snapshot(row):
    """读取盘口、Greeks 和流动性字段；缺失 IVP 时记为 NaN。"""
    last = finite_row_number(row, "last", "close", "price")
    bid = finite_row_number(row, "bid", "bid_price")
    ask = finite_row_number(row, "ask", "ask_price")
    delta = finite_row_number(row, "delta", "option_delta")
    if (not math.isfinite(ask) or ask <= 0) and last > 0:
        ask = last
    if (not math.isfinite(bid) or bid <= 0) and last > 0:
        bid = last
    open_interest = safe_number(row.get("open_interest", 0.0), 0.0)
    ivp = safe_number(row.get("iv_percentile", row.get("iv_rank", row.get("ivp"))))
    hv = safe_number(row.get("historical_volatility", row.get("hv", row.get("iv"))))
    mid = last if last > 0 else ((bid + ask) / 2.0 if bid > 0 and ask >= bid else 0.0)
    spread_pct = (ask - bid) / mid if mid > 0 and ask >= bid > 0 else float("inf")
    return {
        "bid": bid,
        "ask": ask,
        "last": last,
        "delta": delta,
        "open_interest": open_interest if math.isfinite(open_interest) else 0.0,
        "iv_percentile": ivp,
        "historical_volatility": hv if math.isfinite(hv) else None,
        "spread_pct": spread_pct,
    }


def dte_days(expiry, current_dt):
    """计算剩余到期天数。"""
    if expiry is None or current_dt is None:
        return None
    return int((pd.Timestamp(expiry).normalize() - pd.Timestamp(current_dt).normalize()).days)


def underlying_feed(broker, key):
    """按归一化底层代码找到正股/ETF 行情。"""
    target = underlying_key(key)
    for data in getattr(broker, "datas", []) or []:
        name = str(getattr(data, "_name", "") or "")
        if parse_option_symbol(name).get("option_type"):
            continue
        if underlying_key(name) == target:
            return data
    return None


def _normalize_option_types(option_types):
    """把 PUT/CALL 别名收成策略过滤用的标准集合。"""
    if option_types is None:
        return None
    allowed = {str(item).strip().upper() for item in option_types}
    return {
        "PUT" if item in {"P", "PUT"} else "CALL" if item in {"C", "CALL"} else item
        for item in allowed
    }


def _option_quote_days(data):
    """返回离线行情中可能存在有效当前报价的交易日键。"""
    dataframe = getattr(getattr(data, "p", None), "dataname", None)
    if not isinstance(dataframe, pd.DataFrame) or dataframe.empty:
        return ()
    attrs = getattr(dataframe, "attrs", {})
    cached = attrs.get("_quantada_option_quote_days")
    if cached is not None:
        return cached
    index = pd.to_datetime(dataframe.index, errors="coerce")
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)

    def numeric_column(*names):
        for name in names:
            if name in dataframe.columns:
                return pd.to_numeric(dataframe[name], errors="coerce").fillna(0.0)
        return pd.Series(0.0, index=dataframe.index)

    close = numeric_column("close")
    bid = numeric_column("bid", "bid_price")
    ask = numeric_column("ask", "ask_price")
    valid = (close > 0) | ((bid > 0) & (ask >= bid))
    valid &= ~index.isna()
    days = index.normalize()[valid.to_numpy(dtype=bool)]
    keys = tuple(dict.fromkeys(pd.Timestamp(day).value for day in days))
    attrs["_quantada_option_quote_days"] = keys
    return keys


def _looks_like_option_data(data):
    """判断行情 feed 是否可能是期权；未知代码用期权元数据列兜底。"""
    if parse_option_symbol(getattr(data, "_name", "")).get("option_type"):
        return True
    dataframe = getattr(getattr(data, "p", None), "dataname", None)
    if not isinstance(dataframe, pd.DataFrame):
        return False
    return bool(
        set(dataframe.columns).intersection(
            {"option_type", "right", "cp", "put_call", "strike", "expiry"}
        )
    )


def _option_snapshot_datas(broker, current_dt):
    """按当前交易日返回有报价的期权 feed；实盘不启用离线索引。"""
    if getattr(broker, "is_live", False) or current_dt is None:
        return getattr(broker, "datas", []) or []
    datas = list(getattr(broker, "datas", []) or [])
    signature = tuple(id(data) for data in datas)
    if getattr(broker, "_option_snapshot_datas_signature", None) != signature:
        by_day = {}
        for data in datas:
            if not _looks_like_option_data(data):
                continue
            for day_key in _option_quote_days(data):
                by_day.setdefault(day_key, []).append(data)
        broker._option_snapshot_datas_signature = signature
        broker._option_snapshot_datas_by_day = by_day
    timestamp = pd.Timestamp(current_dt)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    by_day = getattr(broker, "_option_snapshot_datas_by_day", {})
    return by_day.get(timestamp.normalize().value, ())


def iter_option_rows(broker, current_dt, option_types=None):
    """遍历当前可见的期权行情行。"""
    allowed = _normalize_option_types(option_types)
    offline_cache = None
    cache_key = None
    if not getattr(broker, "is_live", False):
        timestamp = pd.Timestamp(current_dt)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_localize(None)
        if getattr(broker, "_option_rows_cache_dt", None) != timestamp:
            broker._option_rows_cache_dt = timestamp
            broker._option_rows_cache = {}
        offline_cache = getattr(broker, "_option_rows_cache", None)
        if isinstance(offline_cache, dict):
            normalized_allowed = None if allowed is None else tuple(sorted(allowed))
            cache_key = normalized_allowed
            cached = offline_cache.get(cache_key)
            if cached is not None:
                yield from cached
                return
    rows = []
    snapshot_datas = _option_snapshot_datas(broker, current_dt)
    for data in snapshot_datas:
        row = visible_row(data, current_dt, require_current_quote=True, cache_owner=broker)
        if row is None:
            continue
        meta = option_contract(data, row)
        if meta is None:
            continue
        if allowed is not None and meta["option_type"] not in allowed:
            continue
        rows.append((data, row, meta, quote_snapshot(row)))
    if offline_cache is not None:
        offline_cache[cache_key] = rows
    yield from rows


def _metadata_row(data):
    """读取 feed 最后一行元数据；报价是否可交易由调用方另判。"""
    dataframe = getattr(getattr(data, "p", None), "dataname", None)
    if isinstance(dataframe, pd.DataFrame) and not dataframe.empty:
        return dataframe.iloc[-1]
    return None


def iter_held_options(broker, option_types=None):
    """遍历已确认期权持仓；缺当日报价时也必须看见，不能当成空仓。"""
    allowed = _normalize_option_types(option_types)
    for data in getattr(broker, "datas", []) or []:
        size = position_size(broker, data)
        if size == 0:
            continue
        meta = option_contract(data, _metadata_row(data))
        if meta is None:
            continue
        if allowed is not None and meta["option_type"] not in allowed:
            continue
        yield data, size, meta


def reserved_underlying_keys(broker, option_types=None):
    """持仓和在途期权占用的底层。零填充或缺 K 不能把已有仓位让出名额。"""
    allowed = _normalize_option_types(option_types)
    keys = set()
    for _data, _size, meta in iter_held_options(broker, allowed):
        key = meta.get("underlying_key")
        if key:
            keys.add(key)
    for symbol in pending_symbols(broker):
        parsed = parse_option_symbol(symbol)
        option_type = str(parsed.get("option_type") or "").strip().upper()
        if option_type in {"P", "PUT"}:
            option_type = "PUT"
        elif option_type in {"C", "CALL"}:
            option_type = "CALL"
        else:
            continue
        if allowed is not None and option_type not in allowed:
            continue
        key = underlying_key(parsed.get("underlying", ""))
        if key:
            keys.add(key)
    return keys


def held_protective_put(broker, short_meta, current_dt):
    """同到期、更低执行价且实际持有的保护腿。没有当日报价时 quote/row 为 None。"""
    candidates = []
    short_key = short_meta.get("underlying_key")
    short_expiry = short_meta.get("expiry")
    short_strike = short_meta.get("strike")
    for data, size, meta in iter_held_options(broker, {"PUT"}):
        if size <= 0 or meta.get("underlying_key") != short_key:
            continue
        if meta.get("expiry") != short_expiry or meta.get("strike", 0) >= short_strike:
            continue
        row = visible_row(data, current_dt, require_current_quote=True, cache_owner=broker)
        candidates.append({
            "data": data,
            "meta": meta,
            "quote": quote_snapshot(row) if row is not None else None,
            "row": row,
            "size": size,
        })
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (item["meta"]["strike"], item["meta"]["symbol"]),
    )[0]

def matches_chain_window(meta, quote, current_dt, params):
    """按 DTE、Delta、价差和持仓量过滤；IVP 缺省且下限为 0 时放行。"""
    dte = dte_days(meta["expiry"], current_dt)
    if dte is None:
        return False
    min_dte = int(params.min_dte)
    max_dte = int(params.max_dte)
    if dte < min_dte or dte > max_dte:
        return False
    delta = quote["delta"]
    if math.isfinite(delta):
        if delta < float(params.min_delta) or delta > float(params.max_delta):
            return False
    if quote["open_interest"] < float(params.min_open_interest):
        return False
    min_ivp = float(getattr(params, "min_iv_percentile", 0.0) or 0.0)
    ivp = quote["iv_percentile"]
    if min_ivp > 0:
        if not math.isfinite(ivp) or ivp < min_ivp:
            return False
    bid = quote["bid"]
    ask = quote["ask"]
    has_bid = math.isfinite(bid) and bid > 0
    has_ask = math.isfinite(ask) and ask > 0
    if not has_bid and not has_ask:
        return False
    if has_bid and has_ask:
        if ask < bid:
            return False
        if quote["spread_pct"] > float(params.max_spread_pct):
            return False
    return True



def chain_window_reject_reason(meta, quote, current_dt, params):
    """返回过滤原因；通过时返回空字符串。供实盘样例打印，不改变下单语义。"""
    dte = dte_days(meta["expiry"], current_dt)
    if dte is None:
        return "dte_missing"
    if dte < int(params.min_dte) or dte > int(params.max_dte):
        return "dte=%s" % dte
    delta = quote["delta"]
    if math.isfinite(delta) and (delta < float(params.min_delta) or delta > float(params.max_delta)):
        return "delta=%.4f" % delta
    if quote["open_interest"] < float(params.min_open_interest):
        return "open_interest"
    min_ivp = float(getattr(params, "min_iv_percentile", 0.0) or 0.0)
    ivp = quote["iv_percentile"]
    if min_ivp > 0 and (not math.isfinite(ivp) or ivp < min_ivp):
        return "iv_percentile"
    bid = quote["bid"]
    ask = quote["ask"]
    has_bid = math.isfinite(bid) and bid > 0
    has_ask = math.isfinite(ask) and ask > 0
    if not has_bid and not has_ask:
        return "quote_missing"
    if has_bid and has_ask:
        if ask < bid:
            return "crossed_quote"
        if quote["spread_pct"] > float(params.max_spread_pct):
            return "spread=%.3f" % quote["spread_pct"]
    return ""


def option_limit_price(broker, data, quote, effect):
    """实盘用快照价并多/少一跳跨价，避免挂在买一/卖一上一直不成交。回测仍用行内 bid/ask。"""
    buy = str(effect or "").upper().startswith("BUY")
    bar = quote["ask"] if buy else quote["bid"]
    live = 0.0
    if getattr(broker, "is_live", False) and data is not None:
        getter = getattr(broker, "get_current_price", None)
        if callable(getter):
            live = safe_number(getter(data), 0.0)
    candidates = [value for value in (bar, live) if value and value > 0]
    if not candidates:
        return 0.0
    price = max(candidates) if buy else min(candidates)
    if getattr(broker, "is_live", False):
        tick = 0.05
        price = price + tick if buy else max(tick, price - tick)
    return float(price)
