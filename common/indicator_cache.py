"""
Indicator registry and cache helpers for strategy runtime.

BaseStrategy owns the public strategy API. This module owns the mechanics for
normalizing indicator series, building fast backtest lookup dictionaries, and
sharing immutable indicator series across optimizer trials.
"""

import pandas as pd


def _ensure_registries(strategy):
    if not hasattr(strategy, '_indicator_registry'):
        strategy._indicator_registry = {}
        strategy._fast_dict_registry = {}


def _normalize_series_index(series):
    if hasattr(series.index, 'tz') and series.index.tz is not None:
        series.index = series.index.tz_localize(None)
    else:
        series.index = pd.to_datetime(series.index)
    return series


def register_indicator(strategy, data_name, indicator_name, series):
    _ensure_registries(strategy)

    if data_name not in strategy._indicator_registry:
        strategy._indicator_registry[data_name] = {}
        strategy._fast_dict_registry[data_name] = {}

    series = _normalize_series_index(series)
    strategy._indicator_registry[data_name][indicator_name] = series

    if getattr(strategy.broker, 'is_live', False):
        return

    cache = getattr(strategy.broker, 'indicator_cache', None)
    if isinstance(cache, dict):
        try:
            fast_key = (
                'fast_dict',
                data_name,
                indicator_name,
                id(series),
                len(series),
                series.index[0] if len(series.index) else None,
                series.index[-1] if len(series.index) else None,
            )
            fast_dict = cache.get(fast_key)
            if fast_dict is None:
                idx = [dt.to_pydatetime() for dt in series.index]
                fast_dict = dict(zip(idx, series.values))
                cache[fast_key] = fast_dict
            strategy._fast_dict_registry[data_name][indicator_name] = fast_dict
            return
        except Exception:
            pass

    idx = [dt.to_pydatetime() for dt in series.index]
    strategy._fast_dict_registry[data_name][indicator_name] = dict(zip(idx, series.values))


def get_cached_indicator_series(strategy, data, indicator_name, params_key, compute_func):
    cache = getattr(strategy.broker, 'indicator_cache', None)
    if getattr(strategy.broker, 'is_live', False) or not isinstance(cache, dict):
        return compute_func()

    dataframe = getattr(getattr(data, 'p', None), 'dataname', None)
    if not isinstance(dataframe, pd.DataFrame):
        return compute_func()

    index = dataframe.index
    cache_key = (
        'indicator_series',
        getattr(data, '_name', str(data)),
        id(dataframe),
        len(dataframe),
        index[0] if len(index) else None,
        index[-1] if len(index) else None,
        indicator_name,
    ) + tuple(params_key)

    series = cache.get(cache_key)
    if series is None:
        series = compute_func()
        cache[cache_key] = series
    return series


def get_indicator(strategy, data, indicator_name, current_dt):
    if not hasattr(strategy, '_indicator_registry'):
        return None

    data_name = data._name
    is_live_mode = getattr(strategy.broker, 'is_live', False) or not hasattr(data, 'datetime')

    if is_live_mode:
        series = strategy._indicator_registry.get(data_name, {}).get(indicator_name)
        if series is not None:
            return series.asof(current_dt)
        return None

    data_dt = data.datetime.datetime(0)
    if getattr(data_dt, 'tzinfo', None) is not None:
        data_dt = data_dt.replace(tzinfo=None)

    fast_dict = strategy._fast_dict_registry.get(data_name, {}).get(indicator_name)
    if fast_dict is not None:
        return fast_dict.get(data_dt)

    return None
