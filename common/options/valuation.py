"""期权策略使用的点时估值快照工具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import math

import numpy as np
import pandas as pd


_VALUE_ALIASES = (
    "v",
    "fair_value",
    "underlying_fair_value",
    "underlying_value",
    "fundamental_value",
    "value_v",
)
_WEIGHTED_VALUE_ALIASES = (
    "w",
    "weighted_value",
    "underlying_weighted_value",
    "weighted_underlying_value",
    "value_w",
)

DEFAULT_FCFE_DISCOUNT_RATE = 0.10
DEFAULT_FCFE_TERMINAL_GROWTH = 0.02
DEFAULT_FCFE_HISTORY_PERIODS = 3
FCFE_VALUATION_METHOD = "fcfe_perpetuity_v1"


@dataclass(frozen=True)
class ValuationAnchor:
    """一个可审计的估值锚。"""

    ticker: str
    asof: pd.Timestamp | None
    v: float
    w: float
    confidence: float | None
    method: str
    source_id: str
    source: str


def load_valuation_snapshots(source: str | Path, *, w_multiple: float | None = None) -> pd.DataFrame:
    """读取并校验 CSV 或 Parquet 点时估值快照。"""
    path = Path(source)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path)
    elif suffix in {".parquet", ".pq"}:
        frame = pd.read_parquet(path)
    else:
        raise ValueError("估值快照仅支持 CSV 或 Parquet")
    return normalize_valuation_snapshots(frame, w_multiple=w_multiple)


def normalize_valuation_snapshots(
    snapshots: pd.DataFrame,
    *,
    w_multiple: float | None = None,
) -> pd.DataFrame:
    """规范化估值快照字段并拒绝重复或无效记录。"""
    if not isinstance(snapshots, pd.DataFrame):
        raise TypeError("估值快照必须是 pandas.DataFrame")
    if snapshots.empty:
        return pd.DataFrame(
            columns=["ticker", "asof", "v", "w", "confidence", "method", "source_id"]
        )

    columns = {str(column).strip().lower(): column for column in snapshots.columns}
    required = ("ticker", "asof", "v", "confidence", "method", "source_id")
    missing = [name for name in required if name not in columns]
    if missing:
        raise ValueError(f"估值快照缺少必需字段: {', '.join(missing)}")

    multiple = _require_w_multiple(w_multiple) if w_multiple is not None else None
    ticker = snapshots[columns["ticker"]].astype("string").str.strip().str.upper()
    asof = _date_series(snapshots[columns["asof"]])
    value = pd.to_numeric(snapshots[columns["v"]], errors="coerce")
    weighted_column = columns.get("w") or columns.get("weighted_value")
    if weighted_column is None:
        weighted = pd.Series(np.nan, index=snapshots.index, dtype="float64")
    else:
        weighted = pd.to_numeric(snapshots[weighted_column], errors="coerce")

    if multiple is not None:
        weighted = weighted.where(weighted.notna(), value * multiple)
        value = value.where(value.notna(), weighted / multiple)

    invalid = (
        ticker.isna()
        | ticker.eq("")
        | asof.isna()
        | value.isna()
        | ~np.isfinite(value)
        | value.le(0)
    )
    invalid |= weighted.notna() & (~np.isfinite(weighted) | weighted.le(0))
    if bool(invalid.any()):
        rows = [str(index) for index in snapshots.index[invalid][:5]]
        raise ValueError(f"估值快照含无效 ticker/asof/V/W，行索引示例: {', '.join(rows)}")

    confidence_raw = snapshots[columns["confidence"]]
    confidence = pd.to_numeric(confidence_raw, errors="coerce")
    confidence_text = confidence_raw.astype("string").str.strip()
    invalid_confidence = confidence_text.notna() & confidence_text.ne("") & confidence.isna()
    invalid_confidence |= confidence.notna() & ~np.isfinite(confidence)
    if bool(invalid_confidence.any()):
        raise ValueError("估值快照 confidence 必须是有限数值或空值")
    method = _text_series(snapshots, columns["method"])
    source_id = _text_series(snapshots, columns["source_id"])
    invalid_audit = method.eq("") | source_id.eq("")
    if bool(invalid_audit.any()):
        raise ValueError("估值快照 method 和 source_id 必须非空")

    result = pd.DataFrame(
        {
            "ticker": ticker,
            "asof": asof,
            "v": value.astype("float64"),
            "w": weighted.astype("float64"),
            "confidence": confidence.astype("float64"),
            "method": method,
            "source_id": source_id,
        }
    )
    duplicated = result.duplicated(["ticker", "asof"], keep=False)
    if bool(duplicated.any()):
        rows = result.loc[duplicated, ["ticker", "asof"]].head(5).to_dict("records")
        raise ValueError(f"同一 ticker/asof 只能有一条估值快照: {rows}")
    return result.sort_values(["ticker", "asof"], kind="stable").reset_index(drop=True)


def compute_fcfe_valuation_snapshots(
    fcfe_reports: pd.DataFrame,
    ticker: str,
    *,
    discount_rate: float = DEFAULT_FCFE_DISCOUNT_RATE,
    terminal_growth: float = DEFAULT_FCFE_TERMINAL_GROWTH,
    history_periods: int = DEFAULT_FCFE_HISTORY_PERIODS,
    w_multiple: float | None = None,
) -> pd.DataFrame:
    """从已披露的年度或 TTM FCFE 与股数计算点时永续增长估值。"""
    required = {"asof", "report_end", "fcfe", "shares", "source_id"}
    if not isinstance(fcfe_reports, pd.DataFrame):
        raise TypeError("FCFE 基本面必须是 pandas.DataFrame")
    missing = sorted(required.difference(fcfe_reports.columns))
    if missing:
        raise ValueError(f"年度基本面缺少必需字段: {', '.join(missing)}")
    try:
        discount = float(discount_rate)
        growth = float(terminal_growth)
        periods = int(history_periods)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("DCF 参数格式无效") from None
    if not math.isfinite(discount) or not math.isfinite(growth) or not 0 <= growth < discount:
        raise ValueError("discount_rate 和 terminal_growth 必须满足 0 <= growth < discount")
    if periods < 1:
        raise ValueError("history_periods 必须是正整数")
    multiple = _require_w_multiple(w_multiple) if w_multiple is not None else None

    rows = fcfe_reports.copy()
    rows["asof"] = _date_series(rows["asof"])
    rows["report_end"] = _date_series(rows["report_end"])
    rows["fcfe"] = pd.to_numeric(rows["fcfe"], errors="coerce")
    rows["shares"] = pd.to_numeric(rows["shares"], errors="coerce")
    rows["source_id"] = rows["source_id"].fillna("").astype("string").str.strip()
    valid = (
        rows["asof"].notna()
        & rows["report_end"].notna()
        & np.isfinite(rows["fcfe"])
        & np.isfinite(rows["shares"])
        & rows["shares"].gt(0)
        & rows["source_id"].ne("")
    )
    rows = rows.loc[valid].sort_values(["asof", "report_end", "source_id"], kind="stable")
    if rows.empty:
        return normalize_valuation_snapshots(
            pd.DataFrame(columns=["ticker", "asof", "v", "w", "confidence", "method", "source_id"])
        )

    output = []
    for available_date in rows["asof"].drop_duplicates().sort_values():
        available = rows.loc[rows["asof"].le(available_date)]
        latest_by_period = available.drop_duplicates("report_end", keep="last")
        history = latest_by_period.sort_values("report_end", kind="stable").tail(periods)
        if len(history) < periods:
            continue
        fcfe_values = history["fcfe"].to_numpy(dtype="float64")
        current = history.iloc[-1]
        if not bool(np.isfinite(fcfe_values).all()) or not bool((fcfe_values > 0).all()):
            continue
        normalized_fcfe = float(np.median(fcfe_values))
        value = normalized_fcfe * (1.0 + growth) / (discount - growth) / float(current["shares"])
        if not math.isfinite(value) or value <= 0:
            continue
        record = {
            "ticker": str(ticker or "").strip().upper(),
            "asof": pd.Timestamp(available_date),
            "v": value,
            "confidence": np.nan,
            "method": FCFE_VALUATION_METHOD,
            "source_id": str(current["source_id"]),
        }
        if multiple is not None:
            record["w"] = value * multiple
        output.append(record)
    if not output:
        return pd.DataFrame(
            columns=["ticker", "asof", "v", "w", "confidence", "method", "source_id"]
        )
    return normalize_valuation_snapshots(pd.DataFrame(output), w_multiple=multiple)


def merge_valuation_snapshots(
    price_frame: pd.DataFrame,
    snapshots: pd.DataFrame | None,
    ticker: str,
    *,
    max_age_days: int | None = None,
) -> pd.DataFrame:
    """把点时估值快照按日历日向后对齐到行情行，供缓存和回测复用。"""
    if not isinstance(price_frame, pd.DataFrame) or price_frame.empty:
        return price_frame
    ticker_key = str(ticker or "").strip().upper()
    result = price_frame.copy()
    price_dates = _date_index(result.index)
    left = pd.DataFrame({"_row": np.arange(len(result)), "_date": price_dates})
    aligned = left.copy()
    aligned["_asof"] = pd.NaT
    aligned["v"] = np.nan
    aligned["w"] = np.nan
    aligned["confidence"] = np.nan
    aligned["method"] = ""
    aligned["source_id"] = ""
    aligned["valuation_status"] = "unavailable" if snapshots is None else "no_snapshot"
    if snapshots is not None:
        normalized = normalize_valuation_snapshots(snapshots)
        selected = normalized.loc[normalized["ticker"].eq(ticker_key)]
        if not selected.empty:
            aligned = pd.merge_asof(
                left.sort_values("_date", kind="stable"),
                selected.sort_values("asof", kind="stable").rename(columns={"asof": "_asof"})
                .drop(columns=["ticker"]),
                left_on="_date",
                right_on="_asof",
                direction="backward",
                allow_exact_matches=True,
            ).sort_values("_row", kind="stable")
            aligned["valuation_status"] = np.where(aligned["_asof"].notna(), "available", "no_snapshot")
            if max_age_days is not None:
                try:
                    max_age = int(max_age_days)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("max_age_days 必须是非负整数或空值") from None
                if max_age < 0:
                    raise ValueError("max_age_days 必须是非负整数或空值")
                age_days = (aligned["_date"] - aligned["_asof"]).dt.days
                stale = aligned["_asof"].notna() & age_days.gt(max_age)
                aligned.loc[stale, "valuation_status"] = "stale"
                aligned.loc[stale, ["v", "w"]] = np.nan
    aligned = aligned.sort_values("_row", kind="stable")
    result["underlying_fair_value"] = aligned["v"].to_numpy(dtype="float64")
    result["underlying_weighted_value"] = aligned["w"].to_numpy(dtype="float64")
    result["valuation_asof"] = aligned["_asof"].to_numpy()
    result["valuation_confidence"] = aligned["confidence"].to_numpy(dtype="float64")
    result["valuation_method"] = aligned["method"].fillna("").astype(str).to_numpy()
    result["valuation_source_id"] = aligned["source_id"].fillna("").astype(str).to_numpy()
    result["valuation_status"] = aligned["valuation_status"].astype(str).to_numpy()
    result.attrs.update(getattr(price_frame, "attrs", {}) or {})
    result.attrs["valuation_source"] = "point_in_time_snapshots"
    return result


def resolve_valuation_anchor(
    price_frame: pd.DataFrame,
    ticker: str,
    decision_date: Any,
    *,
    snapshots: pd.DataFrame | None = None,
    w_multiple: float | None = None,
    value_window: int = 252,
    min_history: int = 60,
    allow_proxy: bool = False,
) -> ValuationAnchor | None:
    """读取决策日前的外部或行情内估值；可选回退到滞后滚动中位数。"""
    if not isinstance(price_frame, pd.DataFrame) or price_frame.empty:
        return None
    ticker_key = str(ticker or "").strip().upper()
    if not ticker_key:
        return None
    decision = _as_date(decision_date)
    if pd.isna(decision):
        return None

    if snapshots is not None:
        normalized = normalize_valuation_snapshots(snapshots, w_multiple=w_multiple)
        eligible = normalized.loc[
            normalized["ticker"].eq(ticker_key) & normalized["asof"].lt(decision)
        ]
        if eligible.empty:
            return None
        row = eligible.iloc[-1]
        return ValuationAnchor(
            ticker=ticker_key,
            asof=pd.Timestamp(row["asof"]),
            v=float(row["v"]),
            w=float(row["w"]),
            confidence=_optional_float(row["confidence"]),
            method=str(row["method"] or ""),
            source_id=str(row["source_id"] or ""),
            source="point_in_time_snapshot",
        )

    index = _date_index(price_frame.index)
    visible = index < decision
    if not bool(visible.any()):
        return None
    multiple = _require_w_multiple(w_multiple) if w_multiple is not None else None
    value_values = _numeric_alias(price_frame, _VALUE_ALIASES).to_numpy(dtype="float64")
    weighted_values = _numeric_alias(price_frame, _WEIGHTED_VALUE_ALIASES).to_numpy(dtype="float64")
    has_value = np.isfinite(value_values) & (value_values > 0)
    has_weighted = np.isfinite(weighted_values) & (weighted_values > 0)
    usable = visible & (
        (has_value & has_weighted)
        | (has_value & (multiple is not None))
        | (has_weighted & (multiple is not None))
    )
    value = weighted = float("nan")
    value_date = None
    source = "inline"
    if bool(usable.any()):
        # 行情行本身早于决策日并不等于估值快照已可见；显式审计日期仍必须
        # 严格早于决策日，避免把后来回填到旧行情行的估值带入历史决策。
        positions = np.flatnonzero(usable)
        position = None
        audit = None
        future_audit = False
        for candidate in reversed(positions):
            candidate_audit = _inline_audit(price_frame, int(candidate))
            if candidate_audit["asof"] is not None and candidate_audit["asof"] >= decision:
                future_audit = True
                continue
            position = int(candidate)
            audit = candidate_audit
            break
        if position is None:
            if future_audit:
                return None
            usable[:] = False
            confidence = method = source_id = None
        else:
            value = float(value_values[position]) if has_value[position] else float("nan")
            weighted = float(weighted_values[position]) if has_weighted[position] else float("nan")
            value_date = index[position]
            if not math.isfinite(value):
                value = weighted / multiple
                source = "weighted_derived"
            elif not math.isfinite(weighted):
                weighted = value * multiple
                source = "weighted_derived"
            if audit["asof"] is not None:
                value_date = audit["asof"]
                source = "point_in_time_snapshot"
            confidence = audit["confidence"]
            method = audit["method"]
            source_id = audit["source_id"]
    else:
        if _latest_text(price_frame, "valuation_status", index, visible) in {
            "no_snapshot", "unavailable", "stale"
        }:
            return None
        confidence = method = source_id = None

    if not (math.isfinite(value) and value > 0 and math.isfinite(weighted) and weighted > 0):
        if not allow_proxy or "close" not in price_frame.columns:
            return None
        close = pd.to_numeric(price_frame["close"], errors="coerce").to_numpy(dtype="float64")
        history = pd.Series(close, index=index)
        history = history.loc[visible & np.isfinite(close) & (close > 0)]
        history = history[~history.index.duplicated(keep="last")].sort_index()
        try:
            window = max(2, int(value_window))
            required = max(2, min(int(min_history), window))
        except (TypeError, ValueError, OverflowError):
            return None
        if len(history) < required or multiple is None:
            return None
        value = float(history.tail(window).median())
        weighted = value * multiple
        value_date = history.index[-1]
        source = "lagged_median_proxy"

    if not (math.isfinite(value) and value > 0 and math.isfinite(weighted) and weighted > 0):
        return None
    return ValuationAnchor(
        ticker=ticker_key,
        asof=None if value_date is None else pd.Timestamp(value_date),
        v=float(value),
        w=float(weighted),
        confidence=confidence,
        method=method or "",
        source_id=source_id or "",
        source=source,
    )


def _require_w_multiple(value: Any) -> float:
    """校验用于从 V 派生 W 的保守折扣比例。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("w_multiple 必须是大于 0 且不超过 1 的有限数值") from None
    if not math.isfinite(parsed) or not 0 < parsed <= 1:
        raise ValueError("w_multiple 必须是大于 0 且不超过 1 的有限数值")
    return parsed


def _date_series(values) -> pd.Series:
    """把日期列归一为无时区日历日。"""
    parsed = pd.to_datetime(values, errors="coerce", utc=True)
    return (
        pd.Series(parsed)
        .dt.tz_convert(None)
        .dt.normalize()
        .astype("datetime64[ns]")
        .set_axis(values.index)
    )


def _as_date(value) -> pd.Timestamp:
    """把决策时点归一到日历日。"""
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT
    if pd.isna(parsed):
        return pd.NaT
    if parsed.tzinfo is not None:
        parsed = parsed.tz_localize(None)
    return parsed.normalize()


def _date_index(index) -> pd.DatetimeIndex:
    """把行情索引转换为无时区日历日。"""
    parsed = pd.to_datetime(index, errors="coerce", utc=True)
    return pd.DatetimeIndex(parsed).tz_convert(None).normalize().astype("datetime64[ns]")


def _text_series(frame: pd.DataFrame, column) -> pd.Series:
    """提取文本审计列，缺失值规范为空串。"""
    if column is None:
        return pd.Series("", index=frame.index, dtype="string")
    return frame[column].fillna("").astype("string").str.strip()


def _optional_float(value: Any) -> float | None:
    """把可选数值转成有限浮点数。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _inline_audit(frame: pd.DataFrame, position: int) -> dict[str, Any]:
    """读取与行情行对齐的估值来源字段。"""
    row = frame.iloc[position]
    raw_asof = row.get("valuation_asof")
    asof = None if raw_asof is None or pd.isna(raw_asof) or str(raw_asof).strip() == "" else _as_date(raw_asof)
    return {
        "asof": asof,
        "confidence": _optional_float(row.get("valuation_confidence")),
        "method": str(row.get("valuation_method", "") or "").strip(),
        "source_id": str(row.get("valuation_source_id", "") or "").strip(),
    }


def _latest_text(
    frame: pd.DataFrame,
    column: str,
    index: pd.DatetimeIndex,
    visible: np.ndarray,
) -> str:
    """读取决策日前最后一条非空状态字段。"""
    columns = {str(name).strip().lower(): name for name in frame.columns}
    actual = columns.get(column.lower())
    if actual is None:
        return ""
    values = frame[actual].fillna("").astype(str).to_numpy()
    positions = np.flatnonzero(visible & (values != ""))
    return values[positions[-1]].strip().lower() if len(positions) else ""


def _numeric_alias(frame: pd.DataFrame, aliases: tuple[str, ...]) -> pd.Series:
    """按不区分大小写的别名读取首个数值字段。"""
    columns = {str(column).strip().lower(): column for column in frame.columns}
    for alias in aliases:
        column = columns.get(alias)
        if column is not None:
            return pd.to_numeric(frame[column], errors="coerce")
    attrs = getattr(frame, "attrs", {}) or {}
    for alias in aliases:
        if alias not in attrs:
            continue
        value = attrs[alias]
        if isinstance(value, pd.Series):
            return pd.to_numeric(value.reindex(frame.index), errors="coerce")
        try:
            return pd.to_numeric(pd.Series(value, index=frame.index), errors="coerce")
        except (TypeError, ValueError):
            continue
    return pd.Series(np.nan, index=frame.index, dtype="float64")


__all__ = [
    "ValuationAnchor",
    "compute_fcfe_valuation_snapshots",
    "load_valuation_snapshots",
    "merge_valuation_snapshots",
    "normalize_valuation_snapshots",
    "resolve_valuation_anchor",
]
