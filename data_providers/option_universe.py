"""按策略声明把标的池展开为期权合约代码。

本模块只负责调用 DataManager.get_option_chain 并合并结果；回测必须使用带
HISTORICAL_OPTION_CHAIN 标记的历史链，实盘才允许当前快照。
"""

from __future__ import annotations

import inspect

import pandas as pd

from common.options.universe import (
    DEFAULT_AS_OF_STEP_DAYS,
    MAX_UNION_PER_UNDERLYING,
    historical_as_of_dates,
    merge_universe_symbols,
    resolve_option_universe_spec,
    select_chain_candidates,
    split_symbol_pool,
    widen_universe_bounds,
)


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
    if live:
        snapshot_times = [pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now()]
    else:
        snapshot_times = historical_as_of_dates(start_date, end_date, DEFAULT_AS_OF_STEP_DAYS)

    discovered = []
    failed_underlyings = []
    for underlying in underlyings:
        union = []
        seen = set()
        success = 0
        for snapshot in snapshot_times:
            chain = _query_chain(
                data_manager,
                underlying,
                specified_sources,
                as_of=None if live else snapshot,
                normalized=True,
            )
            if chain is None:
                continue
            success += 1
            for symbol in select_chain_candidates(chain, as_of=snapshot, spec=filter_spec):
                key = symbol.upper()
                if key in seen:
                    continue
                seen.add(key)
                union.append(symbol)
                if len(union) >= MAX_UNION_PER_UNDERLYING:
                    break
            if len(union) >= MAX_UNION_PER_UNDERLYING:
                if callable(log):
                    log(
                        f"[OptionUniverse] {underlying} reached {MAX_UNION_PER_UNDERLYING} "
                        "contract cap; extra strikes are ignored."
                    )
                break
        if success == 0:
            failed_underlyings.append(underlying)
            continue
        discovered.extend(union)
        if callable(log):
            log(
                f"[OptionUniverse] {underlying}: {success}/{len(snapshot_times)} "
                f"chain snapshots, {len(union)} contracts"
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
