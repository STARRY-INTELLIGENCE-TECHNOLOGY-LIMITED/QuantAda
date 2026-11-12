"""定时日 K 的语法及与数据源无关的 OHLCV 聚合。"""

from __future__ import annotations

from datetime import time
import re

import pandas as pd


def parse_daily_cutoff(value: str) -> time | None:
    """解析 Day:HH:MM[:SS]；普通周期返回 None，带非法时刻的周期报错。"""
    text = str(value or "").strip().lower()
    if ":" not in text:
        return None
    matched = re.fullmatch(r"(?:d|day|days|1d):(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if matched is not None:
        try:
            return time(int(matched[1]), int(matched[2]), int(matched[3] or 0))
        except ValueError:
            pass
    raise ValueError("timeframe must be Day:HH:MM[:SS] with a valid time")


def daily_cutoff_end_date(end_date, cutoff: time) -> str | None:
    """将分钟请求终点限制到指定交易日的 cutoff。"""
    if end_date in (None, ""):
        now = pd.Timestamp.now()
        candidate = now.normalize() + pd.Timedelta(
            hours=cutoff.hour, minutes=cutoff.minute, seconds=cutoff.second
        )
        if now < candidate:
            candidate = now
        return candidate.strftime("%Y-%m-%d %H:%M:%S")
    raw = str(end_date).strip()
    try:
        value = pd.Timestamp(end_date)
    except Exception:
        return end_date
    candidate = value.normalize() + pd.Timedelta(
        hours=cutoff.hour, minutes=cutoff.minute, seconds=cutoff.second
    )
    if re.search(r"(?:T|\s)\d", raw):
        candidate = min(value, candidate)
    now = pd.Timestamp.now(tz=value.tzinfo)
    return min(candidate, now).isoformat(sep=" ")


def aggregate_daily_cutoff(frame: pd.DataFrame, cutoff: time) -> pd.DataFrame:
    """按本地交易日聚合截止时刻之前已结束的 K 线。"""
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    local_index = frame.index.tz_localize(None) if frame.index.tz is not None else frame.index
    seconds = local_index.hour * 3600 + local_index.minute * 60 + local_index.second
    visible = frame.loc[seconds <= cutoff.hour * 3600 + cutoff.minute * 60 + cutoff.second]
    if visible.empty:
        return visible
    local_index = visible.index.tz_localize(None) if visible.index.tz is not None else visible.index
    grouped = visible.groupby(local_index.normalize())
    result = grouped.agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    })
    for column in visible.columns:
        if column not in result.columns:
            result[column] = grouped[column].last()
    result = result[[column for column in frame.columns if column in result.columns]]
    result.index.name = "datetime"
    if "openinterest" not in result.columns:
        result["openinterest"] = 0
    return result
