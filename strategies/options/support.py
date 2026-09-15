"""期权样例策略共用的只读行情、仓位和过滤工具。"""

from __future__ import annotations

import math

import pandas as pd

from common.options.analytics import parse_option_symbol, safe_number, underlying_key


def bar_datetime(data, broker=None):
    """获取当前 K 线时间；实盘优先用 broker 时钟，避免日线 00:00 挡住当日快照。"""
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


def visible_row(data, current_dt):
    """返回不超过当前时间的最后一行行情。"""
    dataframe = getattr(getattr(data, "p", None), "dataname", None)
    if not isinstance(dataframe, pd.DataFrame) or dataframe.empty or current_dt is None:
        return None
    index = pd.to_datetime(dataframe.index, errors="coerce")
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    visible = dataframe.loc[index <= current_dt]
    return visible.iloc[-1] if not visible.empty else None


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


def position_size(broker, data):
    """读取已确认仓位数量；没有持仓接口时视为 0。"""
    getter = getattr(broker, "get_position", None) or getattr(broker, "getposition", None)
    if not callable(getter):
        return 0.0
    position = getter(data)
    return safe_number(getattr(position, "size", 0), 0.0)


def position_price(broker, data):
    """读取持仓成本价，供止盈判断。"""
    getter = getattr(broker, "get_position", None) or getattr(broker, "getposition", None)
    if not callable(getter):
        return 0.0
    position = getter(data)
    return abs(safe_number(getattr(position, "price", 0), 0.0))


def pending_symbols(broker):
    """读取在途期权代码。实盘快照不可信时失败关闭，避免重复开仓。"""
    if not getattr(broker, "is_live", False):
        return set()
    getter = getattr(broker, "get_pending_orders", None)
    if not callable(getter):
        return set()
    if getattr(broker, "_last_pending_orders_fetch_failed", False):
        raise RuntimeError("option sample stopped: pending snapshot is untrusted")
    symbols = set()
    for item in getter() or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "") or "").strip().upper()
        if symbol:
            symbols.add(symbol)
    return symbols


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


def iter_option_rows(broker, current_dt, option_types=None):
    """遍历当前可见的期权行情行。"""
    allowed = None
    if option_types is not None:
        allowed = {str(item).strip().upper() for item in option_types}
        allowed = {"PUT" if item in {"P", "PUT"} else "CALL" if item in {"C", "CALL"} else item for item in allowed}
    for data in getattr(broker, "datas", []) or []:
        row = visible_row(data, current_dt)
        if row is None:
            continue
        meta = option_contract(data, row)
        if meta is None:
            continue
        if allowed is not None and meta["option_type"] not in allowed:
            continue
        yield data, row, meta, quote_snapshot(row)


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


def require_close_column(broker):
    """初始化时确认每个 feed 都有 close 列。"""
    for data in getattr(broker, "datas", []) or []:
        dataframe = getattr(getattr(data, "p", None), "dataname", None)
        name = getattr(data, "_name", data)
        if not isinstance(dataframe, pd.DataFrame) or "close" not in dataframe.columns:
            raise TypeError(f"期权样例策略要求 {name!r} 提供 close 列。")
