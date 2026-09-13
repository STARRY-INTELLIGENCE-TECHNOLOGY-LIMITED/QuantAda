"""通用历史数据与实时数据叠加骨架。

本模块不继承 BaseDataProvider，避免被 DataManager 当作可直接实例化的数据源；
具体组合只需注入历史 Provider、实时 Provider、代码映射和合并函数。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable

import pandas as pd

from common.options.data_safety import sanitize_market_dataframe


class OverlayDataProvider:
    """将历史 Provider 与可选实时 Provider 组合成一个有界叠加层。"""

    def __init__(
        self,
        historical_provider,
        realtime_provider=None,
        *,
        symbol_mapper: Callable[[str], tuple[str, str]] | None = None,
        merge_realtime: Callable[..., pd.DataFrame | None] | None = None,
        cache_size: int = 64,
    ) -> None:
        self.historical_provider = historical_provider
        self.realtime_provider = realtime_provider
        self.symbol_mapper = symbol_mapper or (lambda symbol: (symbol, symbol))
        self.merge_realtime = merge_realtime
        try:
            self.cache_size = max(1, int(cache_size))
        except (TypeError, ValueError, OverflowError):
            self.cache_size = 64
        self.live_mode = False
        self._cache = OrderedDict()

    def set_live_mode(self, enabled: bool) -> None:
        """切换是否叠加实时 Provider。"""

        self.live_mode = bool(enabled)

    def get_data(self, symbol, start_date=None, end_date=None,
                 timeframe="Days", compression=1, refresh=False):
        """读取历史数据；实盘模式下调用注入的实时合并函数。"""

        historical_symbol, realtime_symbol = self.symbol_mapper(str(symbol or ""))
        try:
            compression_key = int(compression or 1)
        except (TypeError, ValueError, OverflowError):
            compression_key = str(compression or "")
        key = (
            historical_symbol, str(start_date or ""), str(end_date or ""),
            str(timeframe or ""), compression_key,
        )
        historical = None if refresh else self._cache.get(key)
        if historical is None:
            historical = self.historical_provider.get_data(
                historical_symbol, start_date, end_date, timeframe, compression
            )
            historical = sanitize_market_dataframe(historical, require_ohlcv=True)
            if historical is not None and not historical.empty:
                self._cache[key] = historical.copy()
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        else:
            historical = historical.copy()
        if historical is None or historical.empty:
            return None
        if not self.live_mode or self.merge_realtime is None:
            return historical
        return self.merge_realtime(
            historical,
            symbol=str(symbol or ""),
            timeframe=timeframe,
            realtime_provider=self.realtime_provider,
            realtime_symbol=realtime_symbol,
        )

    def bind_realtime_provider(self, realtime_provider) -> None:
        """替换实时 Provider；用于把 Broker 已有会话注入组合层。"""

        self.realtime_provider = realtime_provider

    def close(self) -> None:
        """关闭组合层自行持有的 Provider。"""

        seen = set()
        for provider in (self.historical_provider, self.realtime_provider):
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            closer = getattr(provider, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


__all__ = ["OverlayDataProvider"]
