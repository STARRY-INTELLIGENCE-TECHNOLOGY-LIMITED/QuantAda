from abc import ABC, abstractmethod

import pandas as pd


# 数据源瞬时失败的默认总尝试次数；作为模块常量，不另开配置项。
REQUEST_ATTEMPTS = 5


class BaseDataProvider(ABC):
    """
    数据提供者的抽象基类
    """

    # 定义一个类属性作为优先级，数值越小，优先级越高。
    # 默认值设为一个较大的数，确保未指定优先级的provider排在最后。
    PRIORITY = 100

    @abstractmethod
    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        """
        获取指定交易标的的历史行情数据

        :param symbol: 交易标的代码，例如：'SHSE.510300'
        :param start_date: 开始日期，例如：'2020101'
        :param end_date: 结束日期，例如：'20250101'
        :param timeframe: Backtrader的时间维度 (e.g., 'Days', 'Minutes', 'Seconds')
        :param compression: 周期 (e.g., 1, 30)
        :return: 标准化后的Pandas DataFrame，若获取失败则返回None。
        DataFrame必须包含['datetime', 'open', 'high', 'low', 'close', 'volume']
        且以'datetime'为索引
        """
        pass

    def close(self) -> None:
        """关闭 Provider 持有的连接；无状态 Provider 默认无需处理。"""
        return None
