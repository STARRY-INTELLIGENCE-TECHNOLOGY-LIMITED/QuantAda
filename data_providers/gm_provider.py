from datetime import datetime
import os
import time

# GM SDK 的旧版 protobuf 生成代码与新版 C++ 实现不兼容；历史行情仍可用纯 Python
# 实现运行。该环境变量必须在导入 gm.api 前设置。
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')

import pandas as pd
from common.live_runtime import dependency_install_hint

try:
    from gm.api import history, set_token, current, set_serv_addr
    try:
        from gm.api import set_option
    except ImportError:
        set_option = None
except Exception as e:
    print(dependency_install_hint('gm', e))
    history = set_token = current = set_serv_addr = set_option = None

from .base_provider import BaseDataProvider, ProviderConfigError, REQUEST_ATTEMPTS
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

    @staticmethod
    def price_adjustment():
        """读取并校验 GM 历史行情复权口径；DataManager 也用它区分缓存价格口径。"""
        raw = getattr(config, 'GM_PRICE_ADJUSTMENT', 1)
        try:
            adjustment = int(raw)
        except (TypeError, ValueError, OverflowError):
            adjustment = None
        if adjustment not in {0, 1, 2}:
            raise ProviderConfigError(f'GM_PRICE_ADJUSTMENT must be 0, 1 or 2, got {raw!r}')
        return adjustment

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

                # 可按需打印从 config 读取的 token 前缀。
            except Exception as e:
                print(f"[GmDataProvider] Error parsing config token: {e}")

        # 2. 如果没有有效配置，启用外部托管模式 (被动模式)
        #    这样 LiveTrader 在 engine.py 里无参实例化时，不会导致 Provider 失效
        if not self.token:
            # 可按需打印：config 没有有效 token，默认由外部全局 set_token() 提供。
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

        # 复权口径是配置错误，必须在宽泛 try 之外校验，避免被当成“无数据”。
        price_adjustment = self.price_adjustment()

        try:
            freq = self._map_frequency(timeframe, compression)

            # 1. 获取历史数据
            if end_date is None:
                end_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if timeframe in {'Minutes', 'Seconds'} and start_date:
                # 长窗口分段取数，避免 SDK 单次截断；每段复权终点保持一致。
                if set_option:
                    set_option(max_wait_time=1000 if timeframe == 'Seconds' else 10000)
                request_start = pd.Timestamp(start_date)
                request_end = pd.Timestamp(end_date)
                if request_start.tzinfo is not None:
                    request_start = request_start.tz_convert('Asia/Shanghai').tz_localize(None)
                if request_end.tzinfo is not None:
                    request_end = request_end.tz_convert('Asia/Shanghai').tz_localize(None)
                if len(str(end_date).strip()) <= 10:
                    request_end += pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
                frames = []
                cursor = request_start
                # 秒 K 的时间块按周期缩小，单块不扩成整年高频明细。
                chunk_span = pd.Timedelta(days=14) if timeframe == 'Minutes' else pd.Timedelta(hours=1)
                while cursor <= request_end:
                    chunk_end = min(cursor.normalize() + chunk_span - pd.Timedelta(seconds=1), request_end)
                    if chunk_end < cursor:
                        chunk_end = min(cursor + chunk_span - pd.Timedelta(seconds=1), request_end)
                    for attempt in range(REQUEST_ATTEMPTS):
                        try:
                            chunk = history(
                                symbol=symbol, frequency=freq,
                                start_time=cursor.strftime('%Y-%m-%d %H:%M:%S'),
                                end_time=chunk_end.strftime('%Y-%m-%d %H:%M:%S'),
                                fields='open,high,low,close,volume,eob', adjust=price_adjustment,
                                adjust_end_time=request_end.strftime('%Y-%m-%d %H:%M:%S'), df=True,
                            )
                            if chunk is None:
                                raise RuntimeError('GM history returned None')
                            break
                        except Exception as exc:
                            # 认证失败重试无益；不把已下载的半个窗口当作完整行情。
                            if getattr(exc, 'code', None) in {1000, 2002} or any(
                                f'"status": {code}' in str(exc) for code in (1000, 2002)
                            ):
                                raise
                            if attempt + 1 == REQUEST_ATTEMPTS:
                                raise
                            time.sleep(0.2 * (attempt + 1))
                    if not chunk.empty:
                        frames.append(chunk)
                    cursor = chunk_end + pd.Timedelta(seconds=1)
                df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            else:
                df = history(symbol=symbol, frequency=freq, start_time=start_date, end_time=end_date,
                             fields='open,high,low,close,volume,eob', adjust=price_adjustment, df=True)

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
            # 历史结束日在今天之前时不请求 current，避免把未复权实时快照混入历史回测，
            # 也避免旧版 GM SDK 的实时 protobuf 字段转换影响历史数据链路。
            should_stitch_realtime = freq == '1d' and (
                end_date is None or pd.Timestamp(end_date).normalize() >= pd.Timestamp.now().normalize()
            )
            if should_stitch_realtime:
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
            df = df[~df.index.duplicated(keep='last')]
            df.attrs['gm_price_adjustment'] = price_adjustment

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

            # 可按需打印已为 {symbol} 拼接实时 K 线。

        return df
