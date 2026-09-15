"""策略运行时的指标注册表和缓存辅助函数。

BaseStrategy 负责公开的策略 API。本模块负责规范化指标序列、构造快速回测
查询字典，并在优化试验之间共享不可变的指标序列。
"""

from collections import OrderedDict
from bisect import bisect_right
import math

import pandas as pd

from common.options.data_safety import sanitize_indicator_series


class BoundedIndicatorCache(OrderedDict):
    """
    仅供优化器使用的指标序列和查询字典 LRU 缓存。

    优化试验可能生成大量参数专属序列。普通字典会随试验次数增长，
    即使源 OHLCV 数据很小，长时间训练仍可能耗尽内存。
    """

    DEFAULT_MAX_ENTRIES = 512

    def __init__(self, max_entries=None):
        super().__init__()
        try:
            max_entries = int(max_entries or self.DEFAULT_MAX_ENTRIES)
        except (TypeError, ValueError):
            max_entries = self.DEFAULT_MAX_ENTRIES
        self.max_entries = max(1, max_entries)

    def get(self, key, default=None):
        try:
            value = super().__getitem__(key)
        except KeyError:
            return default
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        exists = key in self
        super().__setitem__(key, value)
        if exists:
            self.move_to_end(key)
        while len(self) > self.max_entries:
            self.popitem(last=False)


def _ensure_registries(strategy):
    if not hasattr(strategy, '_indicator_registry'):
        strategy._indicator_registry = {}
    if not hasattr(strategy, '_fast_dict_registry'):
        strategy._fast_dict_registry = {}
    if not hasattr(strategy, '_fast_dict_keys_registry'):
        strategy._fast_dict_keys_registry = {}


def _normalize_series_index(series, preserve_timezone=False):
    if hasattr(series.index, 'tz') and series.index.tz is not None:
        if not preserve_timezone:
            series.index = series.index.tz_localize(None)
        else:
            series.index = pd.to_datetime(series.index)
    else:
        series.index = pd.to_datetime(series.index)
    return series


def register_indicator(strategy, data_name, indicator_name, series):
    _ensure_registries(strategy)

    if data_name not in strategy._indicator_registry:
        strategy._indicator_registry[data_name] = {}
        strategy._fast_dict_registry[data_name] = {}
        strategy._fast_dict_keys_registry[data_name] = {}
    else:
        strategy._fast_dict_registry.setdefault(data_name, {})
        strategy._fast_dict_keys_registry.setdefault(data_name, {})

    series = sanitize_indicator_series(series)
    series = _normalize_series_index(
        series,
        preserve_timezone=bool(getattr(strategy.broker, 'is_live', False)),
    )
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
            strategy._fast_dict_keys_registry[data_name][indicator_name] = tuple(sorted(fast_dict))
            return
        except Exception:
            pass

    idx = [dt.to_pydatetime() for dt in series.index]
    strategy._fast_dict_registry[data_name][indicator_name] = dict(zip(idx, series.values))
    strategy._fast_dict_keys_registry[data_name][indicator_name] = tuple(idx)


def get_cached_indicator_series(strategy, data, indicator_name, params_key, compute_func):
    cache = getattr(strategy.broker, 'indicator_cache', None)
    if getattr(strategy.broker, 'is_live', False) or not isinstance(cache, dict):
        return sanitize_indicator_series(compute_func())

    dataframe = getattr(getattr(data, 'p', None), 'dataname', None)
    if not isinstance(dataframe, pd.DataFrame):
        return sanitize_indicator_series(compute_func())

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
        series = sanitize_indicator_series(series)
        cache[cache_key] = series
    else:
        # 命中缓存时保留对象身份；仅在发现非有限值时替换，避免优化器
        # 每次查询都复制整条序列。
        try:
            numeric = pd.to_numeric(series, errors='coerce')
            if not numeric.map(math.isfinite).all():
                series = sanitize_indicator_series(series)
                cache[cache_key] = series
        except Exception:
            series = sanitize_indicator_series(series)
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
            if getattr(series, 'empty', False):
                return None
            # 将查询时间归一化到指标序列的时区；实盘序列保留时区信息，无时区序列继续使用原有的本地墙上时间语义，不截断秒级以下精度。
            try:
                lookup_dt = pd.Timestamp(current_dt)
                series_tz = getattr(getattr(series, 'index', None), 'tz', None)
                if series_tz is not None:
                    if lookup_dt.tzinfo is None:
                        lookup_dt = lookup_dt.tz_localize(series_tz)
                    else:
                        lookup_dt = lookup_dt.tz_convert(series_tz)
                elif lookup_dt.tzinfo is not None:
                    lookup_dt = lookup_dt.tz_convert(None)
            except (TypeError, ValueError, OverflowError):
                lookup_dt = current_dt
            return series.asof(lookup_dt)
        return None

    data_dt = data.datetime.datetime(0)
    if getattr(data_dt, 'tzinfo', None) is not None:
        data_dt = data_dt.replace(tzinfo=None)

    fast_dict = strategy._fast_dict_registry.get(data_name, {}).get(indicator_name)
    if fast_dict is not None:
        sentinel = object()
        value = fast_dict.get(data_dt, sentinel)
        if value is not sentinel:
            return value
        # 与实盘 Series.asof 保持一致：当前 bar 缺少精确指标时回退到
        # 之前最近的一条有限值，而不是因 fast dict 查询返回 None。
        keys = getattr(strategy, '_fast_dict_keys_registry', {}).get(data_name, {}).get(indicator_name)
        if keys:
            try:
                position = bisect_right(keys, data_dt) - 1
            except TypeError:
                return None
            if position >= 0:
                return fast_dict[keys[position]]
            return None
        try:
            previous = [key for key in fast_dict if key <= data_dt]
        except TypeError:
            return None
        if previous:
            return fast_dict[max(previous)]
        return None

    return None
