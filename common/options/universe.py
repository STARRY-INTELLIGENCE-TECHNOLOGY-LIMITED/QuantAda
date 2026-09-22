"""期权标的池展开用的纯筛选工具。

这些函数只处理标准化期权链和策略声明的过滤条件，不访问账户、Broker 或具体 Provider。
回测按 as_of 可见性筛选；实盘同步只决定保留/新增哪些合约代码。
"""

from __future__ import annotations

import pandas as pd

from .analytics import parse_option_symbol, safe_number


DEFAULT_AS_OF_STEP_DAYS = 7
DEFAULT_DELTA_BUFFER = 0.05
MAX_CANDIDATES_PER_SNAPSHOT = 64
MAX_UNION_PER_UNDERLYING = 256
HISTORICAL_MAX_UNION_PER_UNDERLYING = 1024
_DELTA_PARAM_NAMES = ("min_delta", "max_delta", "protective_put_delta")


def is_option_contract_symbol(symbol) -> bool:
    """判断代码是否已是期权合约，而不是正股/ETF。"""
    return bool(parse_option_symbol(symbol).get("option_type"))


def split_symbol_pool(symbols) -> tuple[list[str], list[str]]:
    """把输入池拆成标的列表和显式期权合约列表，并保持原顺序去重。"""
    underlyings = []
    option_symbols = []
    seen = set()
    for raw in symbols or []:
        symbol = str(raw or "").strip()
        if not symbol:
            continue
        key = symbol.upper()
        if key in seen:
            continue
        seen.add(key)
        if is_option_contract_symbol(symbol):
            option_symbols.append(symbol)
        else:
            underlyings.append(symbol)
    return underlyings, option_symbols


def _finite_param(values, name, default=None):
    """从合并后的参数表读取有限数值。"""
    number = safe_number(values.get(name), default if default is not None else float("nan"))
    if number != number:
        return default
    return number


def resolve_option_universe_spec(strategy_class, params=None) -> dict | None:
    """读取策略 opt-in 声明；未声明时返回 None，表示沿用静态代码列表。"""
    raw = getattr(strategy_class, "option_universe", None)
    if raw in {None, False, ""}:
        return None

    merged = {}
    class_params = getattr(strategy_class, "params", None)
    if isinstance(class_params, dict):
        merged.update(class_params)
    if isinstance(params, dict):
        merged.update(params)

    option_types = None
    overrides = {}
    if raw is True:
        option_types = {"CALL", "PUT"}
    elif isinstance(raw, str):
        option_types = {raw.strip().upper()} if raw.strip() else set()
    elif isinstance(raw, (list, tuple, set, frozenset)):
        option_types = {str(item).strip().upper() for item in raw if str(item).strip()}
    elif isinstance(raw, dict):
        declared = raw.get("option_types", raw.get("types", True))
        if declared is True:
            option_types = {"CALL", "PUT"}
        elif isinstance(declared, str):
            option_types = {declared.strip().upper()} if declared.strip() else set()
        else:
            option_types = {
                str(item).strip().upper() for item in declared or () if str(item).strip()
            }
        for key in ("min_dte", "max_dte", "min_delta", "max_delta", "min_open_interest"):
            if key in raw and raw[key] is not None:
                overrides[key] = raw[key]
    else:
        return None

    option_types = {item for item in option_types or set() if item in {"CALL", "PUT", "C", "P"}}
    normalised_types = set()
    for item in option_types:
        if item in {"P", "PUT"}:
            normalised_types.add("PUT")
        elif item in {"C", "CALL"}:
            normalised_types.add("CALL")
    if not normalised_types:
        raise ValueError("option_universe 必须声明 PUT/CALL")

    min_dte = overrides.get("min_dte", merged.get("min_dte"))
    max_dte = overrides.get("max_dte", merged.get("max_dte"))
    min_dte = safe_number(min_dte)
    max_dte = safe_number(max_dte)
    if min_dte != min_dte or max_dte != max_dte:
        raise ValueError("option_universe 需要有限的 min_dte 和 max_dte")
    min_dte = int(min_dte)
    max_dte = int(max_dte)
    if min_dte < 0 or max_dte < min_dte:
        raise ValueError("option_universe 的 DTE 窗口无效")

    delta_values = []
    for name in _DELTA_PARAM_NAMES:
        number = _finite_param(overrides if name in overrides else merged, name)
        if number is not None:
            delta_values.append(number)
    min_delta = min(delta_values) if delta_values else None
    max_delta = max(delta_values) if delta_values else None
    protective_delta = _finite_param(merged, "protective_put_delta")
    delta_targets = None
    if min_delta is not None and max_delta is not None and protective_delta is not None:
        delta_targets = (
            (float(min_delta) + float(max_delta)) / 2.0,
            float(protective_delta),
        )
    min_open_interest = _finite_param(
        overrides if "min_open_interest" in overrides else merged,
        "min_open_interest",
        0.0,
    )
    if min_open_interest is None or min_open_interest < 0:
        min_open_interest = 0.0

    return {
        "option_types": frozenset(normalised_types),
        "min_dte": min_dte,
        "max_dte": max_dte,
        "min_delta": min_delta,
        "max_delta": max_delta,
        "delta_targets": delta_targets,
        "min_open_interest": float(min_open_interest),
    }


def widen_universe_bounds(spec: dict, *, live: bool, step_days: int = DEFAULT_AS_OF_STEP_DAYS) -> dict:
    """历史抽样时放宽 DTE/Delta，避免周频 as_of 漏掉边界合约；实盘使用原窗口。"""
    result = dict(spec)
    if live:
        return result
    step = max(0, int(step_days or 0))
    result["min_dte"] = max(0, int(spec["min_dte"]) - step)
    result["max_dte"] = int(spec["max_dte"]) + step
    if spec.get("min_delta") is not None:
        result["min_delta"] = float(spec["min_delta"]) - DEFAULT_DELTA_BUFFER
    if spec.get("max_delta") is not None:
        result["max_delta"] = float(spec["max_delta"]) + DEFAULT_DELTA_BUFFER
        if "PUT" in spec["option_types"] and "CALL" not in spec["option_types"]:
            result["max_delta"] = min(0.0, result["max_delta"])
        if "CALL" in spec["option_types"] and "PUT" not in spec["option_types"]:
            result["min_delta"] = max(0.0, result.get("min_delta") or 0.0)
    return result


def historical_as_of_dates(start_date, end_date, step_days: int = DEFAULT_AS_OF_STEP_DAYS):
    """生成回测可见性日期；包含起止日，并跳过周末。"""
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if pd.isna(start) or pd.isna(end):
        raise ValueError("historical option universe requires start_date and end_date")
    start = start.tz_localize(None) if start.tzinfo is not None else start
    end = end.tz_localize(None) if end.tzinfo is not None else end
    start = start.normalize()
    end = end.normalize()
    if end < start:
        raise ValueError("option universe end_date is before start_date")
    step = max(1, int(step_days or DEFAULT_AS_OF_STEP_DAYS))
    dates = list(pd.date_range(start, end, freq=f"{step}D"))
    if not dates or dates[0] != start:
        dates.insert(0, start)
    if dates[-1] != end:
        dates.append(end)
    unique = []
    seen = set()
    for value in dates:
        day = pd.Timestamp(value).normalize()
        if day.weekday() >= 5:
            continue
        key = day.date()
        if key in seen:
            continue
        seen.add(key)
        unique.append(day)
    if not unique:
        unique = [start, end] if start != end else [start]
    return unique


def select_chain_candidates(chain, *, as_of, spec: dict, limit: int = MAX_CANDIDATES_PER_SNAPSHOT) -> list[str]:
    """从标准化期权链中选出当前可见的候选合约代码。"""
    if chain is None or not isinstance(chain, pd.DataFrame) or chain.empty:
        return []
    as_of_ts = pd.Timestamp(as_of)
    if pd.isna(as_of_ts):
        return []
    as_of_ts = as_of_ts.tz_localize(None) if as_of_ts.tzinfo is not None else as_of_ts
    as_of_ts = as_of_ts.normalize()

    required = {"option_symbol", "option_type", "expiry"}
    if any(column not in chain.columns for column in required):
        return []

    option_types = spec["option_types"]
    min_dte = int(spec["min_dte"])
    max_dte = int(spec["max_dte"])
    min_delta = spec.get("min_delta")
    max_delta = spec.get("max_delta")
    min_open_interest = float(spec.get("min_open_interest") or 0.0)
    target_deltas = []
    raw_targets = spec.get("delta_targets")
    if raw_targets:
        for raw_target in raw_targets:
            target = safe_number(raw_target)
            if target == target:
                target_deltas.append(float(target))
    target_delta = None
    if min_delta is not None and max_delta is not None:
        target_delta = (float(min_delta) + float(max_delta)) / 2.0
    if not target_deltas and target_delta is not None:
        target_deltas = [target_delta]

    scored = []
    for _, row in chain.iterrows():
        symbol = str(row.get("option_symbol") or "").strip()
        option_type = str(row.get("option_type") or "").strip().upper()
        if option_type in {"P", "C"}:
            option_type = "PUT" if option_type == "P" else "CALL"
        if not symbol or option_type not in option_types:
            continue
        expiry = pd.to_datetime(row.get("expiry"), errors="coerce")
        if pd.isna(expiry):
            continue
        expiry = expiry.tz_localize(None) if expiry.tzinfo is not None else expiry
        dte = int((expiry.normalize() - as_of_ts).days)
        if dte < min_dte or dte > max_dte:
            continue
        delta = safe_number(row.get("delta"))
        if min_delta is not None or max_delta is not None:
            if delta != delta:
                continue
            if min_delta is not None and delta < float(min_delta):
                continue
            if max_delta is not None and delta > float(max_delta):
                continue
        open_interest = safe_number(row.get("open_interest"), 0.0)
        if open_interest != open_interest or open_interest < min_open_interest:
            continue
        strike = safe_number(row.get("strike"), 0.0)
        target_index = 0
        if target_deltas and delta == delta:
            distances = [abs(delta - target) for target in target_deltas]
            target_index = min(range(len(distances)), key=lambda index: distances[index])
            delta_distance = distances[target_index]
        else:
            delta_distance = 0.0
        scored.append((
            delta_distance,
            strike if strike == strike else 0.0,
            symbol,
            target_index,
        ))

    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    cap = max(len(target_deltas), 1, int(limit or MAX_CANDIDATES_PER_SNAPSHOT))
    unique = []
    seen = set()
    # 每个 Delta 锚点至少保留一个最近候选；剩余名额再按整体距离填充。
    # PCS 的保护腿因此不会被短腿中心附近的合约全部挤掉。
    prioritized = []
    for target_index in range(len(target_deltas)):
        candidate = next(
            (item for item in scored if item[3] == target_index),
            None,
        )
        if candidate is not None:
            prioritized.append(candidate)
    ordered = prioritized + [item for item in scored if item not in prioritized]
    for _distance, _strike, symbol, _target_index in ordered:
        key = symbol.upper()
        if key in seen:
            continue
        seen.add(key)
        unique.append(symbol)
        if len(unique) >= cap:
            break
    return unique


def merge_universe_symbols(underlyings, explicit_options, discovered_options) -> list[str]:
    """标的在前，其次调用方显式合约，最后是链上发现的合约。"""
    merged = []
    seen = set()
    for group in (underlyings, explicit_options, discovered_options):
        for raw in group or []:
            symbol = str(raw or "").strip()
            if not symbol:
                continue
            key = symbol.upper()
            if key in seen:
                continue
            seen.add(key)
            merged.append(symbol)
    return merged


def reconcile_live_option_symbols(
    source_symbols,
    discovered_options,
    *,
    held_symbols=(),
    pending_symbols=(),
    pending_trusted=True,
    current_symbols=(),
) -> list[str]:
    """实盘目标列表：当前链候选 + 仍有仓位/在途的合约。链不可信时不丢现有期权。"""
    underlyings, explicit_options = split_symbol_pool(source_symbols)
    keep_extra = []
    held = {str(item).strip().upper() for item in held_symbols or () if str(item).strip()}
    pending = {str(item).strip().upper() for item in pending_symbols or () if str(item).strip()}
    current = [str(item).strip() for item in current_symbols or () if str(item).strip()]
    protect = set(held)
    if pending_trusted:
        protect.update(pending)
    else:
        protect.update(item.upper() for item in current)
    for symbol in current:
        if is_option_contract_symbol(symbol) and symbol.upper() in protect:
            keep_extra.append(symbol)
    # 账户里已有或在途的期权即使从未进入当前 datas，也必须保留；
    # 否则链外旧仓会从滚动池消失，后续义务/平仓都看不到它。
    for raw in held:
        if is_option_contract_symbol(raw):
            keep_extra.append(raw)
    if pending_trusted:
        for raw in pending:
            if is_option_contract_symbol(raw):
                keep_extra.append(raw)
    discovered = [] if discovered_options is None else discovered_options
    return merge_universe_symbols(underlyings, explicit_options + keep_extra, discovered)


__all__ = [
    "DEFAULT_AS_OF_STEP_DAYS",
    "MAX_CANDIDATES_PER_SNAPSHOT",
    "MAX_UNION_PER_UNDERLYING",
    "HISTORICAL_MAX_UNION_PER_UNDERLYING",
    "historical_as_of_dates",
    "is_option_contract_symbol",
    "merge_universe_symbols",
    "reconcile_live_option_symbols",
    "resolve_option_universe_spec",
    "select_chain_candidates",
    "split_symbol_pool",
    "widen_universe_bounds",
]
