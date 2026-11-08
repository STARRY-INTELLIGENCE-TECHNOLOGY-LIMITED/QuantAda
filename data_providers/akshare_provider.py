import akshare as ak
import pandas as pd

from .base_provider import BaseDataProvider


class AkshareDataProvider(BaseDataProvider):

    PRIORITY = 80

    def _map_akshare_period(self, timeframe: str, compression: int) -> str:
        """将Backtrader的时间框架映射到Akshare的period参数"""
        native = {'Days': 'daily', 'Weeks': 'weekly', 'Months': 'monthly'}
        if timeframe in native:
            if str(compression) != '1':
                raise ValueError(f"Akshare {timeframe} requires compression=1, got {compression}")
            return native[timeframe]
        if timeframe == 'Minutes':
            # Akshare 支持 '1', '5', '15', '30', '60'
            supported_compressions = [1, 5, 15, 30, 60]
            if compression in supported_compressions:
                return str(compression)
            else:
                raise ValueError(f"Akshare Minutes requires compression in {supported_compressions}, got {compression}")

        # 不支持的周期必须失败，让责任链回退，不能把日线冒充为其它周期写入缓存。
        raise ValueError(f"AkshareProvider: Unsupported timeframe {timeframe}")

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        try:
            # 1. 对symbol进行更健壮的解析
            if '.' in symbol:
                ak_symbol = symbol.split('.')[1]
            else:
                ak_symbol = symbol

            period = self._map_akshare_period(timeframe, compression)

            if timeframe == 'Minutes':
                start_text = str(start_date or '1979-09-01 09:30:00')
                end_text = str(end_date or '2222-01-01 15:00:00')
                if len(start_text) == 8 and start_text.isdigit():
                    start_text = pd.Timestamp(start_text).strftime('%Y-%m-%d 09:30:00')
                if len(end_text) == 8 and end_text.isdigit():
                    end_text = pd.Timestamp(end_text).strftime('%Y-%m-%d 15:00:00')
                if ak_symbol.startswith(('6', '0', '3', '8', '4')):
                    minute_method = ak.stock_zh_a_hist_min_em
                else:
                    minute_method = ak.fund_etf_hist_min_em
                df = minute_method(
                    symbol=ak_symbol,
                    start_date=start_text,
                    end_date=end_text,
                    period=period,
                    adjust='qfq',
                )
                if df is None or df.empty:
                    return None
                df.rename(columns={
                    '时间': 'datetime', '开盘': 'open', '最高': 'high',
                    '最低': 'low', '收盘': 'close', '成交量': 'volume',
                }, inplace=True)
                if 'datetime' not in df.columns:
                    return None
                df['datetime'] = pd.to_datetime(df['datetime'])
                df.set_index('datetime', inplace=True)
                required = ['open', 'high', 'low', 'close', 'volume']
                if any(column not in df.columns for column in required):
                    return None
                result = df[required].apply(pd.to_numeric, errors='coerce').dropna()
                if result[['open', 'high', 'low', 'close']].le(0).any().any():
                    print(f"[Akshare] {symbol}: invalid intraday OHLC; cannot build reliable {period}-minute bars.")
                    return None
                result['openinterest'] = 0
                result.attrs['intraday_source'] = 'akshare_hist_min_em'
                return result[~result.index.duplicated(keep='last')].sort_index()

            # 2. 优雅地构建API参数字典
            # 仅当start_date或end_date不为None时，才将其添加到参数字典中
            api_params = {
                'symbol': ak_symbol,
                'period': period,
                'adjust': 'qfq'
            }
            if start_date:
                api_params['start_date'] = start_date
            if end_date:
                api_params['end_date'] = end_date

            # 3. 使用字典解包（**）调用akshare函数
            if ak_symbol.startswith(('6', '0', '3', '8', '4')):
                print(f"Akshare: Detected stock symbol '{ak_symbol}'. Using 'stock_zh_a_hist'.")
                df = ak.stock_zh_a_hist(**api_params)
            else:
                print(f"Akshare: Detected ETF/fund symbol '{ak_symbol}'. Using 'fund_etf_hist_em'.")
                # fund_etf_hist_em 不支持 period 参数, 这是一个限制
                if period != 'daily':
                    # 不能用日线冒充周/月线写入缓存；返回 None 让责任链回退。
                    print(f"Akshare 'fund_etf_hist_em' path only supports daily; skipping {timeframe}.")
                    return None
                df = ak.fund_etf_hist_em(**api_params)

            if df is None or df.empty:
                return None

            df.rename(columns={
                '日期': 'datetime',
                '开盘': 'open',
                '最高': 'high',
                '最低': 'low',
                '收盘': 'close',
                '成交量': 'volume'
            }, inplace=True)

            df['datetime'] = pd.to_datetime(df['datetime'])
            df.set_index('datetime', inplace=True)

            df = df[['open', 'high', 'low', 'close', 'volume']]
            df['openinterest'] = 0

            return df.sort_index(ascending=True)

        except Exception as e:
            print(f"Akshare data provider failed for {symbol}: {e}")
            return None
