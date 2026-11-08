import os
import re

import pandas as pd

import config
from common.data_paths import ensure_data_dir, resolve_data_path
from .base_provider import BaseDataProvider

# 缓存行的价格口径身份（来源 + 复权口径）；读取时剥离到 attrs['price_basis']，不进入策略行情。
PRICE_BASIS_COLUMN = "_price_basis"


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
            data_path = getattr(config, "DATA_PATH", ".data") or ".data"
        self.data_path = resolve_data_path(data_path)
        self.ensure_cache_dir(self.data_path)

    @staticmethod
    def resolve_cache_dir(data_path: str) -> str:
        """行情 CSV 目录；放在 DATA_PATH/market_cache，避免和日志混放。"""
        root = resolve_data_path(data_path)
        return os.path.join(root, CsvDataProvider.CACHE_SUBDIR)

    @staticmethod
    def ensure_cache_dir(data_path: str) -> str:
        """创建数据根与 market_cache，并尽量放开共享权限。"""
        root = resolve_data_path(data_path)
        ensure_data_dir(root)
        return ensure_data_dir(CsvDataProvider.resolve_cache_dir(root))

    @staticmethod
    def get_cache_filepath(data_path: str, symbol: str, timeframe: str, compression: int) -> str:
        """辅助函数：根据时间框架生成唯一的缓存文件名"""
        if timeframe == "Days" and compression == 1:
            tf_str = ""
        else:
            safe_timeframe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(timeframe))
            tf_str = f"_{safe_timeframe}_{compression}"

        csv_filename = f"{symbol.replace('.', '_')}{tf_str}.csv"
        return os.path.join(CsvDataProvider.resolve_cache_dir(data_path), csv_filename)

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = "Days", compression: int = 1) -> pd.DataFrame:

        csv_filepath = self.get_cache_filepath(self.data_path, symbol, timeframe, compression)

        if not os.path.exists(csv_filepath):
            return None

        try:
            df = pd.read_csv(csv_filepath, index_col="datetime", parse_dates=True)

            # 口径必须在按窗口切片前按整个文件判定，否则混合口径文件在只含单一口径的窗口里会被误判有效。
            price_basis = None
            if PRICE_BASIS_COLUMN in df.columns:
                values = df[PRICE_BASIS_COLUMN].dropna().astype(str).unique()
                price_basis = values[0] if len(values) == 1 and df[PRICE_BASIS_COLUMN].notna().all() else None
                df = df.drop(columns=[PRICE_BASIS_COLUMN])

            # 根据日期筛选
            if start_date:
                df = df[df.index >= pd.to_datetime(start_date)]
            if end_date:
                df = df[df.index <= pd.to_datetime(end_date)]

            # 缺少标记的旧缓存或混合口径记为 None，由 DataManager 视为未知口径而重取。
            df.attrs["price_basis"] = price_basis
            return df
        except Exception as e:
            print(f"Error reading CSV file {csv_filepath}: {e}")
            return None
