import importlib
import inspect
import math
import os
import re
import threading
from collections import OrderedDict

import pandas as pd

import config
from common.data_paths import resolve_data_path, share_data_file
from .base_provider import BaseDataProvider
from .compositions import build_provider_composition
from .csv_provider import CsvDataProvider
from .thetadata_provider import ENRICHMENT_INCOMPLETE_COLUMN, enrichment_retry_needed
from common.options.analytics import parse_option_symbol
from common.options.events import normalize_dividend_calendar, normalize_earnings_calendar
from common.options.data_safety import sanitize_market_dataframe
from common.options.market_inputs import align_rate_curve, forward_dividend_yield_series


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
    if normalized in {
        'hybrid', 'option_hybrid', 'theta+futu', 'futu+theta',
        'thetadata+futu', 'futu+thetadata', 'theta_futu', 'futu_theta',
    }:
        return 'hybrid'
    if normalized == 'sxsc_tushare':
        return 'sxsctushare'
    return _PLATFORM_DEFAULT_SOURCES.get(normalized, normalized)


class DataManager:
    def __init__(self):
        self.providers = self.auto_discover_and_sort_providers()
        self.data_path = resolve_data_path(getattr(config, "DATA_PATH", ".data"))
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

    def _cache_root_for_source(self, specified_sources=None):
        """按辅助源自动选择 DATA_PATH 下的缓存子目录。"""
        base_root = getattr(self, "data_path", None) or resolve_data_path(
            getattr(config, "DATA_PATH", ".data")
        )
        if not specified_sources:
            return base_root
        names = self._split_source_names(specified_sources)
        if not any(name in {"sec", "earnings", "rates", "occ"} for name in names):
            return base_root
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", "_".join(names)) or "source"
        return os.path.join(base_root, "sources", safe)

    @staticmethod
    def _strip_unselected_auxiliary_columns(frame, valuation_providers):
        """不选择估值/基本面源时清除旧缓存残留列。"""
        if not isinstance(frame, pd.DataFrame) or valuation_providers:
            return frame
        prefixes = ("valuation_", "fundamental_")
        columns = [
            column for column in frame.columns
            if str(column).startswith(prefixes)
            or str(column) in {"underlying_fair_value", "underlying_weighted_value"}
        ]
        return frame.drop(columns=columns, errors="ignore") if columns else frame

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

    def _provider_supports_symbol(self, provider, symbol):
        """有 Provider 显式白名单时跳过不匹配标的，保留未知能力的既有回退行为。"""
        method = self._explicit_provider_method(provider, "supports_symbol")
        if method is None:
            return True
        try:
            return bool(method(symbol))
        except Exception:
            return True

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

    def _provider_supports_historical_option_quotes(self, provider):
        """判断组合 Provider 是否能为历史期权补充真实 NBBO。"""
        if bool(self._explicit_provider_value(provider, "HISTORICAL_OPTION_QUOTES", False)):
            return True
        for name in ("theta_provider", "historical_provider"):
            child = self._explicit_provider_value(provider, name, None)
            if child is not None and bool(
                self._explicit_provider_value(child, "HISTORICAL_OPTION_QUOTES", False)
            ):
                return True
        return False

    def _option_history_prefetch_provider(self, specified_sources):
        """查找所选源中的 Theta 历史批量取数能力。"""
        if specified_sources:
            providers = []
            for name in self._split_source_names(specified_sources):
                try:
                    provider = self._provider_for_source(name)
                except Exception:
                    continue
                if provider is not None:
                    providers.append(provider)
        else:
            providers = list(self._all_provider_instances())
        for provider in providers:
            candidates = [provider]
            for name in ("theta_provider", "historical_provider"):
                child = self._explicit_provider_value(provider, name, None)
                if child is not None:
                    candidates.append(child)
            for candidate in candidates:
                method = self._explicit_provider_method(candidate, "prefetch_option_histories")
                if callable(method):
                    return candidate
        return None

    @staticmethod
    def _split_source_names(specified_sources: str) -> list:
        """
        将 data_source 字符串拆分为 provider 名称列表。
        支持组合表达式、空格/逗号分隔和配置的嵌套别名。
        """
        if not specified_sources:
            return []
        raw = str(specified_sources).strip().lower()
        if not raw:
            return []
        compositions = getattr(config, 'DATA_PROVIDER_COMPOSITIONS', {})
        if not isinstance(compositions, dict):
            compositions = {}
        configured_aliases = getattr(config, 'DATA_PROVIDER_ALIASES', {})
        aliases = {}
        if isinstance(configured_aliases, dict):
            for alias, target in configured_aliases.items():
                key = str(alias or '').strip().lower()
                if (
                    key
                    and not re.search(r'[+,\s]', key)
                    and isinstance(target, str)
                    and target.strip()
                ):
                    aliases[key] = re.sub(r'\s*\+\s*', '+', target.strip().lower())

        def expand(expression, alias_stack=()):
            compact = re.sub(r'\s*\+\s*', '+', str(expression or '').strip().lower())
            if not compact:
                return []
            if compact in aliases:
                if compact in alias_stack:
                    cycle = ' -> '.join((*alias_stack, compact))
                    raise ValueError(f'Provider alias cycle: {cycle}')
                return expand(aliases[compact], (*alias_stack, compact))
            if compact in compositions:
                return [compact]
            if normalize_source_name(compact) == 'hybrid':
                return ['hybrid']

            sources = []
            for token in re.split(r'[,\s]+', compact):
                if not token:
                    continue
                if token in aliases:
                    sources.extend(expand(token, alias_stack))
                elif token in compositions:
                    sources.append(token)
                else:
                    sources.append(normalize_source_name(token))
            return sources

        return expand(raw)

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

    @staticmethod
    def _normalize_daily_sessions(frame, timeframe='Days', compression=1):
        """日线只保留每个交易日一根 K 线，并把时间戳对齐到交易日键。"""
        normalized_timeframe = str(timeframe or '').strip().lower()
        try:
            normalized_compression = int(compression or 1)
        except (TypeError, ValueError, OverflowError):
            normalized_compression = 1
        if normalized_timeframe not in {'', 'days', 'day', 'd'} or normalized_compression != 1:
            return frame
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return frame

        result = frame.copy()
        try:
            index = pd.to_datetime(result.index, errors='coerce', format='mixed')
        except (TypeError, ValueError):
            index = pd.to_datetime(result.index, errors='coerce')
        valid = ~pd.isna(index)
        if not bool(valid.all()):
            result = result.loc[valid].copy()
            index = index[valid]
        if result.empty:
            return result

        session_keys = pd.DatetimeIndex(index).normalize()
        keep_mask = ~session_keys.duplicated(keep='first')
        # 中间日期保留完整历史日线；窗口末日用最新快照反映最后可见状态。
        final_session = session_keys.max()
        final_positions = [
            position for position, session in enumerate(session_keys)
            if session == final_session
        ]
        if len(final_positions) > 1:
            keep_mask[final_positions] = False
            keep_mask[final_positions[-1]] = True
        result = result.loc[keep_mask].copy()
        result.index = session_keys[keep_mask]
        result.index.name = getattr(frame.index, 'name', None) or 'datetime'
        return result.sort_index()

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
                 specified_sources: str = None, timeframe: str = 'Days', compression: int = 1,
                 refresh: bool = False) -> pd.DataFrame:
        """
        智能获取数据。
        - 如果指定了 specified_sources，则按指定顺序尝试。
        - 否则，执行默认的责任链逻辑（带增量更新）。
        """
        origin = "fetch"
        final_df = None
        valuation_providers = []
        cache_root = self._cache_root_for_source(specified_sources)

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
            valuation_providers = [
                provider
                for provider in providers_to_use
                if callable(self._explicit_provider_method(provider, "get_valuation_snapshots"))
            ]
            price_providers = [
                provider
                for provider in providers_to_use
                if not bool(self._explicit_provider_value(provider, "VALUATION_ONLY", False))
                and not bool(self._explicit_provider_value(provider, "AUXILIARY_ONLY", False))
                and not bool(self._explicit_provider_value(provider, "EVENT_ONLY", False))
                and self._provider_supports_symbol(provider, symbol)
            ]
            if not price_providers:
                print(f"Error: data_source '{specified_sources}' contains no OHLCV provider for {symbol}.")
                return None
            require_option_quotes = bool(parse_option_symbol(symbol).get("option_type")) and any(
                self._provider_supports_historical_option_quotes(provider)
                for provider in price_providers
            )
            expected_price_adjustment = None
            if len(price_providers) == 1 and not parse_option_symbol(symbol):
                expected_price_adjustment = self._explicit_provider_value(
                    price_providers[0], "UNDERLYING_PRICE_ADJUSTMENT", None
                )
            # 估值辅助源不进入 OHLCV 回退链；唯一行情源仍可独立复用完整缓存。
            # 实盘必须重新读取在线事实；即使本地 CSV 覆盖完整窗口，也不能
            # 用历史缓存短路当前行情。回测/优化的 hybrid 正股与期权历史可以走 CSV。
            if (
                len(price_providers) == 1
                and not isinstance(price_providers[0], CsvDataProvider)
                and not bool(getattr(self, '_live_mode', False))
            ):
                final_df = self._load_complete_cache(
                    symbol,
                    start_date,
                    end_date,
                    timeframe,
                    compression,
                    refresh,
                    require_option_quotes=require_option_quotes,
                    expected_price_adjustment=expected_price_adjustment,
                    cache_root=cache_root,
                )
                if final_df is not None and not final_df.empty:
                    origin = "cache"
            else:
                final_df = None
            if final_df is None or final_df.empty:
                final_df = self._fetch_from_providers(
                    symbol, start_date, end_date, price_providers,
                    timeframe, compression, refresh=refresh, cache_root=cache_root,
                )
                origin = "fetch"

        # 路径二: 执行默认的责任链逻辑
        else:
            final_df = self._get_data_smart(symbol, start_date, end_date, timeframe, compression, refresh)

        # 【最终切片】确保返回的数据在请求的日期范围内
        if final_df is not None and not final_df.empty:
            final_df = self._strip_unselected_auxiliary_columns(
                final_df, valuation_providers,
            )
            if valuation_providers and not parse_option_symbol(symbol):
                from common.options.valuation import merge_valuation_snapshots

                for provider in valuation_providers:
                    method = self._explicit_provider_method(provider, "get_valuation_snapshots")
                    if method is None:
                        continue
                    try:
                        snapshots = method(symbol, start_date=start_date, end_date=end_date)
                    except Exception as exc:
                        print(f"[DataManager] valuation snapshot failed for {symbol}: {exc}")
                        snapshots = None
                    final_df = merge_valuation_snapshots(
                        final_df,
                        snapshots,
                        symbol,
                        max_age_days=self._explicit_provider_value(
                            provider, "VALUATION_MAX_AGE_DAYS", None
                        ),
                    )
                    if snapshots is not None:
                        break
                if bool(getattr(config, "CACHE_DATA", False)) and not bool(
                    getattr(self, "_live_mode", False)
                ):
                    self._cache_data(
                        final_df, symbol, timeframe, compression, cache_root=cache_root,
                    )

            if parse_option_symbol(symbol).get("option_type"):
                self._attach_occ_contract_metadata(
                    final_df,
                    symbol,
                    providers_to_use,
                    start_date=start_date,
                    end_date=end_date,
                )

            final_df = self._normalize_daily_sessions(
                final_df,
                timeframe=timeframe,
                compression=compression,
            )

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

            # 切片后再标记，避免布尔索引丢掉来源。进度行用它区分缓存和实拉。
            if not final_df.empty:
                final_df.attrs["data_origin"] = origin
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

    def get_earnings_calendar(
        self,
        symbols=None,
        start_date: str = None,
        end_date: str = None,
        specified_sources: str = None,
        as_of: str = None,
    ):
        """通过选定 data_source 查询财报日，不让策略直接依赖 Provider SDK。"""
        if specified_sources:
            providers = []
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

        fallback = None
        for provider in providers:
            method = self._explicit_provider_method(provider, "get_earnings_calendar")
            declared = next(
                (base.__dict__.get("get_earnings_calendar") for base in type(provider).__mro__
                 if "get_earnings_calendar" in base.__dict__),
                None,
            )
            if method is None or declared is BaseDataProvider.get_earnings_calendar:
                continue
            try:
                signature = inspect.signature(method)
                kwargs = {
                    "symbols": symbols,
                    "start_date": start_date,
                    "end_date": end_date,
                    "as_of": as_of,
                }
                if not any(item.kind == inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()):
                    kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
                result = method(**kwargs)
            except TypeError:
                continue
            except Exception as exc:
                print(f"[DataManager] earnings calendar failed: {exc}")
                continue
            normalized = normalize_earnings_calendar(
                result, symbols=symbols, start_date=start_date, end_date=end_date,
            )
            normalized_attrs = dict(getattr(result, "attrs", {}) or {})
            if normalized is not None and (
                not normalized.empty or bool(normalized_attrs.get("as_of_supported", False))
            ):
                normalized.attrs.update(normalized_attrs)
                if bool(normalized_attrs.get("as_of_supported", False)):
                    return normalized
                if fallback is None:
                    fallback = normalized
        return fallback

    def attach_earnings_calendar(
        self, datas, symbols=None, start_date: str = None, end_date: str = None,
        specified_sources: str = None, as_of: str = None,
    ):
        """把 data_source 财报快照附加到行情 attrs，供回测和优化复用。"""
        from common.options.events import attach_earnings_calendar

        events = self.get_earnings_calendar(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            specified_sources=specified_sources,
            as_of=as_of,
        )
        return attach_earnings_calendar(datas, events)

    def get_rate_curve(self, start_date=None, end_date=None, specified_sources=None, as_of=None, deadline=None):
        """从指定辅助源读取动态无风险利率曲线。"""
        names = self._split_source_names(specified_sources) if specified_sources else []
        providers = []
        for name in names:
            try:
                provider = self._provider_for_source(name)
            except Exception:
                continue
            if provider is not None:
                providers.append(provider)
        if not providers:
            providers = list(self._all_provider_instances())
        for provider in providers:
            method = self._explicit_provider_method(provider, "get_rate_curve")
            if method is None:
                continue
            try:
                result = method(
                    start_date=start_date, end_date=end_date, as_of=as_of, deadline=deadline,
                )
            except TypeError:
                result = method(start_date, end_date)
            except Exception as exc:
                print(f"[DataManager] rate curve failed: {exc}")
                continue
            if isinstance(result, pd.DataFrame) and not result.empty:
                return result
        return None

    def get_option_contract_adjustments(
        self, symbols=None, start_date=None, end_date=None,
        specified_sources=None, as_of=None,
    ):
        """通过选定辅助源读取 OCC 合约调整事实。"""
        names = self._split_source_names(specified_sources) if specified_sources else []
        providers = []
        for name in names:
            try:
                provider = self._provider_for_source(name)
            except Exception:
                continue
            if provider is not None:
                providers.append(provider)
        if not providers:
            providers = list(self._all_provider_instances())
        for provider in providers:
            method = self._explicit_provider_method(provider, "get_contract_adjustments")
            if method is None:
                continue
            try:
                result = method(
                    symbols=symbols,
                    start_date=start_date,
                    end_date=end_date,
                    as_of=as_of,
                )
            except TypeError:
                result = method(symbols, start_date, end_date)
            except Exception as exc:
                print(f"[DataManager] OCC adjustment query failed: {exc}")
                continue
            if isinstance(result, pd.DataFrame):
                return result
        return None

    def get_option_history_quotes(
        self, symbol, start_date=None, end_date=None, specified_sources=None,
        interval="1m", full_session=True, refresh=False,
    ):
        """查询全天历史 NBBO；调用方显式选择该高容量研究接口。"""
        names = self._split_source_names(specified_sources) if specified_sources else []
        providers = []
        for name in names:
            try:
                provider = self._provider_for_source(name)
            except Exception:
                continue
            if provider is not None:
                providers.append(provider)
        if not providers:
            providers = list(self._all_provider_instances())
        for provider in providers:
            method = self._explicit_provider_method(provider, "get_option_history_quotes")
            if method is None:
                for name in ("theta_provider", "historical_provider"):
                    child = self._explicit_provider_value(provider, name, None)
                    method = self._explicit_provider_method(child, "get_option_history_quotes") if child is not None else None
                    if method is not None:
                        break
            if method is None:
                continue
            try:
                result = method(
                    symbol, start_date, end_date, interval,
                    full_session=full_session, refresh=refresh,
                )
            except TypeError:
                result = method(symbol, start_date, end_date, interval)
            except Exception as exc:
                print(f"[DataManager] option history quotes failed for {symbol}: {exc}")
                continue
            if isinstance(result, pd.DataFrame) and not result.empty:
                return result
        return None

    def get_option_history_trades(
        self, symbol, start_date=None, end_date=None, specified_sources=None,
        interval="1m", full_session=True, refresh=False,
    ):
        """查询全天历史成交；权限或接口不可用时返回 None。"""
        names = self._split_source_names(specified_sources) if specified_sources else []
        providers = []
        for name in names:
            try:
                provider = self._provider_for_source(name)
            except Exception:
                continue
            if provider is not None:
                providers.append(provider)
        if not providers:
            providers = list(self._all_provider_instances())
        for provider in providers:
            method = self._explicit_provider_method(provider, "get_option_history_trades")
            if method is None:
                for name in ("theta_provider", "historical_provider"):
                    child = self._explicit_provider_value(provider, name, None)
                    method = self._explicit_provider_method(child, "get_option_history_trades") if child is not None else None
                    if method is not None:
                        break
            if method is None:
                continue
            try:
                result = method(
                    symbol, start_date, end_date, interval,
                    full_session=full_session, refresh=refresh,
                )
            except TypeError:
                result = method(symbol, start_date, end_date, interval)
            except Exception as exc:
                print(f"[DataManager] option history trades failed for {symbol}: {exc}")
                continue
            if isinstance(result, pd.DataFrame) and not result.empty:
                return result
        return None

    def attach_market_inputs(
        self, datas, symbols=None, start_date=None, end_date=None,
        specified_sources=None, as_of=None, deadline=None,
    ):
        """把动态利率和可见分红输入附加到正股及期权行情。"""
        frames = datas or {}
        if not frames:
            return frames
        curve = self.get_rate_curve(
            start_date=start_date, end_date=end_date,
            specified_sources=specified_sources, as_of=as_of, deadline=deadline,
        )
        if curve is None or curve.empty:
            return frames
        if not any(
            bool((getattr(frame, "attrs", {}) or {}).get("dividend_calendar_available", False))
            for frame in frames.values()
            if isinstance(frame, pd.DataFrame)
        ):
            try:
                self.attach_dividend_calendar(
                    frames,
                    symbols=symbols or list(frames.keys()),
                    start_date=start_date,
                    end_date=end_date,
                    specified_sources=specified_sources,
                    as_of=as_of,
                )
            except Exception as exc:
                print(f"[DataManager] dividend inputs unavailable: {exc}")
        events_by_key = {}
        for symbol, frame in frames.items():
            attrs = dict(getattr(frame, "attrs", {}) or {})
            events_by_key[self._symbol_underlying_key(symbol)] = tuple(
                attrs.get("dividend_calendar", ()) or ()
            )
        for symbol, frame in frames.items():
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            frame["risk_free_rate"] = align_rate_curve(frame.index, curve).to_numpy()
            parsed = parse_option_symbol(str(symbol))
            if parsed.get("option_type"):
                key = self._symbol_underlying_key(symbol)
                events = events_by_key.get(key, ())
                expiry = pd.Series(pd.Timestamp(parsed.get("expiry")), index=frame.index)
                # 期权 close 是期权权利金，不能当作正股 spot 计算股息率。
                if "spot" in frame.columns:
                    spot = pd.to_numeric(frame["spot"], errors="coerce")
                    frame["dividend_yield"] = forward_dividend_yield_series(
                        frame.index, expiry, spot, events,
                    ).to_numpy()
                else:
                    frame["dividend_yield"] = float("nan")
            attrs = dict(getattr(frame, "attrs", {}) or {})
            attrs["risk_free_curve"] = curve.copy()
            attrs["market_inputs_available"] = True
            attrs["market_inputs_source"] = str(curve.attrs.get("source", "rates"))
            frame.attrs = attrs
        return frames

    @staticmethod
    def _symbol_underlying_key(symbol):
        parsed = parse_option_symbol(str(symbol or ""))
        raw = parsed.get("underlying") if parsed.get("option_type") else str(symbol or "")
        from common.options.analytics import underlying_key
        return underlying_key(raw)

    def _attach_occ_contract_metadata(self, frame, symbol, providers, start_date=None, end_date=None):
        """将 OCC 已确认的调整乘数写入期权行情，不把未知调整伪装成标准合约。"""
        for provider in providers:
            method = self._explicit_provider_method(provider, "get_option_contract_metadata")
            if method is None:
                continue
            try:
                metadata = method(symbol, as_of=end_date)
            except Exception as exc:
                print(f"[DataManager] OCC metadata failed for {symbol}: {exc}")
                continue
            if not isinstance(metadata, dict):
                continue
            status = str(metadata.get("status", "unknown"))
            frame.attrs["occ_adjustment_status"] = status
            frame.attrs["occ_adjustment_source_id"] = metadata.get("source_id", "")
            if status != "adjusted":
                return
            multiplier = pd.to_numeric(metadata.get("new_multiplier"), errors="coerce")
            if pd.notna(multiplier) and float(multiplier) > 0:
                frame["contract_multiplier"] = float(multiplier)
                frame.attrs["contract_multiplier"] = float(multiplier)
            for field in ("new_strike", "new_underlying", "adjustment_type", "effective_date"):
                value = metadata.get(field)
                if value is not None and not (isinstance(value, float) and pd.isna(value)):
                    frame[f"occ_{field}"] = value
            return

    def get_dividend_calendar(
        self,
        symbols=None,
        start_date: str = None,
        end_date: str = None,
        specified_sources: str = None,
        as_of: str = None,
        deadline: float = None,
    ):
        """通过选定 data_source 查询带公告日的除息事件。"""
        if specified_sources:
            providers = []
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
            method = self._explicit_provider_method(provider, "get_dividend_calendar")
            declared = next(
                (base.__dict__.get("get_dividend_calendar") for base in type(provider).__mro__
                 if "get_dividend_calendar" in base.__dict__),
                None,
            )
            if method is None or declared is BaseDataProvider.get_dividend_calendar:
                continue
            try:
                signature = inspect.signature(method)
                kwargs = {
                    "symbols": symbols,
                    "start_date": start_date,
                    "end_date": end_date,
                    "as_of": as_of,
                    "deadline": deadline,
                }
                if not any(item.kind == inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()):
                    kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
                result = method(**kwargs)
            except TypeError:
                continue
            except Exception as exc:
                print(f"[DataManager] dividend calendar failed: {exc}")
                continue
            normalized = normalize_dividend_calendar(
                result, symbols=symbols, start_date=start_date, end_date=end_date, as_of=as_of,
            )
            if normalized is None:
                continue
            if isinstance(result, pd.DataFrame) and not result.empty and normalized.empty:
                print("[DataManager] dividend calendar contained no valid normalized events; treating it as unavailable.")
                continue
            attrs = dict(getattr(result, "attrs", {}) or {})
            supported = bool(attrs.get(
                "historical_as_of_supported", attrs.get("as_of_supported", False)
            ))
            current_supported = bool(attrs.get(
                "current_snapshot_supported", attrs.get("as_of_supported", False)
            ))
            fields_ready = normalized.empty or (
                normalized["announcement_date"].notna().all()
                and pd.to_numeric(normalized["cash_per_share"], errors="coerce").map(
                    lambda value: math.isfinite(float(value)) and float(value) > 0
                    if pd.notna(value) else False
                ).all()
                and normalized["currency"].astype("string").fillna("").str.strip().ne("").all()
            )
            normalized.attrs.update(attrs)
            normalized.attrs["as_of_supported"] = bool(supported and fields_ready)
            normalized.attrs["current_snapshot_supported"] = bool(current_supported and fields_ready)
            return normalized
        return None

    def attach_dividend_calendar(
        self,
        datas,
        symbols=None,
        start_date: str = None,
        end_date: str = None,
        specified_sources: str = None,
        as_of: str = None,
        deadline: float = None,
    ):
        """把带公告日的分红快照附加到行情 attrs。"""
        from common.options.events import attach_dividend_calendar

        events = self.get_dividend_calendar(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            specified_sources=specified_sources,
            as_of=as_of,
            deadline=deadline,
        )
        return attach_dividend_calendar(datas, events)

    def prefetch_option_histories(
        self,
        symbols,
        start_date,
        end_date,
        *,
        specified_sources=None,
        timeframe="Days",
        compression=1,
        refresh=False,
    ):
        """按到期日批量预取未缓存期权，并逐合约写入可复用行情缓存。"""
        if (
            not specified_sources
            or not getattr(config, "CACHE_DATA", False)
            or refresh
            or getattr(self, "_live_mode", False)
            or str(timeframe or "").strip().lower() not in {"", "days", "day", "d"}
        ):
            return 0
        try:
            if int(compression or 1) != 1:
                return 0
        except (TypeError, ValueError, OverflowError):
            return 0
        option_symbols = [
            str(symbol).strip().upper()
            for symbol in symbols or []
            if parse_option_symbol(str(symbol)).get("option_type")
        ]
        if len(option_symbols) < 2:
            return 0
        cache_root = self._cache_root_for_source(specified_sources)
        provider = self._option_history_prefetch_provider(specified_sources)
        if provider is None:
            return 0
        missing = []
        for symbol in option_symbols:
            cached = self._load_complete_cache(
                symbol,
                start_date,
                end_date,
                timeframe,
                compression,
                False,
                require_option_quotes=self._provider_supports_historical_option_quotes(provider),
                cache_root=cache_root,
            )
            if cached is None:
                missing.append(symbol)
        if len(missing) < 2:
            return 0
        saved = 0

        def save_ready(symbol, frame):
            nonlocal saved
            self._cache_data(
                frame, symbol, timeframe, compression, cache_root=cache_root,
            )
            saved += 1

        method = self._explicit_provider_method(provider, "prefetch_option_histories")
        try:
            method(missing, start_date, end_date, on_ready=save_ready)
        except Exception as exc:
            print(f"[DataManager] option history batch prefetch failed; continuing per contract: {exc}")
        return saved


    def _cache_roots(self):
        """只返回一个行情根。相对路径已锚定 run.py 同级，避免两份缓存混读。"""
        return [resolve_data_path(self.data_path)]

    def _load_complete_cache(
        self, symbol, start_date, end_date, timeframe, compression, refresh,
        require_option_quotes=False, expected_price_adjustment=None, cache_root=None,
    ):
        """读取覆盖请求窗口的缓存；缺口或刷新请求均返回 None。"""
        if not getattr(config, 'CACHE_DATA', False) or refresh:
            return None

        def covered(frame):
            index = pd.to_datetime(frame.index, errors='coerce')
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
                    # 股票缓存必须覆盖请求起点；期权合约上市晚于请求起点时，
                    # 只需覆盖请求结束日（若请求结束日在到期日之后则裁到到期日）。
                    if expiry_compare is None:
                        return None
                    required_end = expiry_compare
                    if end_compare is not None and end_compare < expiry_compare:
                        required_end = end_compare
                    if cache_max < required_end:
                        return None
                if end_compare is not None and cache_max < end_compare:
                    return None
                if expected_price_adjustment:
                    adjustment_column = "futu_price_adjustment_type"
                    if adjustment_column not in frame.columns:
                        return None
                    requested_rows = pd.Series(True, index=frame.index)
                    frame_index = pd.DatetimeIndex(index)
                    if start_compare is not None:
                        requested_rows &= frame_index >= start_compare
                    if end_compare is not None:
                        requested_rows &= frame_index <= end_compare
                    selected = frame.loc[requested_rows, adjustment_column]
                    expected = str(expected_price_adjustment).strip().upper()
                    if selected.empty or not selected.astype("string").str.upper().eq(expected).all():
                        return None
            except Exception as exc:
                print(f"Invalid cache boundary for {symbol}: {exc}")
                return None
            return frame

        root = resolve_data_path(cache_root or self.data_path)
        try:
            candidate = CsvDataProvider(root).get_data(
                symbol,
                timeframe=timeframe,
                compression=compression,
            )
        except Exception as exc:
            print(f"Failed to read cache for {symbol}: {exc}")
            return None
        if candidate is None or candidate.empty:
            return None
        candidate = sanitize_market_dataframe(candidate)
        if candidate is None or candidate.empty:
            print(f"Cached data for {symbol} contains no finite OHLCV rows.")
            return None
        if enrichment_retry_needed(candidate, symbol, require_quotes=require_option_quotes):
            return None
        return covered(candidate)

    def _get_data_smart(self, symbol, start_date, end_date, timeframe: str, compression: int, refresh: bool):
        """
        默认链路不再使用 CSV 缓存，除非显式指定 data_source=csv。
        refresh 仅影响在线数据拉取与缓存回写行为。
        """
        online_providers = [
            p for p in self.providers
            if p.__class__.__name__.replace('DataProvider', '').lower() != 'csv'
            and not bool(self._explicit_provider_value(p, 'HYBRID_ONLY', False))
            and not bool(self._explicit_provider_value(p, 'AUXILIARY_ONLY', False))
            and not bool(self._explicit_provider_value(p, 'EVENT_ONLY', False))
        ]

        return self._fetch_from_providers(
            symbol, start_date, end_date, online_providers,
            timeframe, compression, refresh=refresh,
        )

    def _fetch_from_providers(self, symbol, start_date, end_date, providers,
                              timeframe: str = 'Days', compression: int = 1,
                              refresh: bool = False, cache_root=None):
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
                    df = self._normalize_daily_sessions(
                        df,
                        timeframe=timeframe,
                        compression=compression,
                    )

                    if (
                        not isinstance(provider, CsvDataProvider)
                        and not (
                            bool(self._explicit_provider_value(provider, 'HYBRID_ONLY', False))
                            and bool(getattr(self, '_live_mode', False))
                        )
                    ):
                        self._cache_data(
                            df, symbol, timeframe, compression, cache_root=cache_root,
                        )

                    return df
            except Exception as e:
                print(f"Error in {provider_name}: {e}")
                continue
        return None

    def _cache_data(self, df: pd.DataFrame, symbol: str, timeframe: str = 'Days', compression: int = 1,
                    cache_root=None):
        """将数据按时间索引与既有缓存合并后写入，避免分段下载覆盖历史。"""
        if not getattr(config, 'CACHE_DATA', False):
            return
        root = resolve_data_path(cache_root or self.data_path)
        CsvDataProvider.ensure_cache_dir(root)
        csv_filepath = CsvDataProvider.get_cache_filepath(root, symbol, timeframe, compression)
        temp_filepath = None
        try:
            incoming = sanitize_market_dataframe(df)
            if incoming is None or incoming.empty:
                return
            incoming.index = pd.to_datetime(incoming.index, errors='coerce', utc=True).tz_localize(None)
            incoming = incoming[~incoming.index.isna()]
            incoming = self._normalize_daily_sessions(
                incoming,
                timeframe=timeframe,
                compression=compression,
            )
            if os.path.isfile(csv_filepath):
                try:
                    existing = pd.read_csv(csv_filepath, index_col='datetime', parse_dates=True)
                    existing.index = pd.to_datetime(existing.index, errors='coerce', utc=True).tz_localize(None)
                    existing = sanitize_market_dataframe(existing)
                    if existing is not None and not existing.empty:
                        existing = self._normalize_daily_sessions(
                            existing,
                            timeframe=timeframe,
                            compression=compression,
                        )
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
            merged = self._normalize_daily_sessions(
                merged,
                timeframe=timeframe,
                compression=compression,
            )
            merged.index.name = 'datetime'
            if ENRICHMENT_INCOMPLETE_COLUMN in merged.columns:
                incoming_incomplete = False
                if ENRICHMENT_INCOMPLETE_COLUMN in incoming.columns:
                    incoming_flags = pd.to_numeric(
                        incoming[ENRICHMENT_INCOMPLETE_COLUMN], errors="coerce"
                    ).fillna(0)
                    incoming_incomplete = bool(incoming_flags.gt(0).any())
                covers_existing = (
                    not incoming_incomplete
                    and len(incoming.index) > 0
                    and incoming.index.min() <= merged.index.min()
                    and incoming.index.max() >= merged.index.max()
                )
                flags = pd.to_numeric(merged[ENRICHMENT_INCOMPLETE_COLUMN], errors="coerce").fillna(0)
                # 本次完整结果已覆盖旧缓存区间时，清掉超时标记，下次直接复用。
                if covers_existing or not bool(flags.gt(0).any()):
                    merged = merged.drop(columns=[ENRICHMENT_INCOMPLETE_COLUMN])
            temp_filepath = f"{csv_filepath}.tmp"
            merged.to_csv(temp_filepath, mode='w')
            os.replace(temp_filepath, csv_filepath)
            share_data_file(csv_filepath)
        except Exception as e:
            if temp_filepath and os.path.isfile(temp_filepath):
                try:
                    os.remove(temp_filepath)
                except OSError:
                    pass
            print(f"Failed to cache data for {symbol}: {e}")
