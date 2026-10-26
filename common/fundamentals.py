"""点时基本面快照的规范化与行情对齐。"""

from __future__ import annotations

import numpy as np
import pandas as pd


FUNDAMENTAL_FIELDS = (
    "revenue", "gross_profit", "operating_income", "net_income",
    "stock_based_compensation", "cash", "debt", "shares", "buybacks",
)


def normalize_fundamental_snapshots(snapshots, ticker=None):
    """校验 SEC/其它 Provider 的 filing-date 基本面快照。"""
    columns = ["ticker", "asof", "report_end", *FUNDAMENTAL_FIELDS, "source_id"]
    if snapshots is None:
        return pd.DataFrame(columns=columns)
    if not isinstance(snapshots, pd.DataFrame) or snapshots.empty:
        return pd.DataFrame(columns=columns)
    frame = snapshots.copy()
    if "ticker" not in frame.columns:
        frame["ticker"] = str(ticker or "").strip().upper()
    if "source_id" not in frame.columns:
        frame["source_id"] = ""
    for name in ("asof", "report_end"):
        frame[name] = pd.to_datetime(frame.get(name), errors="coerce").dt.normalize()
    frame["ticker"] = frame["ticker"].astype("string").str.strip().str.upper()
    frame["source_id"] = frame["source_id"].fillna("").astype("string").str.strip()
    for name in FUNDAMENTAL_FIELDS:
        if name not in frame.columns:
            frame[name] = np.nan
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    frame = frame.loc[
        frame["ticker"].ne("") & frame["asof"].notna()
        & frame["report_end"].notna() & frame["source_id"].ne("")
    ]
    if ticker:
        frame = frame.loc[frame["ticker"].eq(str(ticker).strip().upper())]
    frame = frame.drop_duplicates(["ticker", "asof"], keep="last")
    return frame[columns].sort_values(["ticker", "asof"], kind="stable").reset_index(drop=True)


def merge_fundamental_snapshots(price_frame, snapshots, ticker, *, max_age_days=None):
    """把 filing-date 基本面向后对齐到行情，保留缺失状态审计列。"""
    if not isinstance(price_frame, pd.DataFrame) or price_frame.empty:
        return price_frame
    result = price_frame.copy()
    normalized = normalize_fundamental_snapshots(snapshots, ticker=ticker)
    dates = pd.DatetimeIndex(pd.to_datetime(result.index, errors="coerce", utc=True)).tz_convert(None).normalize()
    left = pd.DataFrame({"_row": range(len(result)), "_date": dates})
    aligned = left.copy()
    aligned["_asof"] = pd.NaT
    aligned["source_id"] = ""
    aligned["fundamental_status"] = "no_snapshot" if snapshots is not None else "unavailable"
    for field in FUNDAMENTAL_FIELDS:
        aligned[field] = np.nan
    if not normalized.empty:
        right = normalized.rename(columns={"asof": "_asof"}).drop(columns=["ticker"])
        aligned = pd.merge_asof(
            left.sort_values("_date"),
            right.sort_values("_asof"),
            left_on="_date", right_on="_asof", direction="backward",
            allow_exact_matches=True,
        ).sort_values("_row", kind="stable")
        aligned["fundamental_status"] = np.where(
            aligned["_asof"].notna(), "available", "no_snapshot",
        )
        if max_age_days is not None:
            age = (aligned["_date"] - aligned["_asof"]).dt.days
            stale = aligned["_asof"].notna() & age.gt(int(max_age_days))
            aligned.loc[stale, "fundamental_status"] = "stale"
            aligned.loc[stale, list(FUNDAMENTAL_FIELDS)] = np.nan
    aligned = aligned.sort_values("_row", kind="stable")
    for field in FUNDAMENTAL_FIELDS:
        result[f"fundamental_{field}"] = aligned[field].to_numpy(dtype="float64")
    result["fundamental_asof"] = aligned["_asof"].to_numpy()
    result["fundamental_source_id"] = aligned["source_id"].fillna("").astype(str).to_numpy()
    result["fundamental_status"] = aligned["fundamental_status"].astype(str).to_numpy()
    result.attrs.update(getattr(price_frame, "attrs", {}) or {})
    result.attrs["fundamental_source"] = "point_in_time_companyfacts"
    return result


__all__ = ["FUNDAMENTAL_FIELDS", "normalize_fundamental_snapshots", "merge_fundamental_snapshots"]
