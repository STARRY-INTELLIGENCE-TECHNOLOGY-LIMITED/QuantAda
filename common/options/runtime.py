"""期权链刷新、合约选择与现货对冲的有界纯运行工具。"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import pandas as pd


class OptionRuntimeError(ValueError):
    """动态链或对冲输入不可信。"""


@dataclass(frozen=True)
class ChainSnapshot:
    """带新鲜度边界的当前期权链快照。"""

    data: object
    fetched_at: object
    as_of: object


@dataclass(frozen=True)
class HedgePlan:
    """按组合 Delta 计算的有界现货对冲计划。"""

    underlying: str
    current_delta: float
    target_delta: float
    requested_shares: float
    capped_shares: float
    blocked: bool
    reason: str = ""


def _finite(value, name, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        if default is not None:
            return default
        raise OptionRuntimeError(f"invalid {name}: {value!r}") from None
    if not math.isfinite(parsed):
        if default is not None:
            return default
        raise OptionRuntimeError(f"invalid {name}: {value!r}")
    return parsed


def _as_utc(value, name="timestamp"):
    """将时间统一为带 UTC 时区的时间戳；非法输入直接失败关闭。"""
    try:
        parsed = pd.Timestamp(value)
    except Exception:
        raise OptionRuntimeError(f"invalid {name}: {value!r}") from None
    if pd.isna(parsed):
        raise OptionRuntimeError(f"invalid {name}: {value!r}")
    if parsed.tzinfo is None:
        return parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC")


def _chain_observation_time(chain):
    """读取链中最后一个可见行情时间；没有时间戳时返回 None。"""
    attrs = getattr(chain, "attrs", {}) or {}
    for key in ("fetched_at", "timestamp"):
        value = attrs.get(key)
        if value is not None:
            try:
                return _as_utc(value, key)
            except OptionRuntimeError:
                return None
    if "timestamp" in getattr(chain, "columns", ()):
        values = pd.to_datetime(chain["timestamp"], errors="coerce", utc=True).dropna()
        if not values.empty:
            return values.max()
    return None


def refresh_option_chain(provider, underlying, *, start=None, end=None,
                         as_of=None, max_age_seconds=60.0,
                         now=None, monotonic=time.monotonic):
    """在单次调用预算内刷新期权链；失败或过期快照直接返回 None。"""
    age_limit = _finite(max_age_seconds, "max_age_seconds")
    if age_limit <= 0:
        raise OptionRuntimeError("max_age_seconds must be positive")
    started = monotonic()
    try:
        fetcher = getattr(provider, "get_option_chain_normalized", None)
        if fetcher is not None:
            chain = fetcher(
                underlying,
                start=start,
                end=end,
                timestamp=now,
                as_of=as_of,
            )
        else:
            fetcher = getattr(provider, "get_option_chain", None)
            if fetcher is None:
                return None
            chain = fetcher(
                underlying,
                start=start,
                end=end,
                timestamp=now,
                as_of=as_of,
                normalized=True,
            )
    except Exception:
        return None
    elapsed = monotonic() - started
    if elapsed > age_limit or chain is None or getattr(chain, "empty", True):
        return None
    timestamp = _as_utc(now, "now") if now is not None else pd.Timestamp.now(tz="UTC")
    observed = _chain_observation_time(chain)
    if observed is not None:
        # 快照时间晚于决策时刻说明发生 lookahead；过旧快照不能用于换月或对冲。
        age = (timestamp - observed).total_seconds()
        if age < 0 or age > age_limit:
            return None
    elif as_of is not None or getattr(chain, "attrs", {}).get("timestamp_source") == "caller":
        # 历史边界下没有源时间戳无法证明链的可见性。
        return None
    snapshot_as_of = _as_utc(as_of, "as_of") if as_of is not None else timestamp
    return ChainSnapshot(data=chain, fetched_at=timestamp, as_of=snapshot_as_of)


def select_option_contract(chain, *, option_type=None, min_dte=None, max_dte=None,
                           min_delta=None, max_delta=None,
                           target_delta=None, min_iv_percentile=None,
                           as_of=None,
                           max_spread_pct=None, now=None):
    """从可信链快照中确定性选择最窄盘口、最接近目标 Delta 的一份合约。"""
    if chain is None or getattr(chain, "empty", True):
        return None
    required = {"option_symbol", "option_type", "expiry", "strike", "delta", "bid", "ask", "last"}
    if not required.issubset(set(getattr(chain, "columns", ()) )):
        return None
    frame = chain.copy()
    if option_type:
        frame = frame[frame["option_type"].str.upper() == str(option_type).upper()]
    current = _as_utc(now, "now") if now is not None else pd.Timestamp.now(tz="UTC")
    observation = _chain_observation_time(frame)
    if observation is not None and observation > current:
        return None
    if getattr(chain, "attrs", {}).get("as_of") is not None:
        try:
            if _as_utc(chain.attrs["as_of"], "as_of") > current:
                return None
        except OptionRuntimeError:
            return None
    if getattr(chain, "attrs", {}).get("timestamp_source") == "caller":
        # 调用方时间戳不是源观测时间，不能单独证明历史可见性。
        return None
    frame["expiry"] = pd.to_datetime(frame["expiry"], errors="coerce", utc=True)
    for column in ("strike", "delta", "bid", "ask", "last"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["expiry", "strike", "delta", "bid", "ask", "last"])
    frame = frame[(frame["strike"] > 0) & (frame["bid"] >= 0) & (frame["ask"] >= frame["bid"])]
    dte = (frame["expiry"] - current).dt.total_seconds() / 86400.0
    # 过期合约无论调用方是否传入 min_dte 都不得被选中。
    frame = frame[dte >= 0]
    dte = dte.loc[frame.index]
    if min_dte is not None:
        frame = frame[dte >= _finite(min_dte, "min_dte")]
    if max_dte is not None:
        frame = frame[dte <= _finite(max_dte, "max_dte")]
    # Put/Call 的 Delta 方向有符号。给出负边界时按有符号值过滤，
    # 只有纯正数区间才采用绝对值兼容旧的无方向调用。
    lower = _finite(min_delta, "min_delta") if min_delta is not None else None
    upper = _finite(max_delta, "max_delta") if max_delta is not None else None
    signed = option_type is not None and str(option_type).upper() in {"PUT", "CALL"}
    signed = signed or (lower is not None and lower < 0) or (upper is not None and upper < 0)
    delta_values = frame["delta"] if signed else frame["delta"].abs()
    if lower is not None:
        frame = frame[delta_values >= lower]
        delta_values = delta_values.loc[frame.index]
    if upper is not None:
        frame = frame[delta_values <= upper]
        delta_values = delta_values.loc[frame.index]
    if target_delta is not None:
        target = _finite(target_delta, "target_delta")
        frame = frame.assign(_delta_distance=(delta_values - target).abs())
    if min_iv_percentile is not None:
        if "iv_percentile" not in frame.columns:
            return None
        ivp = pd.to_numeric(frame["iv_percentile"], errors="coerce")
        frame = frame[ivp >= _finite(min_iv_percentile, "min_iv_percentile")]
    spread = (frame["ask"] - frame["bid"]) / frame["last"].replace(0, float("nan")).abs()
    if max_spread_pct is not None:
        frame = frame[spread <= _finite(max_spread_pct, "max_spread_pct")]
    if frame.empty:
        return None
    frame = frame.assign(_spread=spread.loc[frame.index].fillna(float("inf")))
    sort_columns = (["_delta_distance"] if target_delta is not None else []) + [
        "_spread", "expiry", "strike", "option_symbol"
    ]
    selected = frame.sort_values(sort_columns, kind="mergesort").iloc[0]
    return selected.drop(labels=[name for name in ("_spread", "_delta_distance") if name in selected.index])


def compute_delta_hedge(underlying, current_delta, target_delta=0.0,
                       *, max_shares=0, lot_size=1, market_open=True,
                       max_turnover=None):
    """计算现货/ETF 对冲数量；盘口关闭、超限或无效输入时阻断。"""
    delta = _finite(current_delta, "current_delta")
    target = _finite(target_delta, "target_delta")
    limit = _finite(max_shares, "max_shares")
    step = _finite(lot_size, "lot_size")
    if not market_open:
        return HedgePlan(underlying, delta, target, 0.0, 0.0, True, "market_closed")
    if limit <= 0 or step <= 0:
        return HedgePlan(underlying, delta, target, 0.0, 0.0, True, "invalid_limits")
    requested = target - delta
    capped = max(-limit, min(limit, requested))
    capped = math.copysign(math.floor(abs(capped) / step) * step, capped)
    if max_turnover is not None and abs(capped) > _finite(max_turnover, "max_turnover"):
        return HedgePlan(underlying, delta, target, requested, 0.0, True, "max_turnover")
    return HedgePlan(underlying, delta, target, requested, capped, False)


__all__ = [
    "OptionRuntimeError",
    "ChainSnapshot",
    "HedgePlan",
    "refresh_option_chain",
    "select_option_contract",
    "compute_delta_hedge",
]
