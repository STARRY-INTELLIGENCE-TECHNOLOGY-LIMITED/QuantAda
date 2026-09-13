"""期权行情和指标序列的有限值边界。"""

from __future__ import annotations

import math

import pandas as pd


_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_OPTION_NUMERIC_COLUMNS = (
    "spot", "bid", "ask", "last", "volume", "open_interest", "iv",
    "delta", "gamma", "theta", "vega", "rho", "historical_volatility",
)


def sanitize_market_dataframe(frame, *, require_ohlcv=True):
    """返回可进入策略的行情副本；无法证明有限值的数据直接失败关闭。

    这里只清理数据事实，不填充价格或期权风险字段。缺失的期权可选字段保留为
    NaN，交由链标准化或策略的可交易性检查决定，避免用伪造值放大风险。
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    result = frame.copy()
    if not isinstance(result.index, pd.DatetimeIndex):
        result.index = pd.to_datetime(result.index, errors="coerce")
    else:
        result.index = pd.to_datetime(result.index, errors="coerce")
    result = result[~result.index.isna()]
    if result.empty:
        return None
    result = result[~result.index.duplicated(keep="last")].sort_index()

    # 实盘增量快照可以省略 volume，但价格四列始终是可交易事实的最低要求。
    required_names = _OHLCV_COLUMNS if require_ohlcv else _OHLCV_COLUMNS[:4]
    if any(name not in result.columns for name in required_names):
        return None
    # 只对本次调用声明的必需字段做缺失值门控；可选的 volume 等字段即使为
    # NaN 也不能导致整根行情被丢弃。
    required = [name for name in required_names if name in result.columns]
    for name in required:
        result[name] = pd.to_numeric(result[name], errors="coerce")
    if required:
        result = result.dropna(subset=required)
        finite_mask = pd.Series(True, index=result.index)
        for name in required:
            finite_mask &= result[name].map(math.isfinite)
        result = result[finite_mask]
    if result.empty:
        return None
    for name in _OPTION_NUMERIC_COLUMNS:
        if name not in result.columns:
            continue
        values = pd.to_numeric(result[name], errors="coerce")
        result[name] = values.where(values.map(math.isfinite), float("nan"))
    if "volume" in result.columns:
        valid_volume = result["volume"].isna() | (result["volume"] >= 0)
        result = result[valid_volume]
    return result if not result.empty else None


def sanitize_indicator_series(series):
    """清除指标中的非有限值，不改变原序列对象。"""
    if not isinstance(series, pd.Series):
        try:
            series = pd.Series(series)
        except Exception:
            return pd.Series(dtype="float64")
    result = pd.to_numeric(series.copy(), errors="coerce")
    result = result.where(result.map(math.isfinite))
    result = result.dropna()
    if not result.empty:
        result = result[~result.index.duplicated(keep="last")].sort_index()
    return result


__all__ = ["sanitize_market_dataframe", "sanitize_indicator_series"]
