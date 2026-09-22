import importlib
import inspect
import os
import re
import threading
from collections import OrderedDict

import pandas as pd

import config
from .base_provider import BaseDataProvider
from .compositions import build_provider_composition
from .csv_provider import CsvDataProvider
from common.options.analytics import parse_option_symbol
from common.options.data_safety import sanitize_market_dataframe


_PLATFORM_DEFAULT_SOURCES = {
    'ib': 'ibkr',
    'ibkr': 'ibkr',
    'ib_broker': 'ibkr',
    'gm': 'gm',
    'gm_broker': 'gm',
    'futu': 'futu',
    'futu_broker': 'futu',
    'theta': 'theta',
    'thetadata': 'theta',
}


def resolve_platform_default_source(platform: str) -> str:
    """解析实盘平台对应的默认数据源名称。"""
    normalized = str(platform or '').strip().lower()
    return _PLATFORM_DEFAULT_SOURCES.get(normalized, '')


def normalize_source_name(source: str) -> str:
    """将平台或 Provider 别名规范化为 DataManager 使用的名称。"""
    normalized = str(source or '').strip().lower()
    if normalized in {'hybrid', 'option_hybrid', 'theta+futu', 'futu+theta', 'theta_futu', 'futu_theta'}:
        return 'hybrid'
    if normalized == 'sxsc_tushare':
        return 'sxsctushare'
    return _PLATFORM_DEFAULT_SOURCES.get(normalized, normalized)


class DataManager:
    def __init__(self):
        self.providers = self.auto_discover_and_sort_providers()
        self.data_path = config.DATA_PATH
        # 创建一个从字符串名称到提供者实例的映射
        self.provider_map = OrderedDict(
            (p.__class__.__name__.replace('DataProvider', '').lower(), p)
            for p in self.providers
        )
        self._composed_providers = {}
        self._provider_lock = threading.Lock()
        self._live_mode = False
        self._bound_broker = None

    def _provider_for_source(self, name):
        """返回普通 Provider 或按配置惰性构造的组合 Provider。"""

        raw_name = str(name or '').strip().lower()
        normalized = normalize_source_name(raw_name)
        provider = self.provider_map.get(normalized)
        if provider is not None:
            return provider
        compositions = getattr(config, 'DATA_PROVIDER_COMPOSITIONS', {})
        spec = None
        composition_name = normalized
        if isinstance(compositions, dict):
            if raw_name in compositions:
                composition_name = raw_name
                spec = compositions[raw_name]
            else:
                spec = compositions.get(normalized)
        if spec is None:
            return None
        composed = getattr(self, '_composed_providers', None)
        if not isinstance(composed, dict):
            composed = {}
            self._composed_providers = composed
        with self._provider_lock:
            if composition_name not in composed:
                composed[composition_name] = build_provider_composition(
                    composition_name, spec, self.provider_map
                )
                setter = getattr(composed[composition_name], 'set_live_mode', None)
                if callable(setter):
                    setter(getattr(self, '_live_mode', False))
                if getattr(self, '_bound_broker', None) is not None:
                    binder = getattr(composed[composition_name], 'bind_broker', None)
                    if callable(binder):
                        binder(self._bound_broker)
        return composed[composition_name]

    def _all_provider_instances(self):
        """返回普通和已构造组合 Provider，供运行时注入/关闭使用。"""

        yield from self.providers
        composed = getattr(self, '_composed_providers', None)
        if isinstance(composed, dict):
            yield from composed.values()

    @staticmethod
    def _explicit_provider_method(provider, name):
        """仅返回实例显式属性或类层声明的方法，避免触发动态 API。"""

        instance_values = getattr(provider, '__dict__', None)
        if isinstance(instance_values, dict):
            candidate = instance_values.get(name)
            if callable(candidate):
                return candidate
        for base in type(provider).__mro__:
            candidate = base.__dict__.get(name)
            if callable(candidate):
                return getattr(provider, name)
        return None

    @staticmethod
    def _explicit_provider_value(provider, name, default=None):
        """读取显式 Provider 字段，不触发动态 ``__getattr__``。"""

        instance_values = getattr(provider, '__dict__', None)
        if isinstance(instance_values, dict) and name in instance_values:
            return instance_values[name]
        for base in type(provider).__mro__:
            if name in base.__dict__:
                try:
                    return getattr(provider, name)
                except Exception:
                    return default
        return default

    @staticmethod
    def _split_source_names(specified_sources: str) -> list:
        """
        将 data_source 字符串拆分为 provider 名称列表。
        支持空格/逗号分隔（如: "tiingo, akshare" 或 "tiingo akshare"）。
        """
        if not specified_sources:
            return []
        raw = str(specified_sources).strip().lower()
        if not raw:
            return []
        # ``theta+futu`` 表示字段合并 Provider；逗号/空格仍表示普通回退链。
        compact = re.sub(r'\s*\+\s*', '+', raw)
        compositions = getattr(config, 'DATA_PROVIDER_COMPOSITIONS', {})
        if not isinstance(compositions, dict):
            compositions = {}
        if (
            compact in compositions
        ):
            return [compact]
        if normalize_source_name(compact) == 'hybrid':
            return ['hybrid']
        return [normalize_source_name(s) for s in re.split(r"[,\s]+", compact) if s]

    def apply_runtime_token(self, token: str, specified_sources: str = None) -> bool:
        """将外部运行时令牌注入选中的托管数据源。"""
        raw_token = str(token or '').strip()
        if not raw_token:
            return False

        allowed_sources = set(self._split_source_names(specified_sources))
        allowed_sources.update(normalize_source_name(name) for name in allowed_sources)
        applied = False
        for provider in self._all_provider_instances():
            outer_name = provider.__class__.__name__.replace('DataProvider', '').lower()
            candidates = [provider]
            for child_name in (
                'historical_provider', 'realtime_provider',
                'theta_provider', 'futu_provider',
            ):
                child = (
                    getattr(provider, '__dict__', {}).get(child_name)
                    if isinstance(getattr(provider, '__dict__', None), dict)
                    else None
                )
                if child is not None:
                    candidates.append(child)
            for candidate in candidates:
                provider_name = candidate.__class__.__name__.replace('DataProvider', '').lower()
                if allowed_sources and provider_name not in allowed_sources and outer_name not in allowed_sources:
                    continue
                is_external = bool(self._explicit_provider_value(candidate, 'is_external_mode', False))
                is_placeholder = self._explicit_provider_value(candidate, 'token', None) == 'EXTERNAL_MODE'
                if not (is_external or is_placeholder):
                    continue
                try:
                    candidate.token = raw_token
                    candidate.is_external_mode = False
                    applied = True
                except Exception:
                    continue
        return applied

    def set_live_mode(self, enabled: bool) -> None:
        """向支持混合实时行情的 Provider 传递当前运行模式。"""

        live_mode = bool(enabled)
        self._live_mode = live_mode
        for provider in self._all_provider_instances():
            setter = self._explicit_provider_method(provider, 'set_live_mode')
            if callable(setter):
                try:
                    setter(live_mode)
                except Exception:
                    continue

    def bind_broker(self, broker) -> None:
        """将券商持有的共享行情会话注入支持混合数据的 Provider。"""

        self._bound_broker = broker
        for provider in self._all_provider_instances():
            binder = self._explicit_provider_method(provider, 'bind_broker')
            if callable(binder):
                try:
                    binder(broker)
                except Exception:
                    continue

    def close(self) -> None:
        """关闭普通和惰性构造的 Provider，避免组合层连接泄漏。"""
        seen = set()
        for provider in self._all_provider_instances():
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            # 某些 Provider 通过 __getattr__ 暴露动态 API；直接 getattr(provider,
            # 'close') 可能变成一次远程 ``close`` 查询。只调用类层实际声明的关闭方法。
            close_declared = any(
                callable(base.__dict__.get('close'))
                for base in type(provider).__mro__
            )
            if not close_declared:
                continue
            closer = self._explicit_provider_method(provider, 'close')
            if callable(closer):
                try:
                    closer()
                except Exception:
                    continue

    def close_after_fetch(self) -> int:
        """关闭覆盖了基类 close 的 Provider；默认空实现不会产生额外动作。"""
        seen = set()
        closed = 0
        for provider in self._all_provider_instances():
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            if not isinstance(provider, BaseDataProvider):
                continue
            close_impl = None
            for base in type(provider).__mro__:
                candidate = base.__dict__.get('close')
                if callable(candidate):
                    close_impl = candidate
                    break
            if close_impl is None or close_impl is BaseDataProvider.close:
                continue
            try:
                close_impl(provider)
                closed += 1
            except Exception:
                continue
        return closed

    def auto_discover_and_sort_providers(self, provider_dir=None):
        """
        自动扫描、加载并根据PRIORITY属性排序所有数据提供者。
        """
        print("\n--- Auto-discovering Data Providers ---")
        discovered_providers = []

        module_package = __package__ or "data_providers"
        module_base_dir = os.path.dirname(os.path.abspath(__file__))

        if provider_dir is None:
            scan_dir = module_base_dir
        elif os.path.isabs(provider_dir):
            scan_dir = provider_dir
        else:
            # 优先按当前模块目录解析，避免依赖启动时 cwd
            candidate = os.path.join(module_base_dir, provider_dir)
            scan_dir = candidate if os.path.isdir(candidate) else os.path.abspath(provider_dir)

        # 文件扫描和动态导入
        for filename in os.listdir(scan_dir):
            if filename.endswith(".py") and not filename.startswith(("__", "base_", "manager.")):
                module_name = filename[:-3]
                module_path = f"{module_package}.{module_name}"
                try:
                    module = importlib.import_module(module_path)
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if (
                            issubclass(obj, BaseDataProvider)
                            and obj is not BaseDataProvider
                            and obj.__module__ == module.__name__
                            and not bool(getattr(obj, 'HYBRID_ONLY', False))
                        ):
                            discovered_providers.append(obj())
                            print(f"  Discovered provider: {name} (Priority: {obj.PRIORITY})")
                            break
                except Exception as e:
                    print(f"  Warning: Failed to load provider from {filename}: {e}")

        # --- 核心改动：根据PRIORITY属性进行排序 ---
        # 数值越小，优先级越高
        discovered_providers.sort(key=lambda p: p.PRIORITY)

        print("\n--- Data Provider Chain (sorted by priority) ---")
        for i, p in enumerate(discovered_providers):
            print(f"  {i + 1}. {p.__class__.__name__}")

        return discovered_providers

    def get_data(self, symbol: str, start_date: str = None, end_date: str = None,
                 specified_sources: str = None, timeframe: str = 'Days', compression: int = 1, refresh: bool = False) -> pd.DataFrame:
        """
        智能获取数据。
        - 如果指定了 specified_sources，则按指定顺序尝试。
        - 否则，执行默认的责任链逻辑（带增量更新）。
        """
        final_df = None

        # 路径一: 用户指定了数据源
        if specified_sources:
            source_names = self._split_source_names(specified_sources)
            providers_to_use = []
            for name in source_names:
                try:
                    provider = self._provider_for_source(name)
                except Exception as exc:
                    # 单个组合源构造失败时保留后续 Provider 回退链；显式源的
                    # 错误已经在本地日志中说明，不应把整轮数据请求直接抛出。
                    print(f"Error: Failed to construct data source {name!r}: {exc}")
                    continue
                if provider is not None:
                    providers_to_use.append(provider)
            if not providers_to_use:
                print(f"Error: None of the specified sources '{specified_sources}' are valid.")
                return None
            # 单一在线 Provider 显式指定时，优先复用完整本地缓存；多 Provider
            # 仍严格保持调用方给出的责任链顺序，避免缓存改变 fallback 语义。
            # 实盘必须重新读取在线事实；即使本地 CSV 覆盖完整窗口，也不能
            # 用历史缓存短路当前行情。回测/优化的 hybrid 正股与期权历史可以走 CSV。
            if (
                len(source_names) == 1
                and source_names[0] != 'csv'
                and not bool(getattr(self, '_live_mode', False))
            ):
                final_df = self._load_complete_cache(
                    symbol,
                    start_date,
                    end_date,
                    timeframe,
                    compression,
                    refresh,
                )
            else:
                final_df = None
            if final_df is None or final_df.empty:
                final_df = self._fetch_from_providers(
                    symbol, start_date, end_date, providers_to_use,
                    timeframe, compression, refresh=refresh,
                )

        # 路径二: 执行默认的责任链逻辑
        else:
            final_df = self._get_data_smart(symbol, start_date, end_date, timeframe, compression, refresh)

        # 【最终切片】确保返回的数据在请求的日期范围内
        if final_df is not None and not final_df.empty:

            # 辅助函数：将输入的日期字符串对齐到 df 索引的时区，防止比较报错
            def align_date(dt_input, index):
                dt = pd.to_datetime(dt_input)
                if index.tz is not None:
                    # 如果索引有时区，输入时间也加上同样的时区
                    if dt.tz is None:
                        return dt.tz_localize(index.tz)
                    else:
                        return dt.tz_convert(index.tz)
                else:
                    # 如果索引无时区，输入时间也去掉时区
                    if dt.tz is not None:
                        return dt.tz_convert(None)
                return dt

            def normalize_boundary(dt_input, is_end=False):
                intraday_timeframes = {
                    'minutes', 'minute', 'min', 'm',
                    'seconds', 'second', 'sec', 's',
                }
                if not (is_end and str(timeframe or '').strip().lower() in intraday_timeframes):
                    return dt_input
                raw = str(dt_input).strip()
                if re.search(r'[T\s]\d', raw):
                    return dt_input
                try:
                    return pd.Timestamp(dt_input) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
                except Exception:
                    return dt_input

            if start_date:
                start_dt = align_date(start_date, final_df.index)
                final_df = final_df[final_df.index >= start_dt]
            if end_date:
                end_dt = align_date(normalize_boundary(end_date, is_end=True), final_df.index)
                final_df = final_df[final_df.index <= end_dt]

            return final_df

        print(f"Error: All data providers failed for symbol {symbol}.")
        return None

    def get_option_chain(self, underlying: str, specified_sources: str = None, **kwargs):
        """查询期权链；不走 CSV 缓存，失败时返回 None。"""
        providers = []
        if specified_sources:
            for name in self._split_source_names(specified_sources):
                try:
                    provider = self._provider_for_source(name)
                except Exception as exc:
                    print(f"Error: Failed to construct data source {name!r}: {exc}")
                    continue
                if provider is not None:
                    providers.append(provider)
        else:
            providers = list(self._all_provider_instances())
        for provider in providers:
            method = self._explicit_provider_method(provider, "get_option_chain")
            if method is None:
                continue
            call_kwargs = dict(kwargs)
            try:
                signature = inspect.signature(method)
            except (TypeError, ValueError):
                signature = None
            if signature is not None and not any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in signature.parameters.values()
            ):
                call_kwargs = {
                    key: value
                    for key, value in call_kwargs.items()
                    if key in signature.parameters
                }
            try:
                result = method(underlying, **call_kwargs)
            except TypeError:
                continue
            except Exception as exc:
                print(f"[DataManager] option chain failed for {underlying}: {exc}")
                continue
            if result is not None:
                return result
        return None

    def _load_complete_cache(self, symbol, start_date, end_date, timeframe, compression, refresh):
        """读取覆盖请求窗口的缓存；缺口或刷新请求均返回 None。"""
        if not getattr(config, 'CACHE_DATA', False) or refresh:
            return None
        try:
            cached = CsvDataProvider(self.data_path).get_data(
                symbol,
                timeframe=timeframe,
                compression=compression,
            )
        except Exception as exc:
            print(f"Failed to read cache for {symbol}: {exc}")
            return None
        if cached is None or cached.empty:
            return None
        cached = sanitize_market_dataframe(cached)
        if cached is None or cached.empty:
            print(f"Cached data for {symbol} contains no finite OHLCV rows.")
            return None
        index = pd.to_datetime(cached.index, errors='coerce')
        index = index[~index.isna()]
        if len(index) == 0:
            return None

        def boundary(value, is_end=False):
            if value is None or str(value).strip() == '':
                return None
            result = pd.Timestamp(value)
            is_intraday = str(timeframe or '').strip().lower() in {
                'minutes', 'minute', 'min', 'm', 'seconds', 'second', 'sec', 's',
            }
            if is_end and is_intraday and not re.search(r'[T\s]\d', str(value).strip()):
                result = result + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
            return result

        try:
            start_bound = boundary(start_date)
            end_bound = boundary(end_date, is_end=True)
            parsed = parse_option_symbol(symbol)
            raw_expiry = parsed.get('expiry') if parsed else None
            expiry = pd.Timestamp(raw_expiry) if raw_expiry is not None else pd.NaT
            if not pd.isna(expiry):
                # 到期后没有 K 线，结束边界裁到到期日。
                # 请求起点早于缓存时，只要已经接到到期日，就视为覆盖请求窗与上市区间的交集。
                expiry = expiry.normalize()
                if end_bound is None or end_bound > expiry:
                    end_bound = expiry
            elif end_bound is not None:
                daily = str(timeframe or "").strip().lower() in {"", "days", "day", "d"}
                if daily:
                    # 日线完整缓存把周末请求窗回退到周五，避免周六启动训练误判缺口。
                    while int(end_bound.weekday()) >= 5:
                        end_bound = end_bound - pd.Timedelta(days=1)
            index_tz = getattr(index, 'tz', None)
            is_intraday = str(timeframe or '').strip().lower() in {
                'minutes', 'minute', 'min', 'm', 'seconds', 'second', 'sec', 's',
            }
            start_date_only = bool(
                start_date is not None
                and not re.search(r'[T\s]\d', str(start_date).strip())
            )
            end_date_only = bool(
                end_date is not None
                and not re.search(r'[T\s]\d', str(end_date).strip())
            )

            def align(value):
                if value is None:
                    return None
                if index_tz is not None:
                    return value.tz_localize(index_tz) if value.tzinfo is None else value.tz_convert(index_tz)
                return value.tz_convert(None) if value.tzinfo is not None else value

            cache_min = index.min()
            cache_max = index.max()
            start_compare = align(start_bound)
            end_compare = align(end_bound)
            if is_intraday and start_date_only and start_compare is not None:
                start_compare = start_compare.normalize()
                cache_min = cache_min.normalize()
            if is_intraday and end_date_only and end_compare is not None:
                end_compare = end_compare.normalize()
                cache_max = cache_max.normalize()
            expiry_compare = align(expiry) if not pd.isna(expiry) else None
            if is_intraday and end_date_only and expiry_compare is not None:
                expiry_compare = expiry_compare.normalize()
            if start_compare is not None and cache_min > start_compare:
                # 期权缓存起点可以晚于请求窗；未接到到期日的残段仍算缺口。
                if expiry_compare is None or cache_max < expiry_compare:
                    return None
            if end_compare is not None and cache_max < end_compare:
                return None
        except Exception as exc:
            print(f"Invalid cache boundary for {symbol}: {exc}")
            return None
        return cached

    def _get_data_smart(self, symbol, start_date, end_date, timeframe: str, compression: int, refresh: bool):
        """
        默认链路不再使用 CSV 缓存，除非显式指定 data_source=csv。
        refresh 仅影响在线数据拉取与缓存回写行为。
        """
        online_providers = [
            p for p in self.providers
            if p.__class__.__name__.replace('DataProvider', '').lower() != 'csv'
            and not bool(self._explicit_provider_value(p, 'HYBRID_ONLY', False))
        ]

        return self._fetch_from_providers(
            symbol, start_date, end_date, online_providers,
            timeframe, compression, refresh=refresh,
        )

    def _fetch_from_providers(self, symbol, start_date, end_date, providers,
                              timeframe: str = 'Days', compression: int = 1,
                              refresh: bool = False):
        """
        遍历给定的提供者列表获取数据。
        如果成功且来源不是CSV，则执行缓存。
        """
        for provider in providers:
            provider_name = provider.__class__.__name__
            try:
                if bool(self._explicit_provider_value(provider, 'HYBRID_ONLY', False)):
                    try:
                        df = provider.get_data(
                            symbol, start_date, end_date, timeframe, compression,
                            refresh=refresh,
                        )
                    except TypeError as exc:
                        if 'refresh' not in str(exc):
                            raise
                        df = provider.get_data(symbol, start_date, end_date, timeframe, compression)
                else:
                    df = provider.get_data(symbol, start_date, end_date, timeframe, compression)
                if df is not None and not df.empty:
                    df = sanitize_market_dataframe(df)
                    if df is None or df.empty:
                        print(f"{provider_name} returned no finite OHLCV rows for {symbol}.")
                        continue

                    if (
                        not isinstance(provider, CsvDataProvider)
                        and not (
                            bool(self._explicit_provider_value(provider, 'HYBRID_ONLY', False))
                            and bool(getattr(self, '_live_mode', False))
                        )
                    ):
                        self._cache_data(df, symbol, timeframe, compression)

                    return df
            except Exception as e:
                print(f"Error in {provider_name}: {e}")
                continue
        return None

    def _cache_data(self, df: pd.DataFrame, symbol: str, timeframe: str = 'Days', compression: int = 1):
        """将数据按时间索引与既有缓存合并后写入，避免分段下载覆盖历史。"""
        if not getattr(config, 'CACHE_DATA', False):
            return
        CsvDataProvider.ensure_cache_dir(self.data_path)
        csv_filepath = CsvDataProvider.get_cache_filepath(self.data_path, symbol, timeframe, compression)
        temp_filepath = None
        try:
            incoming = sanitize_market_dataframe(df)
            if incoming is None or incoming.empty:
                return
            incoming.index = pd.to_datetime(incoming.index, errors='coerce', utc=True).tz_localize(None)
            incoming = incoming[~incoming.index.isna()]
            if os.path.isfile(csv_filepath):
                try:
                    existing = pd.read_csv(csv_filepath, index_col='datetime', parse_dates=True)
                    existing.index = pd.to_datetime(existing.index, errors='coerce', utc=True).tz_localize(None)
                    existing = sanitize_market_dataframe(existing)
                    merged = (
                        pd.concat([existing, incoming], axis=0, sort=False)
                        if existing is not None and not existing.empty
                        else incoming
                    )
                except Exception as cache_error:
                    print(f"缓存文件损坏，将以本次有效数据重建 {csv_filepath}: {cache_error}")
                    merged = incoming
            else:
                merged = incoming
            merged = sanitize_market_dataframe(merged)
            if merged is None or merged.empty:
                return
            merged.index = pd.to_datetime(merged.index, errors='coerce', utc=True).tz_localize(None)
            merged = merged[~merged.index.isna()]
            merged = merged[~merged.index.duplicated(keep='last')].sort_index()
            merged.index.name = 'datetime'
            temp_filepath = f"{csv_filepath}.tmp"
            merged.to_csv(temp_filepath, mode='w')
            os.replace(temp_filepath, csv_filepath)
        except Exception as e:
            if temp_filepath and os.path.isfile(temp_filepath):
                try:
                    os.remove(temp_filepath)
                except OSError:
                    pass
            print(f"Failed to cache data for {symbol}: {e}")
