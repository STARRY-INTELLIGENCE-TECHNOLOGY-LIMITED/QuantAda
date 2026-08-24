from functools import partial

import pandas as pd
import requests

import config
from .base_provider import BaseDataProvider

"""
基于山西证券tushare数据源代理
https://github.com/ActiveIce/sxscts
"""


class SxscTushareDataProvider(BaseDataProvider):
    PRIORITY = 30

    __http_url = 'http://221.204.19.233:7172'
    __timeout = 30  # 增加一个默认的请求超时时间

    def __init__(self, token: str = None):
        # 在实例化提供者时解析 --config 覆盖值，而不是在 run.py 导入
        # 本模块时提前固定默认值。
        if token is None:
            token = getattr(config, 'SXSC_TUSHARE_TOKEN', None)
        if not token or token == 'your_token_here':
            print("Warning: SXSC Tushare token is not configured. SxscTushareProvider will be skipped.")
            self.token = None
        else:
            self.token = token

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        """
        使用 sxsc-tushare 代理的 query 方法获取股票或ETF数据。
        """
        if not self.token:
            return None

        if timeframe != 'Days' or compression != 1:
            print(f"Warning: SxscTushareDataProvider current implementation only supports daily data. "
                  f"Ignoring timeframe/compression. Intraday API 'daily_mins' not implemented.")

        try:
            # 1. 转换symbol格式, e.g., 'SHSE.600519' -> '600519.SH'
            parts = symbol.split('.')
            ts_code = f"{parts[1]}.{parts[0].replace('SHSE', 'SH').replace('SZSE', 'SZ')}"
            stock_code = parts[1]

            # 统一需要的字段
            fields_to_get = 'ts_code,trade_date,open,high,low,close,vol'

            # 2. 识别股票或ETF，并调用相应的API获取日线和复权因子
            is_stock = len(stock_code) == 6 and not stock_code.startswith(('5', '15', '16', '18'))
            if is_stock:
                print(f"SxscTushareProvider: Detected stock symbol '{ts_code}'. Using 'daily' and 'adj_factor' APIs.")
                # 获取日线数据
                df = self.daily(ts_code=ts_code,
                                start_date=start_date,
                                end_date=end_date,
                                fields=fields_to_get)
                # 获取复权因子
                adj_df = self.adj_factor(ts_code=ts_code,
                                         start_date=start_date,
                                         end_date=end_date,
                                         fields='ts_code,trade_date,adj_factor')
            else:
                print(
                    f"SxscTushareProvider: Detected fund/ETF symbol '{ts_code}'. Using 'fund_daily' and 'fund_adj' APIs.")
                # 获取日线数据
                df = self.fund_daily(ts_code=ts_code,
                                     start_date=start_date,
                                     end_date=end_date,
                                     fields=fields_to_get)
                # 获取复权因子
                adj_df = self.fund_adj(ts_code=ts_code,
                                       start_date=start_date,
                                       end_date=end_date,
                                       fields='ts_code,trade_date,adj_factor')

            if df is None or df.empty:
                print(f"SxscTushareProvider: No daily data found for {ts_code}.")
                return None

            # --- 后复权计算逻辑 ---
            df.rename(columns={'vol': 'volume'}, inplace=True)
            if adj_df is None or adj_df.empty:
                print(
                    f"Warning: SxscTushareProvider: No adjustment factor found for {ts_code}. Returning unadjusted data.")
            else:
                print(f"SxscTushareProvider: Calculating backward adjusted prices for {ts_code}.")
                # 3.1 数据预处理
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                adj_df['trade_date'] = pd.to_datetime(adj_df['trade_date'])

                # 3.2 合并日线数据和复权因子
                df = pd.merge(df, adj_df[['trade_date', 'adj_factor']], on='trade_date', how='left')
                df.set_index('trade_date', inplace=True)
                df.sort_index(ascending=True, inplace=True)  # 确保按时间升序

                # 3.3 填充复权因子（复权因子在非除权日不会变，所以用前一个交易日的数据填充）
                df['adj_factor'] = df['adj_factor'].ffill()
                df['adj_factor'] = df['adj_factor'].bfill()

                # 3.4 计算后复权价格和成交量
                if 'adj_factor' in df.columns and not df['adj_factor'].isnull().all():
                    # 获取最新复权因子
                    latest_adj_factor = df['adj_factor'].iloc[-1]
                    # 计算复权比例
                    adj_ratio = df['adj_factor'] / latest_adj_factor

                    # 应用复权
                    df['open'] = df['open'] * adj_ratio
                    df['high'] = df['high'] * adj_ratio
                    df['low'] = df['low'] * adj_ratio
                    df['close'] = df['close'] * adj_ratio
                    # 成交量是反向复权
                    df['volume'] = df['volume'] / adj_ratio

                    # 删除辅助列
                    df.drop(columns=['adj_factor'], inplace=True)
                else:
                    print(
                        f"Warning: SxscTushareProvider: adj_factor calculation failed for {ts_code}. Returning unadjusted data.")
                    df.set_index('trade_date', inplace=True)  # 如果复权失败，也要设置索引

            # 4. 数据标准化
            df.index.name = 'datetime'  # 重命名索引

            # 如果上面复权失败，需要在这里设置索引
            if not isinstance(df.index, pd.DatetimeIndex):
                df['datetime'] = pd.to_datetime(df['trade_date'])
                df.set_index('datetime', inplace=True)
                df.sort_index(ascending=True, inplace=True)

            df = df[['open', 'high', 'low', 'close', 'volume']]
            df['openinterest'] = 0

            return df

        except Exception as e:
            print(f"SxscTushareProvider data provider failed for {symbol}: {e}")
            return None

    def query(self, api_name, fields='', **kwargs):
        """ 走通用API配置接口 """
        data = {
            'api_name': api_name,
            'token': self.token,  # 修正：使用 self.token
            'params': kwargs,
            'fields': fields
        }
        url = self.__http_url
        resp = requests.post(url, json=data, timeout=self.__timeout, headers={'Connection': 'close'})

        if resp.status_code != 200:
            raise Exception(f"HTTP Error {resp.status_code}: {resp.text}")

        result = resp.json()
        if result['code'] != 0:
            raise Exception(result['msg'])

        result_data = result['data']
        columns = result_data['fields']
        items = result_data['items']
        return pd.DataFrame(items, columns=columns)

    def __getattr__(self, name):
        """
        一个方便的语法糖，允许调用 self.daily(...) 来代替 self.query('daily', ...)。
        """
        return partial(self.query, name)
