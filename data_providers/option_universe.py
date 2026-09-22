"""按策略声明把标的池展开为期权合约代码。

本模块只负责调用 DataManager.get_option_chain 并合并结果；回测必须使用带
HISTORICAL_OPTION_CHAIN 标记的历史链，实盘才允许当前快照。
"""

from __future__ import annotations

import glob
import inspect
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import pandas as pd

import config
from .csv_provider import CsvDataProvider

from common.options.universe import (
    DEFAULT_AS_OF_STEP_DAYS,
    MAX_CANDIDATES_PER_SNAPSHOT,
    HISTORICAL_MAX_UNION_PER_UNDERLYING,
    MAX_UNION_PER_UNDERLYING,
    historical_as_of_dates,
    merge_universe_symbols,
    resolve_option_universe_spec,
    select_chain_candidates,
    split_symbol_pool,
    widen_universe_bounds,
)

_PROGRESS_INTERVAL_SECONDS = 30.0
_FETCH_WORKERS = 4  # 与 Theta 单 session 并发上限对齐，禁止并发新认证。


def _incomplete_vendor_day():
    """美东当日；当日 EOD 未完成，不得作为历史 as_of。"""
    return pd.Timestamp.now(tz=ZoneInfo("America/New_York")).date()


def _iter_chain_providers(data_manager, specified_sources):
    """按指定数据源顺序返回实现了 get_option_chain 的 Provider。"""
    providers = []
    if specified_sources:
        splitter = getattr(data_manager, "_split_source_names", None)
        names = splitter(specified_sources) if callable(splitter) else [specified_sources]
        for name in names or []:
            getter = getattr(data_manager, "_provider_for_source", None)
            try:
                provider = getter(name) if callable(getter) else None
            except Exception:
                continue
            if provider is not None:
                providers.append(provider)
    else:
        all_providers = getattr(data_manager, "_all_provider_instances", None)
        if callable(all_providers):
            providers.extend(list(all_providers()) or [])
    explicit = getattr(data_manager, "_explicit_provider_method", None)
    result = []
    for provider in providers:
        method = None
        if callable(explicit):
            method = explicit(provider, "get_option_chain")
        elif callable(getattr(provider, "get_option_chain", None)):
            method = provider.get_option_chain
        if method is not None:
            result.append(provider)
    return result


def supports_historical_option_chain(data_manager, specified_sources=None) -> bool:
    """回测展开要求至少有一个 Provider 声明历史链能力。"""
    for provider in _iter_chain_providers(data_manager, specified_sources):
        if bool(getattr(provider, "HISTORICAL_OPTION_CHAIN", False)):
            return True
    return False


def _take_provider_count(data_manager, specified_sources, method_name):
    """汇总并清零 Provider 计数器；没有该接口时当 0。"""
    total = 0
    for provider in _iter_chain_providers(data_manager, specified_sources):
        getter = getattr(provider, method_name, None)
        if not callable(getter):
            continue
        try:
            total += int(getter() or 0)
        except Exception:
            continue
    return total


def _query_chain(data_manager, underlying, specified_sources, **kwargs):
    """查询标准化期权链；失败时返回 None，不把空结果当成空仓。"""
    getter = getattr(data_manager, "get_option_chain", None)
    if not callable(getter):
        return None
    try:
        signature = inspect.signature(getter)
    except (TypeError, ValueError):
        signature = None
    call_kwargs = dict(kwargs)
    if signature is not None and not any(
        item.kind == inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()
    ):
        call_kwargs = {
            key: value for key, value in call_kwargs.items() if key in signature.parameters
        }
    try:
        result = getter(underlying, specified_sources=specified_sources, **call_kwargs)
    except TypeError:
        try:
            result = getter(underlying, **call_kwargs)
        except Exception:
            return None
    except Exception:
        return None
    if result is None or not isinstance(result, pd.DataFrame) or result.empty:
        return None
    return result



def _query_as_of_selected(
    data_manager,
    underlying,
    specified_sources,
    snapshot,
    *,
    live,
    hints,
    filter_spec,
    snapshot_limit,
):
    """查询单个 as_of 快照并选出候选；empty/fail 在当前线程内取值。"""
    chain = _query_chain(
        data_manager,
        underlying,
        specified_sources,
        as_of=None if live else snapshot,
        normalized=True,
        **hints,
    )
    empty = _take_provider_count(
        data_manager, specified_sources, "take_empty_result_count"
    )
    fail = _take_provider_count(
        data_manager, specified_sources, "take_retryable_fail_count"
    )
    selected = None
    if chain is not None:
        selected = select_chain_candidates(
            chain,
            as_of=snapshot,
            spec=filter_spec,
            limit=snapshot_limit,
        )
    return selected, empty, fail


def _iter_completed(items, worker, max_workers=_FETCH_WORKERS):
    """完成一项就产出结果；单任务不建线程池。"""
    pending = list(items or [])
    if not pending:
        return
    if len(pending) == 1:
        yield worker(pending[0])
        return
    workers = max(1, min(int(max_workers), len(pending)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, item) for item in pending]
        for future in as_completed(futures):
            yield future.result()


def _snapshot_candidate_limit(params):
    """历史展开每个 as_of 只留少量邻近合约，避免前几周打满 256 上限。"""
    top_k = 1
    if isinstance(params, dict):
        raw = params.get("select_top_k", params.get("selectTopK", 1))
        try:
            number = int(raw)
        except (TypeError, ValueError, OverflowError):
            number = 1
        if number > 0:
            top_k = number
    per_snapshot = max(4, top_k * 3)
    return min(MAX_CANDIDATES_PER_SNAPSHOT, per_snapshot)


def _spread_union(symbols, cap):
    """按时间顺序均匀抽样，保证训练集和测试集都有合约。"""
    items = [str(item).strip() for item in symbols or [] if str(item).strip()]
    try:
        limit = int(cap)
    except (TypeError, ValueError, OverflowError):
        return items
    if limit <= 0 or len(items) <= limit:
        return items
    if limit == 1:
        return items[:1]
    chosen = []
    seen = set()
    last = len(items) - 1
    for index in range(limit):
        cursor = int(index * last / (limit - 1))
        while cursor in seen and cursor < last:
            cursor += 1
        if cursor in seen:
            continue
        seen.add(cursor)
        chosen.append(items[cursor])
    return chosen


def _chain_query_hints(spec):
    """把展开窗口转成 Provider 可忽略的链查询提示，缩小历史链载荷。"""
    types = spec.get("option_types") or frozenset()
    if types == {"PUT"}:
        right = "put"
    elif types == {"CALL"}:
        right = "call"
    else:
        right = "both"
    return {
        "min_dte": spec.get("min_dte"),
        "max_dte": spec.get("max_dte"),
        "right": right,
    }


def _as_of_key(snapshot):
    """把 as_of 规范成断点文件里的日期键。"""
    stamp = pd.Timestamp(snapshot)
    if pd.isna(stamp):
        return ""
    return stamp.strftime("%Y-%m-%d")


def _parse_as_of_day(value):
    """把缓存键解析成无时区的日历日；无法解析时返回 NaT。"""
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT
    if pd.isna(stamp):
        return pd.NaT
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.normalize()


def _cached_snapshot_for_as_of(
    cached_snapshots,
    as_of_text,
    step_days=DEFAULT_AS_OF_STEP_DAYS,
):
    """精确命中 as_of；否则复用步长内最近的已缓存快照，忽略 live。"""
    if not as_of_text or as_of_text == "live" or not cached_snapshots:
        return None
    if as_of_text in cached_snapshots:
        return cached_snapshots[as_of_text]
    target = _parse_as_of_day(as_of_text)
    if pd.isna(target):
        return None
    try:
        max_delta = max(1, int(step_days or DEFAULT_AS_OF_STEP_DAYS))
    except (TypeError, ValueError, OverflowError):
        max_delta = DEFAULT_AS_OF_STEP_DAYS
    best_key = None
    best_delta = None
    for key in cached_snapshots:
        if not key or key == "live":
            continue
        cached_day = _parse_as_of_day(key)
        if pd.isna(cached_day):
            continue
        delta = abs((cached_day - target).days)
        if delta >= max_delta:
            continue
        if (
            best_delta is None
            or delta < best_delta
            or (delta == best_delta and key < best_key)
        ):
            best_delta = delta
            best_key = key
    if best_key is None:
        return None
    return cached_snapshots[best_key]


def _checkpoint_enabled(live, refresh):
    """仅回测/优化且 CACHE_DATA 开启时续拉；refresh 全量重拉。"""
    if live or refresh:
        return False
    return bool(getattr(config, "CACHE_DATA", False))


def _checkpoint_parts(underlying, spec, snapshot_limit):
    """断点身份包含标的、权利、DTE、Delta 锚点和 k。"""
    types = spec.get("option_types") or frozenset()
    if types == {"PUT"}:
        right = "put"
    elif types == {"CALL"}:
        right = "call"
    else:
        right = "both"
    try:
        limit = 0 if snapshot_limit is None else int(snapshot_limit)
    except (TypeError, ValueError, OverflowError):
        limit = 0
    delta_parts = []
    for name in ("min_delta", "max_delta"):
        value = spec.get(name)
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            number = None
        if number is None or not pd.notna(number):
            delta_parts.append("na")
        else:
            token = f"{number:.4f}".rstrip("0").rstrip(".")
            delta_parts.append(token.replace("-", "m").replace(".", "p"))
    for value in spec.get("delta_targets") or ():
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if pd.notna(number):
            token = f"{number:.4f}".rstrip("0").rstrip(".")
            delta_parts.append(token.replace("-", "m").replace(".", "p"))
    try:
        oi_value = float(spec.get("min_open_interest", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        oi_value = 0.0
    oi_token = f"{max(0.0, oi_value):g}".replace(".", "p")
    delta_identity = "_".join(delta_parts) or "na"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(underlying or "").upper()) or "UNKNOWN"
    stem = (
        f"{safe}_{right}_dte{int(spec['min_dte'])}-{int(spec['max_dte'])}"
        f"_delta{delta_identity}_oi{oi_token}_k{limit}"
    )
    return safe, right, int(spec["min_dte"]), int(spec["max_dte"]), limit, stem


def _checkpoint_dir(data_manager):
    """option_universe 目录；父目录不存在时一并创建。"""
    data_path = getattr(data_manager, "data_path", None) or getattr(config, "DATA_PATH", ".data")
    folder = os.path.join(CsvDataProvider.ensure_cache_dir(data_path), "option_universe")
    os.makedirs(folder, exist_ok=True)
    return folder


def _checkpoint_path(data_manager, underlying, spec, snapshot_limit):
    """稳定断点路径，不随 start/end 平移。"""
    folder = _checkpoint_dir(data_manager)
    _safe, _right, _min_dte, _max_dte, _limit, stem = _checkpoint_parts(
        underlying, spec, snapshot_limit
    )
    return os.path.join(folder, f"{stem}.json")


def _legacy_checkpoint_paths(folder, underlying, spec, snapshot_limit):
    """兼容旧的带窗口日期文件名。"""
    safe, right, min_dte, max_dte, limit, _stem = _checkpoint_parts(
        underlying, spec, snapshot_limit
    )
    pattern = os.path.join(folder, f"{safe}_*_{right}_dte{min_dte}-{max_dte}_k{limit}.json")
    return sorted(glob.glob(pattern))


def _load_merged_checkpoints(path, data_manager, underlying, spec, snapshot_limit):
    """合并稳定断点与旧窗口文件中的 as_of。"""
    merged = {}
    folder = os.path.dirname(path) if path else _checkpoint_dir(data_manager)
    # 旧窗口文件名不包含 Delta 锚点；PCS 保护腿需要独立候选池，不能复用旧的
    # Short Put 中心 Delta 快照，否则会静默丢失保护腿合约。
    # 新版 spec 始终带 delta_targets 字段；旧窗口文件名不包含 Delta 身份，
    # 不能再把它们静默迁移为当前策略的候选池。
    if "delta_targets" not in spec:
        for candidate in _legacy_checkpoint_paths(folder, underlying, spec, snapshot_limit):
            merged.update(_load_checkpoint(candidate))
    merged.update(_load_checkpoint(path))
    return merged


def _load_checkpoint(path):
    """读取已成功的 as_of 候选列表；损坏或缺失时当作空断点。"""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
    if not isinstance(snapshots, dict):
        return {}
    cleaned = {}
    for key, symbols in snapshots.items():
        day = str(key or "").strip()
        if not day:
            continue
        cleaned[day] = [str(item).strip() for item in symbols or [] if str(item).strip()]
    return cleaned


def _save_checkpoint(path, snapshots):
    """原子写入 as_of 断点，避免中断留下半截 JSON。"""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    temp_path = f"{path}.tmp"
    payload = {"version": 1, "snapshots": snapshots}
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    os.replace(temp_path, path)


def _append_unique(union, seen, symbols, live=False):
    """把新合约追加到并集；实盘触及上限时返回 True。"""
    capped = False
    for raw in symbols or []:
        symbol = str(raw or "").strip()
        key = symbol.upper()
        if not key or key in seen:
            continue
        seen.add(key)
        union.append(symbol)
        if live and len(union) >= MAX_UNION_PER_UNDERLYING:
            capped = True
            break
    return capped


def expand_option_universe(
    symbols,
    *,
    strategy_class,
    params=None,
    data_manager=None,
    specified_sources=None,
    start_date=None,
    end_date=None,
    live=False,
    as_of=None,
    log=print,
    refresh=False,
):
    """把标的池展开为标的 + 候选期权。未 opt-in 时原样返回。"""
    source = [str(item).strip() for item in symbols or [] if str(item).strip()]
    spec = resolve_option_universe_spec(strategy_class, params)
    if spec is None:
        return source
    underlyings, explicit_options = split_symbol_pool(source)
    if not underlyings:
        return source
    if data_manager is None:
        raise ValueError("option universe expansion requires DataManager")
    if not live and not supports_historical_option_chain(data_manager, specified_sources):
        raise ValueError(
            "historical option universe requires theta or theta+futu; "
            "Futu current chain cannot be used as backtest as_of"
        )

    filter_spec = widen_universe_bounds(spec, live=bool(live))
    if not live:
        # 历史链的 OI 字段经常缺失或只在期权日线数据中可见；发现阶段
        # 不用交易过滤器截断候选，策略 next() 仍会按 min_open_interest
        # 过滤真实开仓，避免整个历史期权池变成空池。
        filter_spec["min_open_interest"] = 0.0
    if live:
        snapshot_times = [pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now()]
    else:
        # 窗口右端钳到美东日历前一天；周末仍由 historical_as_of_dates 跳过。
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        if pd.isna(start_ts) or pd.isna(end_ts):
            raise ValueError("historical option universe requires start_date and end_date")
        if start_ts.tzinfo is not None:
            start_ts = start_ts.tz_localize(None)
        if end_ts.tzinfo is not None:
            end_ts = end_ts.tz_localize(None)
        start_ts = start_ts.normalize()
        end_ts = end_ts.normalize()
        cutoff = pd.Timestamp(_incomplete_vendor_day())
        if end_ts >= cutoff:
            end_ts = cutoff - pd.Timedelta(days=1)
        snapshot_times = historical_as_of_dates(
            start_ts, end_ts, DEFAULT_AS_OF_STEP_DAYS
        )

    discovered = []
    failed_underlyings = []
    for underlying in underlyings:
        union = []
        seen = set()
        success = 0
        empty_total = 0
        fail_total = 0
        hints = _chain_query_hints(filter_spec)
        snapshot_limit = None if live else _snapshot_candidate_limit(params)
        total = len(snapshot_times)
        started = time.monotonic()
        last_progress = started
        checkpoint_path = None
        cached_snapshots = {}
        if _checkpoint_enabled(live, refresh):
            checkpoint_path = _checkpoint_path(
                data_manager,
                underlying,
                filter_spec,
                snapshot_limit,
            )
            cached_snapshots = _load_merged_checkpoints(
                checkpoint_path,
                data_manager,
                underlying,
                filter_spec,
                snapshot_limit,
            )
            if cached_snapshots and callable(log):
                log(
                    f"[OptionUniverse] {underlying}: resume "
                    f"{len(cached_snapshots)} cached as_of snapshots"
                )
        if callable(log):
            log(f"[OptionUniverse] {underlying}: start {total} as_of snapshots")
        selected_map = {}
        pending = []
        for snapshot in snapshot_times:
            as_of_text = "live" if live else _as_of_key(snapshot)
            cached_selected = _cached_snapshot_for_as_of(cached_snapshots, as_of_text)
            if cached_selected is not None:
                selected_map[as_of_text] = list(cached_selected)
                success += 1
            else:
                pending.append((snapshot, as_of_text))
        processed = success
        retry_keys = set()

        def handle_result(as_of_text, selected, empty, fail, allow_retry):
            nonlocal success, empty_total, fail_total, processed, last_progress
            empty_total += int(empty or 0)
            fail_total += int(fail or 0)
            processed += 1
            if selected is not None:
                success += 1
                selected_map[as_of_text] = list(selected)
                if checkpoint_path is not None:
                    cached_snapshots[as_of_text] = list(selected)
                    try:
                        _save_checkpoint(checkpoint_path, cached_snapshots)
                    except Exception as exc:
                        if callable(log):
                            log(f"[OptionUniverse] {underlying}: checkpoint save failed: {exc}")
            elif allow_retry and int(fail or 0) > 0 and not live:
                retry_keys.add(as_of_text)
            now = time.monotonic()
            if callable(log) and (
                processed == 1
                or processed == total
                or now - last_progress >= _PROGRESS_INTERVAL_SECONDS
            ):
                log(
                    f"[OptionUniverse] {underlying}: {processed}/{total} as_of={as_of_text} "
                    f"ok={success} empty={empty_total} fail={fail_total} "
                    f"contracts={sum(len(items) for items in selected_map.values())} "
                    f"elapsed={now - started:.0f}s"
                )
                last_progress = now

        def fetch_one(item):
            snapshot, as_of_text = item
            selected, empty, fail = _query_as_of_selected(
                data_manager,
                underlying,
                specified_sources,
                snapshot,
                live=live,
                hints=hints,
                filter_spec=filter_spec,
                snapshot_limit=snapshot_limit,
            )
            return as_of_text, selected, empty, fail

        for as_of_text, selected, empty, fail in _iter_completed(pending, fetch_one):
            handle_result(as_of_text, selected, empty, fail, allow_retry=True)

        for snapshot in snapshot_times:
            as_of_text = "live" if live else _as_of_key(snapshot)
            selected = selected_map.get(as_of_text)
            if selected is not None:
                _append_unique(union, seen, selected, live=live)
            if live and len(union) >= MAX_UNION_PER_UNDERLYING:
                if callable(log):
                    log(
                        f"[OptionUniverse] {underlying} reached {MAX_UNION_PER_UNDERLYING} "
                        "contract cap; extra strikes are ignored."
                    )
                break

        retry_snapshots = [
            snapshot
            for snapshot in snapshot_times
            if _as_of_key(snapshot) in retry_keys
        ]
        if retry_snapshots:
            if callable(log):
                log(
                    f"[OptionUniverse] {underlying}: retry {len(retry_snapshots)} "
                    f"failed as_of snapshots"
                )

            def retry_one(snapshot):
                as_of_text = _as_of_key(snapshot)
                selected, empty, fail = _query_as_of_selected(
                    data_manager,
                    underlying,
                    specified_sources,
                    snapshot,
                    live=False,
                    hints=hints,
                    filter_spec=filter_spec,
                    snapshot_limit=snapshot_limit,
                )
                return as_of_text, snapshot, selected, empty, fail

            retry_results = {}
            for as_of_text, snapshot, selected, empty, fail in _iter_completed(
                retry_snapshots, retry_one
            ):
                empty_total += int(empty or 0)
                fail_total += int(fail or 0)
                if selected is None:
                    continue
                success += 1
                retry_results[as_of_text] = list(selected)
                if checkpoint_path is not None:
                    cached_snapshots[as_of_text] = list(selected)
                    try:
                        _save_checkpoint(checkpoint_path, cached_snapshots)
                    except Exception as exc:
                        if callable(log):
                            log(f"[OptionUniverse] {underlying}: checkpoint save failed: {exc}")
            for snapshot in retry_snapshots:
                selected = retry_results.get(_as_of_key(snapshot))
                if selected is None:
                    continue
                _append_unique(union, seen, selected, live=live)
            if callable(log):
                log(
                    f"[OptionUniverse] {underlying}: retry done "
                    f"ok={success} empty={empty_total} fail={fail_total} "
                    f"contracts={len(union)}"
                )
        if not live and len(union) > HISTORICAL_MAX_UNION_PER_UNDERLYING:
            union = _spread_union(union, HISTORICAL_MAX_UNION_PER_UNDERLYING)
            if callable(log):
                log(
                    f"[OptionUniverse] {underlying} spread-capped to "
                    f"{HISTORICAL_MAX_UNION_PER_UNDERLYING} contracts across the window"
                )
        if success == 0:
            failed_underlyings.append(underlying)
            continue
        discovered.extend(union)
        if callable(log):
            log(
                f"[OptionUniverse] {underlying}: {success}/{len(snapshot_times)} "
                f"chain snapshots, {len(union)} contracts, "
                f"empty={empty_total} fail={fail_total}"
            )

    if failed_underlyings and not live:
        raise ValueError(
            "option universe expansion failed for "
            + ", ".join(failed_underlyings)
            + "; historical option chain is unavailable"
        )
    if failed_underlyings and callable(log):
        log(
            "[OptionUniverse] live chain unavailable for "
            + ", ".join(failed_underlyings)
            + "; existing feeds will be kept"
        )
    merged = merge_universe_symbols(underlyings, explicit_options, discovered)
    option_found = split_symbol_pool(merged)[1]
    if not live and not option_found:
        raise ValueError("option universe expansion produced no option contracts")
    if callable(log):
        log(f"[OptionUniverse] {len(source)} source symbols -> {len(merged)} tradable symbols")
    return merged


__all__ = [
    "expand_option_universe",
    "supports_historical_option_chain",
]
