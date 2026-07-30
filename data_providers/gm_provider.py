from datetime import datetime

import pandas as pd

try:
    from gm.api import history, set_token, current, set_serv_addr
except ImportError as e:
    print("Warning: 'gm' module not found. GmDataProvider will not be available. Error: {}".format(e))
    history = set_token = current = None

from .base_provider import BaseDataProvider
import config


class GmDataProvider(BaseDataProvider):
    """
    掘金(Goldminer) 数据源封装
    功能：
    1. 支持通过 Token 获取高质量历史数据。
    2. 支持自动粘合当天实时数据 (Current Tick -> Daily Bar)。
    3. 支持实盘/回测环境自动复用鉴权。
    """

    PRIORITY = 40

    def __init__(self, token=None):
        # 如果未传入，尝试从 config 读取默认值
        if token is None and hasattr(config, 'GM_TOKEN'):
            token = config.GM_TOKEN

        self.token = None
        self.is_external_mode = False

        # 1. 尝试解析并设置 Token (主动模式)
        if token and token != 'your_token_here|host:port':
            try:
                # 兼容 "token|host:port" 格式
                if '|' in token:
                    cfg_token, serv_addr = token.split("|", 1)
                    self.token = cfg_token
                    if set_token: set_token(cfg_token)
                    if serv_addr and set_serv_addr: set_serv_addr(serv_addr)
                else:
                    self.token = token
                    if set_token: set_token(token)

                # print(f"[GmDataProvider] Token set from config: {self.token[:4]}****")
            except Exception as e:
                print(f"[GmDataProvider] Error parsing config token: {e}")

        # 2. 如果没有有效配置，启用外部托管模式 (被动模式)
        #    这样 LiveTrader 在 engine.py 里无参实例化时，不会导致 Provider 失效
        if not self.token:
            # print("[GmDataProvider] No valid token in config. Assuming global set_token() is called externally.")
            self.token = "EXTERNAL_MODE"
            self.is_external_mode = True

    def _map_frequency(self, timeframe: str, compression: int) -> str:
        if timeframe == 'Days':
            return '1d'
        if timeframe == 'Minutes':
            return f"{compression * 60}s"
        if timeframe == 'Seconds':
            return f"{compression}s"
        return '1d'

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:

        if not self.token:
            return None
        
        try:
            freq = self._map_frequency(timeframe, compression)

            # 1. 获取历史数据
            if end_date is None:
                end_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            df = history(symbol=symbol, frequency=freq, start_time=start_date, end_time=end_date,
                         fields='open,high,low,close,volume,eob', adjust=1, df=True)

            # 2. 清洗历史数据
            if not df.empty:
                df.rename(columns={'eob': 'datetime'}, inplace=True)
                df['datetime'] = pd.to_datetime(df['datetime'])
                df.set_index('datetime', inplace=True)

                # 转为北京时间，归一化，然后剥离时区信息（变成 Naive，但时间值是北京时间）
                if df.index.tz is not None:
                    df.index = df.index.tz_convert('Asia/Shanghai')
                else:
                    df.index = df.index.tz_localize('UTC').tz_convert('Asia/Shanghai')

                # 仅日线归一化到 00:00；分钟线保留真实时间，避免分钟策略时间维失真。
                if freq == '1d':
                    df.index = df.index.normalize().tz_localize(None)
                else:
                    df.index = df.index.tz_localize(None)

            # 3. 粘合实时数据 (仅限日线)
            if freq == '1d':
                try:
                    df = self._stitch_realtime_bar(symbol, df)
                except Exception as e:
                    # 仅打印警告，不中断流程，返回已有的历史数据
                    print(f"[GmDataProvider] Warning: Real-time stitching failed for {symbol}: {e}")
                    pass

            if df.empty:
                return None

            # 4. 最终格式化
            df['openinterest'] = 0
            df = df[['open', 'high', 'low', 'close', 'volume', 'openinterest']]
            df.sort_index(inplace=True)

            return df

        except Exception as e:
            print(e)
            return None

    def _stitch_realtime_bar(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        获取实时 Tick 拼接到历史 DataFrame
        """
        ticks = current(symbols=symbol)
        if not ticks:
            return df

        tick = ticks[0]
        tick_price = tick['price']

        if tick_price <= 0:
            return df

        # 处理时间
        tick_time = pd.Timestamp(tick['created_at'])

        if tick_time.tzinfo is not None:
            tick_time = tick_time.tz_convert('Asia/Shanghai')
        else:
            tick_time = tick_time.tz_localize('UTC').tz_convert('Asia/Shanghai')

        # 日线归一化到 00:00:00
        bar_time = tick_time.normalize().tz_localize(None)

        # 判断是否需要拼接
        should_append = False
        if df.empty:
            should_append = True
        else:
            # 只有当 实时Bar时间 > 历史最后Bar时间 时才拼接
            # (避免收盘后 history 已经包含了今天的数据，导致重复)
            last_hist_time = df.index[-1]
            if bar_time > last_hist_time:
                should_append = True

        if should_append:
            # 构造今日 Bar
            open_p = tick['open'] if tick['open'] > 0 else tick_price
            high_p = tick['high'] if tick['high'] > 0 else tick_price
            low_p = tick['low'] if tick['low'] > 0 else tick_price

            today_bar = pd.DataFrame({
                'open': [open_p],
                'high': [high_p],
                'low': [low_p],
                'close': [tick_price],
                'volume': [tick['cum_volume']],
                'datetime': [bar_time]
            })
            today_bar.set_index('datetime', inplace=True)

            # 拼接
            if df.empty:
                df = today_bar
            else:
                df = pd.concat([df, today_bar])

            # print(f"[GmDataProvider] Stitched real-time bar for {symbol}")

        return df
