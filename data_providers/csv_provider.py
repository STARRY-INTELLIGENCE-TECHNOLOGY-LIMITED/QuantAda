import os

import pandas as pd

import config
from .base_provider import BaseDataProvider


class CsvDataProvider(BaseDataProvider):
    """
    从本地CSV文件加载数据
    """
    PRIORITY = 10  # 最高优先级，先从本地CSV读取
    CACHE_SUBDIR = "market_cache"

    def __init__(self, data_path: str = None):
        # 在实例化时解析 DATA_PATH，确保命令行配置覆盖值生效；该模块
        # 可能早于命令行参数解析完成导入。
        if data_path is None:
            data_path = getattr(config, "DATA_PATH", ".data")
        self.data_path = data_path
        self.ensure_cache_dir(self.data_path)

    @staticmethod
    def resolve_cache_dir(data_path: str) -> str:
        """行情 CSV 目录；放在 DATA_PATH/market_cache，避免和日志混放。"""
        return os.path.join(str(data_path or ""), CsvDataProvider.CACHE_SUBDIR)

    @staticmethod
    def ensure_cache_dir(data_path: str) -> str:
        """创建 DATA_PATH 与 market_cache；父目录不存在时一并创建。"""
        cache_dir = CsvDataProvider.resolve_cache_dir(data_path)
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    @staticmethod
    def get_cache_filepath(data_path: str, symbol: str, timeframe: str, compression: int) -> str:
        """辅助函数：根据时间框架生成唯一的缓存文件名"""
        if timeframe == "Days" and compression == 1:
            tf_str = ""
        else:
            tf_str = f"_{timeframe}_{compression}"

        csv_filename = f"{symbol.replace('.', '_')}{tf_str}.csv"
        return os.path.join(CsvDataProvider.resolve_cache_dir(data_path), csv_filename)

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:

        csv_filepath = self.get_cache_filepath(self.data_path, symbol, timeframe, compression)

        if not os.path.exists(csv_filepath):
            return None

        try:
            df = pd.read_csv(csv_filepath, index_col="datetime", parse_dates=True)

            # 根据日期筛选
            if start_date:
                df = df[df.index >= pd.to_datetime(start_date)]
            if end_date:
                df = df[df.index <= pd.to_datetime(end_date)]

            return df
        except Exception as e:
            print(f"Error reading CSV file {csv_filepath}: {e}")
            return None
