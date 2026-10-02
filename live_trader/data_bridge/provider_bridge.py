import pandas as pd

from data_providers.base_provider import BaseDataProvider
from data_providers.manager import DataManager


class _DataManagerProvider(BaseDataProvider):
    """
    Live 数据源桥接器:
    将 DataManager 的 get_data 适配成 LiveTrader 所需的 get_history 接口。"""

    def __init__(self, data_manager: DataManager, specified_sources: str = None):
        self._data_manager = data_manager
        self._specified_sources = specified_sources

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        return self._data_manager.get_data(
            symbol,
            start_date=start_date,
            end_date=end_date,
            specified_sources=self._specified_sources,
            timeframe=timeframe,
            compression=compression,
            refresh=False,
        )

    def get_history(self, symbol: str, start_date: str, end_date: str,
                    timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        return self.get_data(symbol, start_date, end_date, timeframe, compression)


class _DataManagerProxy:
    """
    DataManager 代理:
    为选股器等调用路径强制注入指定数据源。"""

    def __init__(self, data_manager: DataManager, specified_sources: str):
        self._data_manager = data_manager
        self._specified_sources = specified_sources

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 specified_sources: str = None, timeframe: str = 'Days',
                 compression: int = 1, refresh: bool = False) -> pd.DataFrame:
        enforced = specified_sources if specified_sources is not None else self._specified_sources
        return self._data_manager.get_data(
            symbol,
            start_date=start_date,
            end_date=end_date,
            specified_sources=enforced,
            timeframe=timeframe,
            compression=compression,
            refresh=refresh,
        )

    def __getattr__(self, name):
        return getattr(self._data_manager, name)
