"""把已结束的日内行情转换为固定截止时刻的日频历史。"""

import pandas as pd

import config
from common.timeframe import aggregate_daily_cutoff, daily_cutoff_end_date
from .csv_provider import CsvDataProvider


_MAX_BOUNDARY_GAP_DAYS = 3


def load_daily_cutoff(manager, symbol, start_date, end_date, specified_sources, cutoff, compression, refresh):
    """复用独立日频缓存，缺口时取分钟 OHLCV；实盘仍在线核验行情。"""
    if compression != 1:
        raise ValueError("Day:HH:MM[:SS] requires compression=1")
    timeframe = f"Day:{cutoff:%H:%M:%S}"
    minute_end = daily_cutoff_end_date(end_date, cutoff)
    visible_end = pd.Timestamp(minute_end)
    last_session = visible_end.normalize()
    if visible_end.time() < cutoff:
        last_session -= pd.Timedelta(days=1)
    if last_session.tzinfo:
        last_session = last_session.tz_localize(None)
    first_session = pd.Timestamp(start_date).normalize() if start_date else None
    if first_session is not None and first_session.tzinfo:
        first_session = first_session.tz_localize(None)
    if first_session is not None and first_session > last_session:
        return None

    cache_root = manager._cache_root_for_source(specified_sources)
    source_names = manager._split_source_names(specified_sources)
    single_source = len(source_names) == 1
    aggregated = None
    origin = "fetch"
    if source_names == ["csv"]:
        aggregated = CsvDataProvider(cache_root).get_data(symbol, timeframe=timeframe, compression=1)
        origin = "cache"
    elif single_source and not getattr(manager, "_live_mode", False):
        cache_start = first_session
        while cache_start is not None and cache_start.weekday() >= 5:
            cache_start += pd.Timedelta(days=1)
        cache_end = last_session
        while cache_end.weekday() >= 5:
            cache_end -= pd.Timedelta(days=1)
        aggregated = manager._load_complete_cache(
            symbol, cache_start, cache_end, timeframe, 1, refresh,
            cache_root=cache_root, price_basis=manager._source_price_basis(specified_sources),
        )
        if aggregated is not None and not aggregated.empty:
            origin = "cache"

    generated = aggregated is None or aggregated.empty
    if generated:
        # cutoff 落在 5 分钟边界时，原生 5 分钟 K 的聚合与 1 分钟 K 等价，且数据量更小。
        can_use_five_minute = (
            cutoff.second == 0
            and cutoff.minute % 5 == 0
            and (cutoff.hour > 9 or (cutoff.hour == 9 and cutoff.minute >= 35))
        )
        minute_compression = 5 if can_use_five_minute else 1
        minute_frame = manager.get_data(
            symbol,
            start_date=first_session.strftime('%Y-%m-%d') if first_session is not None else None,
            end_date=minute_end,
            specified_sources=specified_sources,
            timeframe="Minutes",
            compression=minute_compression,
            refresh=refresh,
        )
        if minute_frame is None or minute_frame.empty:
            return None
        raw_index = minute_frame.index.tz_localize(None) if minute_frame.index.tz is not None else minute_frame.index
        raw_start = raw_index.min().normalize()
        raw_end = raw_index.max().normalize()
        if first_session is not None and (raw_start - first_session).days > _MAX_BOUNDARY_GAP_DAYS:
            print(
                f"[DataManager] {symbol}: intraday history starts at {raw_start.date()}, "
                f"later than requested {first_session.date()}; refusing incomplete Day cutoff data."
            )
            return None
        if (last_session - raw_end).days > _MAX_BOUNDARY_GAP_DAYS:
            print(
                f"[DataManager] {symbol}: intraday history ends at {raw_end.date()}, "
                f"earlier than requested {last_session.date()}; refusing incomplete Day cutoff data."
            )
            return None
        if (raw_index == raw_index.normalize()).all():
            print(f"[DataManager] {symbol}: Day:HH:MM[:SS] requires intraday OHLCV; daily fallback rejected.")
            return None
        aggregated = aggregate_daily_cutoff(minute_frame, cutoff)
        origin = minute_frame.attrs.get("data_origin", "fetch")
        price_basis = minute_frame.attrs.get("price_basis")
    if aggregated is None or aggregated.empty:
        return None
    local_index = aggregated.index.tz_localize(None) if aggregated.index.tz is not None else aggregated.index
    mask = local_index <= last_session
    if first_session is not None:
        mask &= local_index >= first_session
    aggregated = aggregated.loc[mask].copy()
    if aggregated.empty:
        return None
    aggregated.attrs.update({
        "data_origin": origin, "timeframe": timeframe, "partial_day_cutoff": f"{cutoff:%H:%M:%S}",
    })
    if single_source and generated and getattr(config, "CACHE_DATA", False) and not getattr(manager, "_live_mode", False):
        manager._cache_data(aggregated, symbol, timeframe, 1, cache_root=cache_root, price_basis=price_basis)
    return aggregated
