import pandas as pd
import tushare as ts

import config
from .base_provider import BaseDataProvider


class TushareDataProvider(BaseDataProvider):
    """
    使用Tushare Pro获取数据
    """
    PRIORITY = 20

    def __init__(self, token: str = None):
        # 在实例化时解析默认值。``run.py`` 在导入提供者模块之后才应用
        # --config；如果使用 ``config.TUSHARE_TOKEN`` 作为函数默认参数，
        # 旧值会在导入时被冻结，从而静默忽略命令行覆盖。
        if token is None:
            token = getattr(config, 'TUSHARE_TOKEN', None)
        if not token or token == 'your_token_here':
            print("Warning: Tushare token is not configured. Tushare provider will be skipped.")
            self.pro = None
        else:
            ts.set_token(token)
            self.pro = ts.pro_api()

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        if not self.pro:
            return None

        if timeframe != 'Days' or compression != 1:
            print(f"Warning: TushareProvider (TushareDataProvider) current implementation only supports daily data. "
                  f"Ignoring timeframe/compression. Use 'pro.stk_mins' for intraday.")

        try:
            # 转换symbol格式, e.g., 'SHSE.600519' -> '600519.SH'
            parts = symbol.split('.')
            ts_symbol = f"{parts[1]}.{parts[0].replace('SHSE', 'SH').replace('SZSE', 'SZ')}"
            stock_code = parts[1]

            is_stock = len(stock_code) == 6 and not stock_code.startswith(('5', '15', '16', '18'))
            if is_stock:
                print(f"Tushare: Detected stock symbol '{ts_symbol}'. Using 'daily' and 'adj_factor' APIs.")
                df = self.pro.daily(ts_code=ts_symbol, start_date=start_date, end_date=end_date)
                adj_df = self.pro.adj_factor(ts_code=ts_symbol, start_date=start_date, end_date=end_date)
            else:
                print(f"Tushare: Detected fund symbol '{ts_symbol}'. Using 'fund_daily' and 'fund_adj' APIs.")
                df = self.pro.fund_daily(ts_code=ts_symbol, start_date=start_date, end_date=end_date)
                adj_df = self.pro.fund_adj(ts_code=ts_symbol, start_date=start_date, end_date=end_date)

            if df is None or df.empty:
                print(f"Tushare: No daily data found for {ts_symbol}.")
                return None

            df.rename(columns={'vol': 'volume'}, inplace=True)

            if adj_df is None or adj_df.empty:
                print(f"Warning: Tushare: No adjustment factor found for {ts_symbol}. Returning unadjusted data.")
            else:
                print(f"Tushare: Calculating backward adjusted prices for {ts_symbol}.")
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                adj_df['trade_date'] = pd.to_datetime(adj_df['trade_date'])

                df = pd.merge(df, adj_df[['trade_date', 'adj_factor']], on='trade_date', how='left')
                df.sort_values(by='trade_date', ascending=True, inplace=True)

                df['adj_factor'] = df['adj_factor'].ffill()
                df['adj_factor'] = df['adj_factor'].bfill()

                if 'adj_factor' in df.columns and not df['adj_factor'].isnull().all():
                    latest_adj_factor = df['adj_factor'].iloc[-1]
                    adj_ratio = df['adj_factor'] / latest_adj_factor

                    df['open'] = df['open'] * adj_ratio
                    df['high'] = df['high'] * adj_ratio
                    df['low'] = df['low'] * adj_ratio
                    df['close'] = df['close'] * adj_ratio
                    df['volume'] = df['volume'] / adj_ratio

                    df.drop(columns=['adj_factor'], inplace=True)
                else:
                    print(
                        f"Warning: Tushare: adj_factor calculation failed for {ts_symbol}. Returning unadjusted data.")

            df.rename(columns={'trade_date': 'datetime'}, inplace=True)
            df.set_index('datetime', inplace=True)
            df.sort_index(ascending=True, inplace=True)

            df = df[['open', 'high', 'low', 'close', 'volume']]
            df['openinterest'] = 0

            return df

        except Exception as e:
            print(f"Tushare data provider failed for {symbol}: {e}")
            return None
