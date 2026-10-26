"""期权估值使用的利率和远期股息输入。"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def align_rate_curve(index, curve, default=0.03) -> pd.Series:
    """把日收益率曲线按日期向后对齐到行情索引，返回小数形式。"""
    target = pd.DatetimeIndex(pd.to_datetime(index, errors="coerce")).normalize()
    try:
        fallback = float(default)
    except (TypeError, ValueError, OverflowError):
        fallback = 0.03
    if not math.isfinite(fallback):
        fallback = 0.03
    if not isinstance(curve, pd.DataFrame) or curve.empty:
        return pd.Series(fallback, index=index, dtype="float64")
    frame = curve.copy()
    date_column = next(
        (column for column in frame.columns if str(column).strip().lower() in {"date", "datetime"}),
        None,
    )
    rate_column = next(
        (
            column for column in frame.columns
            if str(column).strip().lower() in {"risk_free_rate", "rate_3m", "rate_1m", "sofr"}
        ),
        None,
    )
    if date_column is None or rate_column is None:
        return pd.Series(fallback, index=index, dtype="float64")
    dates = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
    rates = pd.to_numeric(frame[rate_column], errors="coerce")
    valid = dates.notna() & rates.map(np.isfinite)
    right = pd.DataFrame({"date": dates[valid], "rate": rates[valid]}).drop_duplicates(
        "date", keep="last"
    ).sort_values("date")
    if right.empty:
        return pd.Series(fallback, index=index, dtype="float64")
    left = pd.DataFrame({"_position": range(len(target)), "date": target})
    merged = pd.merge_asof(
        left.sort_values("date"), right, on="date", direction="backward",
    ).sort_values("_position")
    values = pd.to_numeric(merged["rate"], errors="coerce").fillna(fallback)
    return pd.Series(values.to_numpy(dtype="float64"), index=index)


def forward_dividend_yield_series(index, expiry, spot, events, default=0.0) -> pd.Series:
    """按已可见分红事件估算连续股息率，不把未来未知公告带入历史。"""
    target = pd.DatetimeIndex(pd.to_datetime(index, errors="coerce")).normalize()
    expiry_values = pd.to_datetime(expiry, errors="coerce")
    spot_values = pd.to_numeric(spot, errors="coerce")
    try:
        fallback = float(default)
    except (TypeError, ValueError, OverflowError):
        fallback = 0.0
    if not math.isfinite(fallback) or fallback < 0:
        fallback = 0.0
    rows = []
    for current, end, price in zip(target, expiry_values, spot_values):
        if pd.isna(current) or pd.isna(end) or not math.isfinite(float(price)) or float(price) <= 0:
            rows.append(fallback)
            continue
        horizon = max(1, (pd.Timestamp(end).normalize() - current).days)
        cash = 0.0
        for event in events or ():
            if not isinstance(event, dict):
                continue
            ex_date = pd.to_datetime(event.get("ex_date"), errors="coerce")
            if pd.isna(ex_date):
                continue
            ex_date = pd.Timestamp(ex_date).normalize()
            announcement = pd.to_datetime(event.get("announcement_date"), errors="coerce")
            visible_from = pd.to_datetime(
                event.get("visible_from", event.get("snapshot_date")), errors="coerce"
            )
            visible = (
                (not pd.isna(announcement) and pd.Timestamp(announcement).normalize() < current)
                or (not pd.isna(visible_from) and pd.Timestamp(visible_from).normalize() < current)
            )
            if not visible or ex_date <= current or ex_date > pd.Timestamp(end).normalize():
                continue
            try:
                amount = float(event.get("cash_per_share"))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(amount) and amount > 0:
                cash += amount
        rows.append(max(0.0, cash / float(price) * 365.0 / float(horizon)))
    return pd.Series(rows, index=index, dtype="float64")


__all__ = ["align_rate_curve", "forward_dividend_yield_series"]
