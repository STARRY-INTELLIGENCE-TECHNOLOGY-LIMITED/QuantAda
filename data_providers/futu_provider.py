"""富途 OpenD 行情数据源。

该模块只负责行情读取和结果标准化，不负责交易下单。富途 OpenD 的
股票、ETF 及普通衍生品使用统一历史 K 线接口；事件合约期权在标准接口
不支持时回退专用历史接口。期权链提供额外的显式查询方法，方便策略先发现合约再调用 ``get_data`` 回测。
"""

import math
import os
import re
import threading
import time

import config
import pandas as pd
from live_trader.adapters.futu_symbols import (
    OPTION_CODE_RE as _OPTION_CODE_RE,
    VENUE_ALIASES as _VENUE_ALIASES,
    normalize_futu_symbol,
)
from common.options.chain import normalize_option_chain
from .base_provider import BaseDataProvider
from common.live_runtime import dependency_install_hint

try:
    import futu as _futu
    OpenQuoteContext = getattr(_futu, 'OpenQuoteContext', None)
    RET_OK = getattr(_futu, 'RET_OK', 0)
    AuType = getattr(_futu, 'AuType', None)
    KLType = getattr(_futu, 'KLType', None)
    SysConfig = getattr(_futu, 'SysConfig', None)
    IndexOptionType = getattr(_futu, 'IndexOptionType', None)
    Market = getattr(_futu, 'Market', None)
    OptionCondType = getattr(_futu, 'OptionCondType', None)
    OptionType = getattr(_futu, 'OptionType', None)
    SecurityType = getattr(_futu, 'SecurityType', None)
    _FUTU_IMPORT_ERROR = None
except Exception as exc:
    # OpenD 是外部运行服务；未安装 futu-api 时不能阻断其他 Provider。
    _futu = None
    AuType = IndexOptionType = KLType = Market = None
    OptionCondType = OptionType = SecurityType = SysConfig = None
    OpenQuoteContext = None
    RET_OK = 0
    _FUTU_IMPORT_ERROR = exc
    print(dependency_install_hint('futu-api', exc))


_DEFAULT_QUERY_TIMEOUT_SECONDS = 5.0
_CONTEXT_INIT_TIMEOUT_SECONDS = 5.0
_QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS = 5.0
_REQUIRED_COLUMNS = ('open', 'high', 'low', 'close', 'volume')

def _enum_value(enum_type, name: str, fallback: str):
    """读取 SDK 枚举值；测试替身或旧 SDK 缺失时回退为协议字符串。"""
    return getattr(enum_type, name, fallback) if enum_type is not None else fallback


def _positive_float(value):
    """解析正的有限数值；无效值返回 None。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _field_value(row, names):
    """从元数据行按别名读取第一个非空字段。"""
    if row is None:
        return None
    for name in names:
        if name in row.index:
            value = row.get(name)
            if value is not None and not pd.isna(value) and not (
                isinstance(value, str) and not value.strip()
            ):
                return value
    return None


def _row_first_value(row, names):
    """从字典或 Series 行读取第一个非空字段。"""
    if row is None:
        return None
    if isinstance(row, dict):
        for name in names:
            if name in row and row[name] is not None and not pd.isna(row[name]):
                return row[name]
        return None
    return _field_value(row, names)


class FutuDataProvider(BaseDataProvider):
    """通过富途 OpenD 获取 A 股、国际股票和期权等衍生品行情。"""

    # OpenD 是外部常驻服务，默认责任链放在常规在线 Provider 之后；需要时可用
    # ``--data_source=futu`` 显式选择，避免未启动 OpenD 时拖慢普通回测。
    PRIORITY = 70

    def __init__(self, quote_ctx=None):
        self.host = getattr(config, 'FUTU_HOST', '127.0.0.1')
        self.port = getattr(config, 'FUTU_PORT', 11111)
        self.rsa_key_path = str(getattr(config, 'FUTU_RSA_KEY_PATH', '') or '').strip()
        self._quote_ctx = quote_ctx
        self._owns_quote_ctx = quote_ctx is None
        self._context_lock = threading.RLock()
        self._known_option_symbols = set()
        self._quote_context_init_thread = None
        self._quote_context_init_failed = False
        self._quote_context_retry_at = 0.0

        if OpenQuoteContext is None and quote_ctx is None and _FUTU_IMPORT_ERROR is None:
            print(dependency_install_hint('futu-api'))

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """将 QuantAda/券商代码转换为 Futu ``MARKET.CODE`` 格式。"""
        return normalize_futu_symbol(symbol)

    @staticmethod
    def _normalize_date(value, *, intraday: bool, end: bool = False):
        """兼容 YYYYMMDD 与时间字符串，并为日内请求保留秒级边界。"""
        if value is None or str(value).strip() == '':
            return None
        try:
            timestamp = pd.Timestamp(value)
        except Exception:
            return str(value)
        if intraday:
            raw = str(value).strip()
            has_explicit_time = ' ' in raw or 'T' in raw
            if end and not has_explicit_time and timestamp.hour == 0 and timestamp.minute == 0 and timestamp.second == 0:
                timestamp = timestamp + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            return timestamp.strftime('%Y-%m-%d %H:%M:%S')
        return timestamp.strftime('%Y-%m-%d')

    @staticmethod
    def _resolve_kline_type(timeframe: str, compression: int):
        """解析 Futu 原生 K 线周期，返回 ``(ktype, local_compression)``。"""
        normalized = str(timeframe or '').strip().lower()
        try:
            period = int(compression or 1)
        except (TypeError, ValueError, OverflowError):
            return None, None
        if period <= 0:
            return None, None

        if normalized in {'days', 'day', 'd'}:
            native = 'K_DAY'
            return _enum_value(KLType, native, native), period
        if normalized in {'weeks', 'week', 'w'}:
            return _enum_value(KLType, 'K_WEEK', 'K_WEEK'), period
        if normalized in {'months', 'month', 'mon'}:
            return _enum_value(KLType, 'K_MON', 'K_MON'), period
        if normalized in {'minutes', 'minute', 'min', 'm'}:
            native_name = {
                1: 'K_1M',
                3: 'K_3M',
                5: 'K_5M',
                10: 'K_10M',
                15: 'K_15M',
                30: 'K_30M',
                60: 'K_60M',
                120: 'K_120M',
                180: 'K_180M',
                240: 'K_240M',
            }.get(period)
            if native_name is None:
                return None, None
            return _enum_value(KLType, native_name, native_name), 1
        # Futu OpenD 没有 Seconds K 线，不能伪装成分钟数据。
        return None, None

    @staticmethod
    def _is_option_symbol(symbol: str) -> bool:
        return bool(_OPTION_CODE_RE.match(FutuDataProvider._normalize_symbol(symbol)))

    def _configure_protocol(self):
        """根据 RSA 路径配置全局协议；空路径明确关闭加密。"""
        if SysConfig is None:
            return
        rsa_path = os.path.expandvars(os.path.expanduser(self.rsa_key_path))
        if rsa_path:
            if not os.path.isfile(rsa_path):
                raise FileNotFoundError(f'Futu RSA key file not found: {rsa_path}')
            SysConfig.enable_proto_encrypt(True)
            SysConfig.set_init_rsa_file(file=rsa_path)
        else:
            SysConfig.enable_proto_encrypt(False)

    def _get_quote_context(self):
        """按需创建并复用行情连接，避免每个标的泄漏一个 OpenD socket。"""
        with self._context_lock:
            if self._quote_ctx is not None:
                try:
                    raw_status = getattr(self._quote_ctx, 'status', '')
                    status = str(getattr(raw_status, 'value', raw_status)).upper()
                except Exception:
                    status = ''
                if status not in {'CLOSED', 'CLOSING'}:
                    return self._quote_ctx
                stale_context = self._quote_ctx
                owns_stale_context = self._owns_quote_ctx
                self._quote_ctx = None
                self._owns_quote_ctx = False
                if owns_stale_context:
                    try:
                        stale_context.close()
                    except Exception as exc:
                        print(f'[Futu] Failed to close stale OpenD context: {exc}')
            if OpenQuoteContext is None:
                return None
            if (
                self._quote_context_init_failed
                and time.monotonic() < self._quote_context_retry_at
            ):
                return None
            if (
                self._quote_context_init_thread is not None
                and self._quote_context_init_thread.is_alive()
            ):
                return None
            try:
                self._configure_protocol()
                # Futu 的异步构造函数会立即启动重连 Timer。在线程中创建可使
                # SDK 自己的 Timer 继承 daemon 属性，OpenD 不可用时不会阻塞进程退出。
                set_all_daemon = getattr(SysConfig, 'set_all_thread_daemon', None)
                if callable(set_all_daemon):
                    set_all_daemon(True)
                result = {}
                error = {}
                state = {'timed_out': False}

                def create_context():
                    try:
                        context = OpenQuoteContext(
                            host=self.host,
                            port=int(self.port),
                            is_encrypt=bool(self.rsa_key_path),
                            is_async_connect=True,
                        )
                        if state['timed_out']:
                            close_context = getattr(context, 'close', None)
                            if callable(close_context):
                                close_context()
                        else:
                            result['context'] = context
                    except Exception as exc:
                        error['exception'] = exc

                creator = threading.Thread(target=create_context, name='quantada-futu-context', daemon=True)
                self._quote_context_init_thread = creator
                creator.start()
                creator.join(_CONTEXT_INIT_TIMEOUT_SECONDS)
                if creator.is_alive():
                    state['timed_out'] = True
                    self._quote_context_init_failed = True
                    self._quote_context_retry_at = (
                        time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                    )
                    print('[Futu] OpenD context initialization timed out.')
                    return None
                if 'exception' in error:
                    raise error['exception']
                self._quote_ctx = result.get('context')
                if self._quote_ctx is None:
                    self._quote_context_init_failed = True
                    self._quote_context_retry_at = (
                        time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                    )
                    return None
                self._owns_quote_ctx = True
                if not self._wait_context_ready(self._quote_ctx):
                    print('[Futu] OpenD quote context did not become ready before timeout.')
                    try:
                        self._quote_ctx.close()
                    except Exception:
                        pass
                    self._quote_ctx = None
                    self._owns_quote_ctx = False
                    self._quote_context_init_failed = True
                    self._quote_context_retry_at = (
                        time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                    )
                    return None
                set_timeout = getattr(self._quote_ctx, 'set_sync_query_connect_timeout', None)
                if callable(set_timeout):
                    try:
                        set_timeout(_DEFAULT_QUERY_TIMEOUT_SECONDS)
                    except Exception as exc:
                        print(f"[Futu] Failed to set OpenD query timeout: {exc}")
                self._quote_context_init_failed = False
                self._quote_context_retry_at = 0.0
                return self._quote_ctx
            except Exception as exc:
                print(f"[Futu] OpenD connection initialization failed: {exc}")
                self._quote_ctx = None
                self._owns_quote_ctx = False
                self._quote_context_init_failed = True
                self._quote_context_retry_at = (
                    time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                )
                return None

    @staticmethod
    def _wait_context_ready(context) -> bool:
        """等待异步 OpenD 连接进入 READY；无 status 属性的测试替身直接放行。"""
        deadline = time.monotonic() + _CONTEXT_INIT_TIMEOUT_SECONDS
        observed_status = False
        while time.monotonic() < deadline:
            status = getattr(context, 'status', None)
            if status is None:
                return True
            observed_status = True
            normalized = str(getattr(status, 'value', status)).upper()
            if normalized == 'READY':
                return True
            if normalized in {'CLOSED', 'CLOSING'}:
                return False
            time.sleep(0.05)
        return not observed_status

    def _request_history(self, code: str, start_date, end_date, ktype, autype):
        """调用历史 K 线接口并统一处理返回协议。"""
        context = self._get_quote_context()
        if context is None:
            return None
        fields = None
        if KLType is not None:
            # ALL_REAL 对股票和期权都包含标准 OHLCV 字段，避免硬编码数字枚举。
            try:
                from futu import KL_FIELD
                fields = KL_FIELD.ALL_REAL
            except Exception:
                fields = None
        kwargs = {
            'start': start_date,
            'end': end_date,
            'ktype': ktype,
            'autype': autype,
            'max_count': None,
        }
        if fields is not None:
            kwargs['fields'] = fields
        try:
            with self._context_lock:
                request_method = getattr(context, 'request_history_kline', None)
                event_method = getattr(context, 'request_history_event_contract_kline', None)
                is_option = self._is_option_symbol(code)
                response = None
                standard_failed = False
                if callable(request_method):
                    try:
                        response = request_method(code, **kwargs)
                        standard_failed = (
                            not isinstance(response, tuple)
                            or len(response) < 2
                            or response[0] != RET_OK
                            or response[1] is None
                            or (
                                isinstance(response[1], pd.DataFrame)
                                and response[1].empty
                            )
                        )
                    except Exception:
                        standard_failed = True
                else:
                    standard_failed = True

                # Futu 美股期权历史行情需要事件合约专用接口；普通证券仍走标准接口。
                if is_option and standard_failed and callable(event_method):
                    response = event_method(
                        code,
                        start=start_date,
                        end=end_date,
                        ktype=ktype,
                        max_count=None,
                    )
                if response is None:
                    raise RuntimeError('Futu history K-line interface is unavailable')
        except Exception as exc:
            print(f"[Futu] History request failed for {code}: {exc}")
            return None
        if not isinstance(response, tuple) or len(response) < 2:
            print(f"[Futu] Invalid history response for {code}.")
            return None
        ret_code, data = response[0], response[1]
        if ret_code != RET_OK:
            print(f"[Futu] History request rejected for {code}: {data}")
            return None
        return data

    @staticmethod
    def _normalize_dataframe(raw_df: pd.DataFrame) -> pd.DataFrame:
        """将 Futu K 线表转换为框架标准 OHLCV 表。"""
        if raw_df is None or not isinstance(raw_df, pd.DataFrame) or raw_df.empty:
            return None
        df = raw_df.copy()
        time_column = next(
            (column for column in ('time_key', 'datetime', 'time', 'date') if column in df.columns),
            None,
        )
        if time_column is None:
            print('[Futu] History response has no time column.')
            return None
        missing = [column for column in _REQUIRED_COLUMNS if column not in df.columns]
        if missing:
            print(f"[Futu] History response missing columns: {', '.join(missing)}")
            return None

        parsed_time = pd.to_datetime(df[time_column], errors='coerce')
        valid_time = parsed_time.notna()
        df = df.loc[valid_time].copy()
        df.index = pd.DatetimeIndex(parsed_time.loc[valid_time], name='datetime')
        for column in _REQUIRED_COLUMNS:
            df[column] = pd.to_numeric(df[column], errors='coerce')
        df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
        if df.empty:
            return None
        if df['volume'].isna().all():
            return None
        df['volume'] = df['volume'].fillna(0)
        df = df[~df.index.duplicated(keep='last')].sort_index()
        ordered = list(_REQUIRED_COLUMNS)
        extras = [column for column in df.columns if column not in ordered and column != time_column]
        return df[ordered + extras]

    @staticmethod
    def _aggregate_rows(df: pd.DataFrame, compression: int) -> pd.DataFrame:
        """对 Futu 不提供的多日周期做确定性的行级 OHLCV 聚合。"""
        if compression <= 1 or df is None or df.empty:
            return df
        group_id = pd.Series(range(len(df)), index=df.index) // compression
        grouped = df.groupby(group_id, sort=True)
        result = pd.DataFrame({
            'open': grouped['open'].first(),
            'high': grouped['high'].max(),
            'low': grouped['low'].min(),
            'close': grouped['close'].last(),
            'volume': grouped['volume'].sum(min_count=1),
        })
        result.index = pd.DatetimeIndex([group.index[-1] for _, group in grouped], name='datetime')
        return result

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        ktype, local_compression = self._resolve_kline_type(timeframe, compression)
        if ktype is None:
            print(f"[Futu] Unsupported timeframe/compression: {timeframe}/{compression}")
            return None
        futu_symbol = self._normalize_symbol(symbol)
        if not futu_symbol:
            return None
        normalized_timeframe = str(timeframe or '').strip().lower()
        intraday = normalized_timeframe in {'minutes', 'minute', 'min', 'm'}
        start = self._normalize_date(start_date, intraday=intraday)
        end = self._normalize_date(end_date, intraday=intraday, end=True)
        market_name = futu_symbol.split('.', 1)[0]
        non_adjusted = market_name in {'FX', 'HK_FUTURE', 'EC'}
        autype_name = (
            'NONE'
            if non_adjusted or futu_symbol in self._known_option_symbols or self._is_option_symbol(futu_symbol)
            else 'QFQ'
        )
        autype = _enum_value(AuType, autype_name, 'None' if autype_name == 'NONE' else 'qfq')
        raw_df = self._request_history(futu_symbol, start, end, ktype, autype)
        result = self._normalize_dataframe(raw_df)
        if result is None:
            return None
        if self._is_option_symbol(futu_symbol):
            # 历史 K 线接口不返回期权合约乘数；把同一 OpenD 快照中的合约元数据
            # 写入结果，供回测柜台按合约张数正确估值和扣款。
            multiplier = None

            # 某些事件合约历史接口会直接返回乘数字段；优先使用历史响应，
            # 这样过期合约即使无法再读取实时快照也能安全回测。
            for name in (
                'option_contract_multiplier',
                'option_contract_size',
                'contract_multiplier',
                'contract_size',
            ):
                if name in result.attrs:
                    multiplier = _positive_float(result.attrs.get(name))
                if multiplier is None and name in result.columns:
                    values = pd.to_numeric(result[name], errors='coerce').dropna()
                    if not values.empty:
                        multiplier = _positive_float(values.iloc[-1])
                if (
                    multiplier is not None
                    and name in {'contract_multiplier', 'contract_size'}
                    and multiplier <= 1
                ):
                    multiplier = None
                if multiplier is not None:
                    break

            try:
                if multiplier is None:
                    snapshot = self.get_market_snapshot([futu_symbol])
                    if snapshot is not None and not snapshot.empty:
                        row = snapshot.iloc[0]
                        for name in (
                            'option_contract_multiplier',
                            'option_contract_size',
                            'contract_multiplier',
                            'contract_size',
                        ):
                            parsed = _positive_float(row.get(name)) if name in row.index else None
                            if (
                                parsed is not None
                                and name in {'contract_multiplier', 'contract_size'}
                                and parsed <= 1
                            ):
                                parsed = None
                            if parsed is not None:
                                multiplier = parsed
                                break
                if multiplier is not None:
                    result.attrs['contract_multiplier'] = multiplier
                    result['contract_multiplier'] = multiplier
            except Exception as exc:
                print(f'[Futu] Option contract metadata unavailable for {futu_symbol}: {exc}')
            if multiplier is None:
                print(
                    f'[Futu] Option contract multiplier unavailable for {futu_symbol}; '
                    'history excluded to avoid incorrect notional sizing.'
                )
                return None
        if local_compression > 1:
            contract_multiplier = result.attrs.get('contract_multiplier')
            result = self._aggregate_rows(result, local_compression)
            if contract_multiplier is not None:
                result.attrs['contract_multiplier'] = contract_multiplier
                result['contract_multiplier'] = contract_multiplier
        return result

    def get_market_snapshot(self, symbols) -> pd.DataFrame:
        """获取股票、ETF、期权或其他衍生品的实时快照。"""
        if isinstance(symbols, str):
            symbols = [item for item in re.split(r'[,\s]+', symbols) if item]
        codes = [self._normalize_symbol(item) for item in (symbols or [])]
        codes = [code for code in codes if code]
        if not codes:
            return None
        context = self._get_quote_context()
        if context is None:
            return None
        try:
            with self._context_lock:
                ret_code, data = context.get_market_snapshot(codes)
        except Exception as exc:
            print(f"[Futu] Snapshot request failed: {exc}")
            return None
        if ret_code != RET_OK or not isinstance(data, pd.DataFrame) or data.empty:
            if ret_code != RET_OK:
                print(f"[Futu] Snapshot request rejected: {data}")
            return None
        return data.copy()

    def get_option_chain(self, underlying: str, start=None, end=None,
                         option_type='ALL', option_cond_type='ALL',
                         index_option_type='NORMAL', data_filter=None,
                         normalized=False, timestamp=None, as_of=None,
                         contract_multiplier=None) -> pd.DataFrame:
        """查询期权链；``normalized=True`` 时返回统一字段模型。"""
        code = self._normalize_symbol(underlying)
        if not code:
            return None
        context = self._get_quote_context()
        if context is None:
            return None
        option_type_value = _enum_value(OptionType, str(option_type).upper(), str(option_type).upper())
        option_cond_value = _enum_value(OptionCondType, str(option_cond_type).upper(), str(option_cond_type).upper())
        index_value = _enum_value(IndexOptionType, str(index_option_type).upper(), str(index_option_type).upper())
        try:
            with self._context_lock:
                ret_code, data = context.get_option_chain(
                    code,
                    index_option_type=index_value,
                    start=self._normalize_date(start, intraday=False),
                    end=self._normalize_date(end, intraday=False, end=True),
                    option_type=option_type_value,
                    option_cond_type=option_cond_value,
                    data_filter=data_filter,
                )
        except Exception as exc:
            print(f"[Futu] Option-chain request failed for {code}: {exc}")
            return None
        if ret_code != RET_OK or not isinstance(data, pd.DataFrame) or data.empty:
            if ret_code != RET_OK:
                print(f"[Futu] Option-chain request rejected for {code}: {data}")
            return None
        if 'code' in data.columns:
            known_symbols = {
                self._normalize_symbol(value) for value in data['code'].dropna().tolist()
            }
            self._known_option_symbols.update(code for code in known_symbols if code)
        result = data.copy()
        if not normalized:
            return result
        result = self._enrich_option_chain_metadata(result, code)
        # OpenD 链接口有时只返回合约字段，实时调用可用本次请求完成时间作为
        # 快照边界；历史调用必须由源数据提供真实 timestamp，runtime 会拒绝
        # 将调用方时间戳伪装成历史可见时间。
        effective_timestamp = timestamp if timestamp is not None else pd.Timestamp.now(tz='UTC')
        normalized_result = normalize_option_chain(
            result,
            code,
            timestamp=effective_timestamp,
            as_of=as_of,
            contract_multiplier=contract_multiplier,
            symbol_normalizer=self._normalize_symbol,
            require_quotes=True,
        )
        if normalized_result is None:
            print(f'[Futu] Option-chain normalization failed for {code}; snapshot rejected.')
        return normalized_result

    def _enrich_option_chain_metadata(self, chain: pd.DataFrame, underlying: str) -> pd.DataFrame:
        """仅从富途元数据和实时快照补全链字段，不猜测合约乘数。"""
        result = chain.copy()
        codes = []
        if 'code' in result.columns:
            codes = [
                self._normalize_symbol(value)
                for value in result['code'].dropna().tolist()
                if self._normalize_symbol(value)
            ]
        multiplier_columns = [
            name for name in (
                'contract_multiplier', 'contract_size',
                'option_contract_multiplier', 'option_contract_size',
            ) if name in result.columns
        ]
        has_multiplier = any(
            pd.to_numeric(result[name], errors='coerce').gt(0).any()
            for name in multiplier_columns
        )
        if codes and not has_multiplier:
            basic = self.get_stock_basicinfo(
                market=codes[0].split('.', 1)[0],
                stock_type='OPTION',
                code_list=codes,
            )
            if isinstance(basic, pd.DataFrame) and not basic.empty:
                basic_codes = {
                    self._normalize_symbol(value): row
                    for _, row in basic.iterrows()
                    for value in [_field_value(row, ('code', 'symbol'))]
                    if value
                }
                multiplier_values = []
                for code_value in result['code']:
                    row = basic_codes.get(self._normalize_symbol(code_value))
                    multiplier_values.append(
                        _row_first_value(
                            row,
                            ('option_contract_multiplier', 'option_contract_size',
                             'contract_multiplier', 'contract_size'),
                        )
                    )
                result['contract_multiplier'] = multiplier_values

        # 期权链接口只描述合约，盘口和乘数从同一 OpenD 行情快照补全。
        if codes:
            quote = self.get_market_snapshot(codes)
            if isinstance(quote, pd.DataFrame) and not quote.empty and 'code' in quote.columns:
                quote_rows = {
                    self._normalize_symbol(value): row
                    for _, row in quote.iterrows()
                    for value in [_field_value(row, ('code', 'symbol'))]
                    if value
                }
                for output_name, aliases in (
                    ('contract_multiplier', ('option_contract_multiplier', 'option_contract_size', 'contract_multiplier', 'contract_size')),
                    ('bid', ('bid_price', 'bid')),
                    ('ask', ('ask_price', 'ask')),
                    ('last', ('last_price', 'last', 'price', 'close')),
                    ('volume', ('volume', 'vol')),
                    ('open_interest', ('option_open_interest', 'open_interest', 'oi')),
                    ('iv', ('option_implied_volatility', 'implied_volatility', 'iv')),
                    ('delta', ('option_delta', 'delta')),
                    ('gamma', ('option_gamma', 'gamma')),
                    ('theta', ('option_theta', 'theta')),
                    ('vega', ('option_vega', 'vega')),
                    ('rho', ('option_rho', 'rho')),
                    ('timestamp', ('update_time', 'timestamp', 'datetime')),
                ):
                    if output_name in result.columns:
                        current_values = result[output_name].tolist()
                    else:
                        current_values = [None] * len(result)
                    result[output_name] = [
                        current if current is not None and not pd.isna(current)
                        else _row_first_value(
                            quote_rows.get(self._normalize_symbol(code_value)), aliases
                        )
                        for current, code_value in zip(current_values, result['code'])
                    ]

        if not any(name in result.columns for name in ('spot', 'spot_price', 'stock_price')):
            snapshot = self.get_market_snapshot([underlying])
            if isinstance(snapshot, pd.DataFrame) and not snapshot.empty:
                spot = _row_first_value(
                    snapshot.iloc[0],
                    ('last_price', 'last', 'price', 'close'),
                )
                if spot is not None:
                    result['spot'] = spot
        # 部分 OpenD 期权快照不返回币种；这是 Provider 已知的市场元数据，
        # 可以安全补全，不能把它留作交易链中的未知风险字段。
        if 'currency' not in result.columns or result['currency'].isna().all():
            market_currency = {
                'US': 'USD',
                'HK': 'HKD',
                'SH': 'CNH',
                'SZ': 'CNH',
                'SG': 'SGD',
                'JP': 'JPY',
                'AU': 'AUD',
                'CA': 'CAD',
            }.get(underlying.split('.', 1)[0].upper())
            if market_currency:
                result['currency'] = market_currency
        return result

    def get_option_chain_normalized(self, underlying: str, start=None, end=None,
                                    option_type='ALL', option_cond_type='ALL',
                                    index_option_type='NORMAL', data_filter=None,
                                    timestamp=None, as_of=None,
                                    contract_multiplier=None) -> pd.DataFrame:
        """返回严格的 QuantAda 统一期权链字段。"""
        return self.get_option_chain(
            underlying,
            start=start,
            end=end,
            option_type=option_type,
            option_cond_type=option_cond_type,
            index_option_type=index_option_type,
            data_filter=data_filter,
            normalized=True,
            timestamp=timestamp,
            as_of=as_of,
            contract_multiplier=contract_multiplier,
        )

    def get_stock_basicinfo(self, market=None, stock_type='STOCK', code_list=None) -> pd.DataFrame:
        """查询股票、ETF、期权等基础合约信息。"""
        context = self._get_quote_context()
        if context is None:
            return None
        codes = None
        if code_list is not None:
            if isinstance(code_list, str):
                code_list = [item for item in re.split(r'[,\s]+', code_list) if item]
            codes = [self._normalize_symbol(item) for item in code_list]
            codes = [code for code in codes if code]
            if not codes:
                return None
        if market:
            market_name = str(market).upper()
            market_name = _VENUE_ALIASES.get(market_name, market_name)
            market_value = _enum_value(Market, market_name, market_name)
        else:
            market_value = _enum_value(Market, 'NONE', 'N/A')
        stock_type_name = 'DRVT' if str(stock_type).upper() == 'OPTION' else str(stock_type).upper()
        stock_type_value = _enum_value(SecurityType, stock_type_name, stock_type_name)
        try:
            with self._context_lock:
                ret_code, data = context.get_stock_basicinfo(
                    market=market_value,
                    stock_type=stock_type_value,
                    code_list=codes,
                )
        except Exception as exc:
            print(f"[Futu] Basic-info request failed: {exc}")
            return None
        if ret_code != RET_OK or not isinstance(data, pd.DataFrame):
            if ret_code != RET_OK:
                print(f"[Futu] Basic-info request rejected: {data}")
            return None
        return data.copy()

    def close(self):
        """关闭由本 Provider 创建的 OpenD 行情连接。"""
        with self._context_lock:
            context = self._quote_ctx
            self._quote_ctx = None
            owns_context = self._owns_quote_ctx
            self._owns_quote_ctx = False
            self._quote_context_init_failed = False
            self._quote_context_retry_at = 0.0
            if context is not None and owns_context:
                try:
                    context.close()
                except Exception as exc:
                    print(f"[Futu] Failed to close OpenD context: {exc}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
