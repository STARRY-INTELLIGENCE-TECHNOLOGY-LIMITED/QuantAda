"""富途官方交易 API 的实盘适配器。

本模块只负责账户、持仓、订单和撤单，不实现历史行情 Provider。历史 K 线由
``data_providers.futu_provider.FutuDataProvider`` 独立管理；两者可以各自使用
一个 OpenD 连接，也可以由调用方通过 context 注入共享连接。
"""

import copy
import datetime
import math
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

import pandas as pd

import config
from common import runtime_notifications
from common.log import coerce_dt
from common.live_schedule import LiveScheduleRunner
from common.order_quantity import positive_quantity, quantity_number
from common.live_runtime import dependency_install_hint, runtime_print
from live_trader.data_bridge.data_warm import SchedulePlanner

from .base_broker import BaseLiveBroker, BaseOrderProxy
from .futu_symbols import (
    OPTION_CODE_RE as _OPTION_CODE_RE,
    normalize_futu_symbol,
)


try:
    import futu as _futu
except Exception as exc:
    _futu = None
    _FUTU_IMPORT_ERROR = exc
    print(dependency_install_hint('futu-api', exc))
else:
    _FUTU_IMPORT_ERROR = None

ModifyOrderOp = getattr(_futu, 'ModifyOrderOp', None)
OpenQuoteContext = getattr(_futu, 'OpenQuoteContext', None)
OpenSecTradeContext = getattr(_futu, 'OpenSecTradeContext', None)
OrderType = getattr(_futu, 'OrderType', None)
RET_OK = getattr(_futu, 'RET_OK', 0)
SecurityFirm = getattr(_futu, 'SecurityFirm', None)
SysConfig = getattr(_futu, 'SysConfig', None)
TimeInForce = getattr(_futu, 'TimeInForce', None)
TrdEnv = getattr(_futu, 'TrdEnv', None)
TrdMarket = getattr(_futu, 'TrdMarket', None)
TrdSide = getattr(_futu, 'TrdSide', None)
TradeOrderHandlerBase = getattr(_futu, 'TradeOrderHandlerBase', None)
# 部分 futu-api 版本没有把行情 handler 基类从包根导出，但仍保留在
# ``futu.quote.quote_response_handler``。优先使用包根导出，缺失时回退到稳定的
# 子模块路径，避免事件模式在旧版 SDK 中被误判为不可用。
try:
    from futu.quote.quote_response_handler import (
        CurKlineHandlerBase as _CurKlineHandlerBase,
        StockQuoteHandlerBase as _StockQuoteHandlerBase,
        RTDataHandlerBase as _RTDataHandlerBase,
        TickerHandlerBase as _TickerHandlerBase,
    )
except Exception:
    _CurKlineHandlerBase = _StockQuoteHandlerBase = None
    _RTDataHandlerBase = _TickerHandlerBase = None
if TradeOrderHandlerBase is None:
    try:
        from futu.trade.trade_response_handler import (
            TradeOrderHandlerBase as _TradeOrderHandlerBase,
        )
    except Exception:
        _TradeOrderHandlerBase = None
    TradeOrderHandlerBase = _TradeOrderHandlerBase
CurKlineHandlerBase = getattr(_futu, 'CurKlineHandlerBase', None) or _CurKlineHandlerBase
StockQuoteHandlerBase = getattr(_futu, 'StockQuoteHandlerBase', None) or _StockQuoteHandlerBase
RTDataHandlerBase = getattr(_futu, 'RTDataHandlerBase', None) or _RTDataHandlerBase
TickerHandlerBase = getattr(_futu, 'TickerHandlerBase', None) or _TickerHandlerBase
SubType = getattr(_futu, 'SubType', None)


def _require_futu_trade_sdk():
    """在真正使用 Futu 交易上下文前给出可执行的依赖安装指引。"""
    if OpenSecTradeContext is None:
        raise ImportError(dependency_install_hint('futu-api', _FUTU_IMPORT_ERROR))


_CONTEXT_INIT_TIMEOUT_SECONDS = 5.0
_QUERY_TIMEOUT_SECONDS = 5.0
_QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS = 5.0
_FX_RATE_CACHE_SECONDS = 30.0
_MARKET_CURRENCIES = {
    'HK': 'HKD',
    'SH': 'CNH',
    'SZ': 'CNH',
    'US': 'USD',
    'SG': 'SGD',
    'JP': 'JPY',
    'AU': 'AUD',
    'CA': 'CAD',
    'MY': 'MYR',
    'HK_FUTURE': 'HKD',
    'CRYPTO': 'USD',
}
_CURRENCY_CASH_FIELDS = {
    'HKD': ('hkd_net_cash_power', 'hk_cash'),
    'USD': ('usd_net_cash_power', 'us_cash'),
    'CNH': ('cnh_net_cash_power', 'cn_cash'),
    'JPY': ('jpy_net_cash_power', 'jp_cash'),
    'SGD': ('sgd_net_cash_power', 'sg_cash'),
    'AUD': ('aud_net_cash_power', 'au_cash'),
    'CAD': ('cad_net_cash_power', 'ca_cash'),
    'MYR': ('myr_net_cash_power', 'my_cash'),
    'NZD': ('nzd_net_cash_power', 'nz_cash'),
}
_TRADABLE_MARKET_STATES = {
    'AUCTION',
    'MORNING',
    'AFTERNOON',
    'TRADE_AT_LAST',
    'NIGHT',
    'NIGHT_OPEN',
    'OVERNIGHT',
    'FUTURE_DAY_OPEN',
    'FUTURE_BREAK_OVER',
    'FUTURE_OPEN',
    'FUTURE_AFTERNOON',
    'ASHARE_AFTER_HOURS_BEGIN',
    'STIB_AFTER_HOURS_BEGIN',
}
_EXTENDED_MARKET_STATES = {
    'PRE_MARKET_BEGIN',
    'PRE_MARKET_END',
    'AFTER_HOURS_BEGIN',
}
_FUTU_EVENT_SUBTYPE_BASES = {
    'K_1M': CurKlineHandlerBase,
    'K_3M': CurKlineHandlerBase,
    'K_5M': CurKlineHandlerBase,
    'K_10M': CurKlineHandlerBase,
    'K_15M': CurKlineHandlerBase,
    'K_30M': CurKlineHandlerBase,
    'K_60M': CurKlineHandlerBase,
    'K_120M': CurKlineHandlerBase,
    'K_180M': CurKlineHandlerBase,
    'K_240M': CurKlineHandlerBase,
    'K_DAY': CurKlineHandlerBase,
    'K_WEEK': CurKlineHandlerBase,
    'K_MON': CurKlineHandlerBase,
    'QUOTE': StockQuoteHandlerBase,
    'RT_DATA': RTDataHandlerBase,
    'TICKER': TickerHandlerBase,
}
_FUTU_EVENT_TIME_FIELDS = (
    'time_key', 'datetime', 'update_time', 'data_time', 'time', 'created_at',
)

def _field(value, names, default=None):
    """兼容 DataFrame 行、字典和 SDK 属性对象。"""
    if value is None:
        return default
    if isinstance(names, str):
        names = (names,)
    for name in names:
        if isinstance(value, dict):
            if name in value:
                return value.get(name)
            lower_name = name.lower()
            for key, item in value.items():
                if str(key).lower() == lower_name:
                    return item
        if isinstance(value, pd.Series):
            if name in value.index:
                return value.get(name)
        try:
            item = getattr(value, name)
        except Exception:
            item = None
        if item is not None:
            return item
    return default


def _text(value, default=''):
    """将 SDK 字段转换为去空白字符串。"""
    if value is None:
        return default
    if isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    return str(value).strip()


def _decimal(value, default=None):
    """解析有限十进制数量；无效值返回 default。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    if not result.is_finite():
        return default
    return result


def _enum_value(enum_type, value, fallback):
    """将配置中的枚举名称转换为 SDK 值，并兼容测试替身/旧 SDK。"""
    if value is None:
        value = fallback
    if enum_type is None or not isinstance(value, str):
        return value
    key = value.strip().upper()
    return getattr(enum_type, key, value)


def _enum_name(value):
    """读取 SDK 枚举或字符串的稳定大写名称。"""
    raw = getattr(value, 'value', value)
    return _text(raw).upper().replace('-', '_').replace(' ', '_')


def _rows(value):
    """把 SDK 返回的表格或单行对象转换成行列表。"""
    if value is None:
        return []
    if isinstance(value, pd.DataFrame):
        return value.to_dict('records')
    if isinstance(value, pd.Series):
        return [value.to_dict()]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _futu_event_subtype_name(raw_value, timeframe='Days', compression=1):
    """把事件订阅配置转换为 Futu SubType 名称。"""
    explicit = _text(raw_value).upper().replace('-', '_')
    if explicit:
        if explicit in _FUTU_EVENT_SUBTYPE_BASES:
            return explicit
        if explicit.startswith('SUBTYPE.'):
            explicit = explicit.split('.', 1)[1]
        if explicit in _FUTU_EVENT_SUBTYPE_BASES:
            return explicit
        raise ValueError(
            f'Unsupported Futu event_subtype: {raw_value}; '
            'expected K_1M/K_5M/K_DAY/K_WEEK/K_MON/QUOTE/RT_DATA/TICKER.'
        )

    normalized = _text(timeframe, 'Days').lower()
    try:
        period = int(compression or 1)
    except (TypeError, ValueError, OverflowError):
        period = 1
    if period <= 0:
        raise ValueError(f'Invalid Futu subscription compression: {compression!r}')
    if normalized in {'days', 'day', 'd'}:
        if period != 1:
            raise ValueError(
                'Futu event subscription supports daily K-lines only with compression=1; '
                'use schedule mode for multi-day aggregation.'
            )
        return 'K_DAY'
    if normalized in {'weeks', 'week', 'w'}:
        if period != 1:
            raise ValueError(
                'Futu event subscription supports weekly K-lines only with compression=1.'
            )
        return 'K_WEEK'
    if normalized in {'months', 'month', 'mon'}:
        if period != 1:
            raise ValueError(
                'Futu event subscription supports monthly K-lines only with compression=1.'
            )
        return 'K_MON'
    if normalized in {'minutes', 'minute', 'min', 'm'}:
        subtype = f'K_{period}M'
        if subtype in _FUTU_EVENT_SUBTYPE_BASES:
            return subtype
        raise ValueError(
            f'Unsupported Futu subscription minute compression: {compression!r}; '
            'supported values are 1, 3, 5, 10, 15, 30, 60, 120, 180 and 240.'
        )
    if normalized in {'seconds', 'second', 'sec', 's'}:
        raise ValueError(
            'Futu event subscription has no second-level K-line subtype; '
            'use timeframe=Minutes or another broker event source.'
        )
    raise ValueError(f'Unsupported Futu subscription timeframe: {timeframe!r}')


def _futu_event_subtype_value(subtype_name):
    """把 SubType 名称映射为 SDK 值；旧版/测试替身保留字符串。"""
    return _enum_value(SubType, subtype_name, subtype_name)


def _futu_event_timestamp(content, target_tz=None):
    """从订阅回调内容中提取最新事件时间。"""
    candidates = []
    for row in _rows(content):
        raw_time = _field(row, _FUTU_EVENT_TIME_FIELDS, None)
        if raw_time in (None, ''):
            continue
        try:
            timestamp = pd.Timestamp(raw_time)
        except Exception:
            continue
        if pd.isna(timestamp):
            continue
        if timestamp.tzinfo is None and target_tz is not None:
            try:
                timestamp = timestamp.tz_localize(target_tz)
            except Exception:
                pass
        candidates.append(timestamp)
    if not candidates:
        return None
    return max(candidates, key=lambda value: value.value)


def _create_futu_quote_event_handler(handler_base, callback):
    """创建 SDK 订阅处理器，并把解析后的 DataFrame 交给回调。"""
    if handler_base is None:
        if _futu is None:
            # 测试替身或调用方注入的上下文可能不依赖 futu-api；此时保留原始
            # 回调内容，避免最小订阅协议测试因可选依赖缺失而无法启动。
            class _PassthroughHandlerBase:
                """无 SDK 时仅透传已由上下文解析的行情内容。"""

                def on_recv_rsp(self, rsp_pb):
                    return RET_OK, rsp_pb

            handler_base = _PassthroughHandlerBase
        else:
            raise RuntimeError(
                'Futu quote subscription handler is unavailable in this SDK; '
                'upgrade futu-api or use schedule mode.'
            )

    class _Handler(handler_base):
        """将 Futu 行情订阅回调转交给框架 worker。"""

        def __init__(self):
            super().__init__()

        def on_recv_rsp(self, rsp_pb):
            ret_code, content = super().on_recv_rsp(rsp_pb)
            if ret_code == RET_OK:
                try:
                    callback(content)
                except Exception as exc:
                    runtime_print(f'[FutuBroker] quote subscription callback failed: {exc}')
            return ret_code, content

    return _Handler()


def _order_dict(value):
    """将 place_order 返回的单行对象转换为可补字段的字典。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, pd.Series):
        return value.to_dict()
    to_dict = getattr(value, 'to_dict', None)
    if callable(to_dict):
        try:
            converted = to_dict()
            if isinstance(converted, dict):
                return dict(converted)
        except Exception:
            pass
    names = (
        'order_id', 'orderId', 'id', 'code', 'symbol', 'trd_side', 'side', 'order_status',
        'status', 'qty', 'price', 'dealt_qty', 'dealt_avg_price', 'updated_time',
        'create_time', 'last_err_msg',
    )
    return {
        name: value
        for name in names
        if _field(value, name, None) is not None
    }


def _context_attr(context, name, default=None):
    """读取 context 中显式注入的句柄，避免 MagicMock 动态属性被误当成 SDK。"""
    if context is None:
        return default
    values = getattr(context, '__dict__', None)
    if isinstance(values, dict) and name in values:
        return values[name]
    class_values = getattr(type(context), '__dict__', {})
    if name in class_values:
        return getattr(context, name, default)
    return default


def _normalise_symbol(symbol, market_hint=None):
    """将框架、IBKR 和富途代码统一为 ``MARKET.CODE``。"""
    return normalize_futu_symbol(symbol, market_hint=market_hint)


def _raw_order_symbol(order):
    return _field(order, ('code', 'symbol', 'stock_code', 'local_symbol'), '')


def _symbol_from_order(order):
    market_hint = _field(order, ('order_market', 'position_market', 'market'), '')
    return _normalise_symbol(_raw_order_symbol(order), market_hint=market_hint)


class FutuOrderProxy(BaseOrderProxy):
    """富途订单状态代理，向基础执行器暴露统一的订单语义。"""

    _PENDING_STATUSES = {
        'WAITING_SUBMIT', 'SUBMITTING', 'SUBMITTED', 'FILLED_PART',
        'CANCELLING_PART', 'CANCELLING_ALL',
    }
    _COMPLETED_STATUSES = {'FILLED_ALL', 'FILLED'}
    _CANCELED_STATUSES = {
        'CANCELLED', 'CANCELLED_PART', 'CANCELLED_ALL',
        'CANCELED', 'CANCELED_PART', 'CANCELED_ALL', 'FILL_CANCELLED',
    }
    _REJECTED_STATUSES = {
        'SUBMIT_FAILED', 'FAILED', 'REJECTED', 'DISABLED', 'DELETED', 'TIMEOUT', 'UNSUBMITTED',
    }

    def __init__(self, raw_order, is_live=True, data=None, contract_multiplier=None):
        self.raw_order = raw_order
        self.platform_order = raw_order
        self.is_live = bool(is_live)
        self.data = data
        requested = _field(raw_order, ('qty', 'volume', 'total_quantity', 'totalQuantity'))
        self.submitted_size = requested
        self.requested_size = requested
        self.reserved_cash = 0.0
        multiplier = contract_multiplier
        multiplier_value = _decimal(multiplier, None)
        if multiplier_value is None or multiplier_value <= 0:
            multiplier_value = None
            for name in (
                'option_contract_multiplier',
                'option_contract_size',
                'contract_multiplier',
                'contract_size',
            ):
                candidate = _decimal(_field(raw_order, name, None), None)
                if candidate is not None and candidate > 0:
                    multiplier_value = candidate
                    break
        self.contract_multiplier = float(multiplier_value) if multiplier_value is not None else 1.0

    @property
    def id(self):
        value = _text(_field(self.raw_order, ('order_id', 'orderId', 'id', 'orderid'), ''))
        return '' if value.upper() in {'N/A', 'NA', 'NONE', 'NAN', 'NULL'} else value

    @property
    def status(self):
        return _enum_name(_field(self.raw_order, ('order_status', 'status'), ''))

    @property
    def executed(self):
        raw = self.raw_order
        size = _decimal(_field(raw, ('dealt_qty', 'filled_qty', 'filled_volume'), 0), Decimal('0'))
        price = _decimal(_field(raw, ('dealt_avg_price', 'avg_fill_price', 'filled_vwap'), 0), Decimal('0'))
        value = _decimal(
            _field(raw, ('dealt_amount', 'dealt_val', 'filled_amount', 'filled_value'), None),
            None,
        )
        if value is None:
            value = size * price * Decimal(str(self.contract_multiplier))
        commission = _decimal(_field(raw, ('commission', 'dealt_commission', 'fee'), 0), Decimal('0'))
        execution_dt = None
        for name in ('updated_time', 'update_time', 'dealt_time', 'create_time'):
            execution_dt = coerce_dt(_field(raw, name))
            if execution_dt is not None:
                break
        return SimpleNamespace(
            size=quantity_number(size),
            price=quantity_number(price),
            value=quantity_number(value),
            comm=quantity_number(commission),
            dt=execution_dt,
        )

    def is_completed(self) -> bool:
        return self.status in self._COMPLETED_STATUSES

    def is_canceled(self) -> bool:
        return self.status in self._CANCELED_STATUSES

    def is_rejected(self) -> bool:
        return self.status in self._REJECTED_STATUSES

    def is_pending(self) -> bool:
        return self.status in self._PENDING_STATUSES

    def is_accepted(self) -> bool:
        return self.is_pending()

    def is_buy(self) -> bool:
        return _enum_name(_field(self.raw_order, ('trd_side', 'side', 'action'), '')) == 'BUY'

    def is_sell(self) -> bool:
        return _enum_name(_field(self.raw_order, ('trd_side', 'side', 'action'), '')) == 'SELL'


class FutuBrokerAdapter(BaseLiveBroker):
    """富途 OpenD 证券交易适配器，支持股票、ETF 和基础期权买卖。"""

    _PENDING_STATUSES = FutuOrderProxy._PENDING_STATUSES
    _TERMINAL_STATUSES = (
        FutuOrderProxy._COMPLETED_STATUSES
        | FutuOrderProxy._CANCELED_STATUSES
        | FutuOrderProxy._REJECTED_STATUSES
    )

    def __init__(
        self,
        context,
        cash_override=None,
        commission_override=None,
        slippage_override=None,
        trade_ctx=None,
        quote_ctx=None,
    ):
        self.is_live = True
        self._context_lock = threading.RLock()
        self._context_init_thread = None
        self._trade_ctx = (
            trade_ctx
            if trade_ctx is not None
            else _context_attr(context, 'futu_trade_context')
        )
        if self._trade_ctx is None:
            self._trade_ctx = _context_attr(context, 'futu_trade_ctx')
        if self._trade_ctx is None:
            self._trade_ctx = _context_attr(context, 'trade_ctx')
        if self._trade_ctx is None:
            self._trade_ctx = _context_attr(context, 'trd_ctx')
        self._owns_trade_ctx = False
        self._quote_ctx = quote_ctx
        if self._quote_ctx is None:
            self._quote_ctx = _context_attr(context, 'futu_quote_context')
        if self._quote_ctx is None:
            self._quote_ctx = _context_attr(context, 'futu_quote_ctx')
        if self._quote_ctx is None:
            self._quote_ctx = _context_attr(context, 'quote_ctx')
        if self._quote_ctx is None:
            self._quote_ctx = _context_attr(context, 'quote_context')
        self._quote_context_init_thread = None
        self._quote_context_init_failed = False
        self._owns_quote_ctx = False
        self._trade_context_init_failed = False
        self._trade_context_retry_at = 0.0
        self.trade_ctx = self._trade_ctx
        self.trd_ctx = self._trade_ctx
        self.quote_ctx = self._quote_ctx

        self._context = context
        self._host = self._futu_setting('FUTU_HOST', '127.0.0.1')
        self._port = self._safe_int(
            self._futu_setting('FUTU_PORT', 11111),
            11111,
        )
        self._rsa_key_path = _text(
            self._futu_setting('FUTU_RSA_KEY_PATH', '')
        )
        self._filter_trdmarket = self._futu_setting(
            'FUTU_FILTER_TRDMARKET', 'N/A'
        )
        self._trade_env = self._futu_setting(
            'FUTU_TRADE_ENV', 'SIMULATE'
        )
        self._account_id = self._safe_int(
            self._futu_setting('FUTU_ACCOUNT_ID', 0),
            0,
        )
        self._account_index = self._safe_int(
            self._futu_setting('FUTU_ACCOUNT_INDEX', 0),
            0,
        )
        self._security_firm = self._futu_setting(
            'FUTU_SECURITY_FIRM', 'N/A'
        )
        self._account_currency = self._futu_setting(
            'FUTU_ACCOUNT_CURRENCY', 'HKD'
        )
        self._last_account_snapshot_fetch_failed = False
        self._last_account_snapshot_fetch_error = None
        self._last_position_snapshot_fetch_failed = False
        self._last_position_snapshot_fetch_error = None
        # 期权报价通常按标的每股计价，而 Futu 下单数量按合约张数计。
        # 首次快照时读取 option_contract_multiplier，后续目标仓位使用同一口径。
        self._contract_multipliers = {}
        self._fx_rate_cache = {}
        self._quote_context_retry_at = 0.0

        if self._trade_ctx is None:
            _require_futu_trade_sdk()

        self._set_query_timeout(self._trade_ctx)

        super().__init__(context, cash_override, commission_override, slippage_override)

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _is_option_symbol(symbol):
        """识别常见 Futu 期权代码，用于行情不可用时失败关闭。"""
        return bool(_OPTION_CODE_RE.match(_text(symbol).upper()))

    def _futu_setting(self, name, default=None):
        """读取 launch 注入的连接配置，再回退到公开配置模块。"""
        context_config = getattr(self, '_context', None)
        context_config = getattr(context_config, '_futu_runtime_config', None)
        if isinstance(context_config, dict) and name in context_config:
            return context_config[name]
        key = str(name or '')
        return getattr(config, key, default)

    def _current_setting(self, name, default=None):
        runtime_config = getattr(self, '_runtime_config', None)
        if isinstance(runtime_config, dict) and name in runtime_config:
            return runtime_config[name]
        context_config = getattr(self._context, '_futu_runtime_config', None)
        if isinstance(context_config, dict) and name in context_config:
            return context_config[name]
        try:
            if str(name or '').startswith('FUTU_'):
                return self._futu_setting(name, default)
            return self._runtime_setting(name, default)
        except Exception:
            return default

    @staticmethod
    def _positive_decimal(value):
        """读取正的有限十进制值；无效值返回 None。"""
        parsed = _decimal(value, None)
        return parsed if parsed is not None and parsed > 0 else None

    @classmethod
    def _market_currency(cls, symbol):
        """按 Futu 市场前缀推断证券报价币种。"""
        market = _normalise_symbol(symbol).split('.', 1)[0]
        return _MARKET_CURRENCIES.get(market)

    def _account_currency_name(self):
        """返回账户估值/可用资金所使用的币种名称。"""
        return _enum_name(self._account_currency_value())

    def _cash_field_names(self):
        """返回账户计价币种对应的实时购买力字段，并保留旧版字段回退。"""
        currency = self._account_currency_name()
        specific = _CURRENCY_CASH_FIELDS.get(currency, ())
        # power 是含保证金假设的最大购买力，不等同于可用现金，不能作为现金回退。
        return tuple(specific) + ('available_funds', 'net_cash_power', 'cash')

    def _get_fx_rate(self, source_currency, target_currency):
        """读取报价币种到账户币种的实时汇率；不可用时返回 None。"""
        source = _text(source_currency).upper()
        target = _text(target_currency).upper()
        if not source or not target:
            return None
        if source == target or {source, target} == {'CNY', 'CNH'}:
            return 1.0

        cache_key = (source, target)
        cached = self._fx_rate_cache.get(cache_key)
        now = time.monotonic()
        if cached is not None:
            cached_at, cached_rate = cached
            if now - cached_at <= _FX_RATE_CACHE_SECONDS:
                return cached_rate

        # 始终通过统一入口读取上下文，避免已注入或已缓存的 CLOSED 句柄绕过重建逻辑。
        quote_context = self._get_quote_context()
        if quote_context is None:
            return None

        def read_rate(code):
            response = quote_context.get_market_snapshot([code])
            if not isinstance(response, tuple) or len(response) < 2 or response[0] != RET_OK:
                return None
            for row in _rows(response[1]):
                row_code = _normalise_symbol(_field(row, ('code', 'symbol'), ''))
                if row_code and row_code != _normalise_symbol(code):
                    continue
                value = self._positive_decimal(
                    _field(row, ('last_price', 'last', 'price', 'close'), None)
                )
                if value is not None:
                    return float(value)
            return None

        def read_pair(base, quote):
            """读取 base 到 quote 的直接或反向 FX 报价。"""
            direct = read_rate(f'FX.{base}{quote}')
            if direct is not None:
                return direct
            inverse = read_rate(f'FX.{quote}{base}')
            if inverse is not None and inverse > 0:
                return 1.0 / inverse
            return None

        try:
            with self._context_lock:
                rate = read_pair(source, target)
                if rate is None and source != 'USD' and target != 'USD':
                    # Futu 并不保证每一对非美元货币都有直接行情；用美元三角换汇，
                    # 仍然要求两条实时报价都可验证，任何一条缺失都失败关闭。
                    source_to_usd = read_pair(source, 'USD')
                    usd_to_target = read_pair('USD', target)
                    if source_to_usd is not None and usd_to_target is not None:
                        rate = source_to_usd * usd_to_target
                if rate is not None and rate > 0 and math.isfinite(rate):
                    self._fx_rate_cache[cache_key] = (now, rate)
                    return rate
        except Exception as exc:
            self._runtime_log(
                f'[FutuBroker] FX rate unavailable for {source}->{target}: {exc}'
            )
        return None

    def _order_unit_value(self, data, price) -> float:
        """按账户估值币种计算一单位下单数量的现金名义价值。"""
        symbol = _normalise_symbol(getattr(data, '_name', '')) if data is not None else ''
        source_currency = self._market_currency(symbol)
        target_currency = self._account_currency_name()
        rate = self._get_fx_rate(source_currency, target_currency)
        if rate is None:
            self._runtime_log(
                f'[FutuBroker] Cannot convert {source_currency or "UNKNOWN"} to '
                f'{target_currency or "UNKNOWN"}; order/valuation skipped for {symbol or "UNKNOWN"}.'
            )
            return 0.0
        try:
            value = float(price) * self._contract_multiplier(data) * rate
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return value if value > 0 and math.isfinite(value) else 0.0

    def get_position_market_value(self, data, size, price=None) -> float:
        """返回按账户币种换算后的持仓市值。"""
        value = super().get_position_market_value(data, size, price=price)
        try:
            positive_size = float(size) > 0
        except (TypeError, ValueError, OverflowError):
            positive_size = False
        if positive_size and (not math.isfinite(value) or value <= 0):
            raise RuntimeError(
                f'Futu position valuation unavailable for {getattr(data, "_name", "UNKNOWN")}'
            )
        return value

    def _get_portfolio_nav(self):
        """使用 Futu 已按账户币种换算的总资产快照。"""
        # 账户快照是跨币种估值的唯一可信来源；查询失败时不能退回只含局部报价
        # 或无法换汇的本地估值，否则目标仓位可能被错误放大或缩小。
        value = float(self.getvalue())
        if math.isfinite(value) and value >= 0:
            return value
        error = RuntimeError(f'Futu account NAV is invalid: {value!r}')
        self._last_account_snapshot_fetch_failed = True
        self._last_account_snapshot_fetch_error = error
        raise error

    @staticmethod
    def _set_query_timeout(context):
        """为已注入的 SDK 上下文设置有限同步查询超时。"""
        setter = getattr(context, 'set_sync_query_connect_timeout', None)
        if callable(setter):
            try:
                setter(_QUERY_TIMEOUT_SECONDS)
            except Exception:
                pass

    def _configure_protocol(self):
        """根据 RSA 路径配置全局协议；空路径明确关闭加密。"""
        if SysConfig is None:
            return
        rsa_path = os.path.expandvars(os.path.expanduser(self._rsa_key_path))
        if rsa_path:
            if not os.path.isfile(rsa_path):
                raise FileNotFoundError(f'Futu RSA key file not found: {rsa_path}')
            SysConfig.enable_proto_encrypt(True)
            SysConfig.set_init_rsa_file(file=rsa_path)
        else:
            SysConfig.enable_proto_encrypt(False)

    def _get_quote_context(self):
        """按需创建行情上下文，使独立启动的 Futu adapter 也能读取期权乘数。"""
        with self._context_lock:
            if self._quote_ctx is not None:
                try:
                    status = _enum_name(getattr(self._quote_ctx, 'status', ''))
                except Exception:
                    # 某些轻量替身或连接关闭中的 SDK 句柄读取 status 可能抛错；
                    # 保留句柄交给实际查询处理，避免初始化线程被异常打断。
                    status = ''
                if status not in {'CLOSED', 'CLOSING'}:
                    return self._quote_ctx
                if self._owns_quote_ctx:
                    try:
                        self._quote_ctx.close()
                    except Exception:
                        pass
                self._quote_ctx = None
                self.quote_ctx = None
                self._owns_quote_ctx = False
            if OpenQuoteContext is None:
                return None
            if self._quote_context_init_failed and time.monotonic() < self._quote_context_retry_at:
                return None
            if self._quote_context_init_thread is not None and self._quote_context_init_thread.is_alive():
                return None

            try:
                self._configure_protocol()
                if SysConfig is not None:
                    set_daemon = getattr(SysConfig, 'set_all_thread_daemon', None)
                    if callable(set_daemon):
                        set_daemon(True)
            except Exception as exc:
                self._quote_context_init_failed = True
                self._runtime_log(f'[FutuBroker] OpenD quote configuration failed: {exc}')
                return None

            result = {}
            error = {}
            state = {'timed_out': False}
            security_firm = _enum_value(SecurityFirm, self._security_firm, 'N/A')
            kwargs = {
                'host': self._host,
                'port': self._port,
                'is_encrypt': bool(self._rsa_key_path),
                'security_firm': security_firm,
                # 异步构造不会在 OpenD 不可用时进入 SDK 内部的无限重连循环。
                'is_async_connect': True,
            }

            def create_context():
                try:
                    try:
                        created = OpenQuoteContext(**kwargs)
                    except TypeError:
                        kwargs_without_firm = dict(kwargs)
                        kwargs_without_firm.pop('security_firm', None)
                        try:
                            created = OpenQuoteContext(**kwargs_without_firm)
                        except TypeError:
                            kwargs_without_firm.pop('is_async_connect', None)
                            created = OpenQuoteContext(**kwargs_without_firm)
                    if state['timed_out']:
                        close_context = getattr(created, 'close', None)
                        if callable(close_context):
                            close_context()
                    else:
                        result['context'] = created
                except Exception as exc:
                    error['exception'] = exc

            creator = threading.Thread(
                target=create_context,
                name='quantada-futu-quote-context',
                daemon=True,
            )
            self._quote_context_init_thread = creator
            creator.start()
            creator.join(_CONTEXT_INIT_TIMEOUT_SECONDS)
            if creator.is_alive():
                state['timed_out'] = True
                error['exception'] = TimeoutError('Futu quote context initialization timed out')
            if 'exception' in error:
                self._quote_context_init_failed = True
                self._quote_context_retry_at = (
                    time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                )
                self._runtime_log(f'[FutuBroker] OpenD quote connection failed: {error["exception"]}')
                return None

            self._quote_ctx = result.get('context')
            if self._quote_ctx is None:
                self._quote_context_init_failed = True
                self._quote_context_retry_at = (
                    time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                )
                return None
            self._owns_quote_ctx = True
            self.quote_ctx = self._quote_ctx
            # 异步构造只代表连接线程已启动；必须等 OpenD 返回 READY 后才能查询或订阅。
            # 否则首个 subscribe 可能收到 invalid connid，并触发无意义的 worker 重启。
            status_observed = False
            ready = False
            ready_deadline = time.monotonic() + _CONTEXT_INIT_TIMEOUT_SECONDS
            while time.monotonic() < ready_deadline:
                try:
                    raw_status = getattr(self._quote_ctx, 'status', None)
                except Exception:
                    raw_status = None
                if raw_status is None:
                    ready = True
                    break
                status_observed = True
                status = _enum_name(raw_status)
                if status == 'READY':
                    ready = True
                    break
                if status in {'CLOSED', 'CLOSING'}:
                    break
                time.sleep(0.05)
            if status_observed and not ready:
                try:
                    self._quote_ctx.close()
                except Exception:
                    pass
                self._quote_ctx = None
                self.quote_ctx = None
                self._owns_quote_ctx = False
                self._quote_context_init_failed = True
                self._quote_context_retry_at = (
                    time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                )
                self._runtime_log('[FutuBroker] OpenD quote context did not become ready before timeout.')
                return None
            self._set_query_timeout(self._quote_ctx)
            self._quote_context_init_failed = False
            self._quote_context_retry_at = 0.0
            return self._quote_ctx

    def _get_trade_context(self):
        """按需创建交易上下文，并限制 SDK 构造阶段的阻塞时间。"""
        with self._context_lock:
            if self._trade_ctx is not None:
                try:
                    status = _enum_name(getattr(self._trade_ctx, 'status', ''))
                except Exception:
                    status = ''
                if status not in {'CLOSED', 'CLOSING'}:
                    return self._trade_ctx

                stale_context = self._trade_ctx
                owns_stale_context = self._owns_trade_ctx
                self._trade_ctx = None
                self.trade_ctx = None
                self.trd_ctx = None
                self._owns_trade_ctx = False
                if owns_stale_context:
                    try:
                        stale_context.close()
                    except Exception as exc:
                        self._runtime_log(
                            f'[FutuBroker] Failed to close stale trade context: {exc}'
                        )
                ledger_lock = getattr(self, '_ledger_lock', None)
                if ledger_lock is None:
                    active_buys = getattr(self, '_active_buys', None)
                    pending_sells = getattr(self, '_pending_sells', None)
                    if active_buys is not None:
                        active_buys.clear()
                    if pending_sells is not None:
                        pending_sells.clear()
                    if hasattr(self, '_virtual_spent_cash'):
                        self._virtual_spent_cash = 0.0
                else:
                    with ledger_lock:
                        self._active_buys.clear()
                        self._pending_sells.clear()
                        self._virtual_spent_cash = 0.0
            if OpenSecTradeContext is None:
                return None
            if (
                self._trade_context_init_failed
                and time.monotonic() < self._trade_context_retry_at
            ):
                return None
            if self._context_init_thread is not None and self._context_init_thread.is_alive():
                return None

            try:
                self._configure_protocol()
                if SysConfig is not None:
                    set_daemon = getattr(SysConfig, 'set_all_thread_daemon', None)
                    if callable(set_daemon):
                        set_daemon(True)
            except Exception as exc:
                self._last_account_snapshot_fetch_failed = True
                self._last_account_snapshot_fetch_error = exc
                self._trade_context_init_failed = True
                self._trade_context_retry_at = (
                    time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                )
                return None

            result = {}
            error = {}
            state = {'timed_out': False}
            filter_market = _enum_value(TrdMarket, self._filter_trdmarket, 'N/A')
            security_firm = _enum_value(SecurityFirm, self._security_firm, 'N/A')
            kwargs = {
                'filter_trdmarket': filter_market,
                'host': self._host,
                'port': self._port,
                'is_encrypt': bool(self._rsa_key_path),
                'security_firm': security_firm,
            }

            def create_context():
                try:
                    try:
                        created = OpenSecTradeContext(**kwargs)
                    except TypeError:
                        # 兼容较旧 SDK 未暴露 security_firm 参数的版本。
                        kwargs_without_firm = dict(kwargs)
                        kwargs_without_firm.pop('security_firm', None)
                        try:
                            created = OpenSecTradeContext(**kwargs_without_firm)
                        except TypeError:
                            created = OpenSecTradeContext(
                                filter_market,
                                self._host,
                                self._port,
                                kwargs['is_encrypt'],
                            )
                    if state['timed_out']:
                        close_context = getattr(created, 'close', None)
                        if callable(close_context):
                            close_context()
                    else:
                        result['context'] = created
                except Exception as exc:
                    error['exception'] = exc

            creator = threading.Thread(
                target=create_context,
                name='quantada-futu-trade-context',
                daemon=True,
            )
            self._context_init_thread = creator
            creator.start()
            creator.join(_CONTEXT_INIT_TIMEOUT_SECONDS)
            if creator.is_alive():
                state['timed_out'] = True
                error['exception'] = TimeoutError('Futu trade context initialization timed out')
            if 'exception' in error:
                self._trade_context_init_failed = True
                self._trade_context_retry_at = (
                    time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                )
                self._runtime_log(f'[FutuBroker] OpenD trade connection failed: {error["exception"]}')
                return None

            self._trade_ctx = result.get('context')
            if self._trade_ctx is None:
                self._trade_context_init_failed = True
                self._trade_context_retry_at = (
                    time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                )
                return None
            self._owns_trade_ctx = True
            self.trade_ctx = self._trade_ctx
            self.trd_ctx = self._trade_ctx
            self._set_query_timeout(self._trade_ctx)
            self._trade_context_init_failed = False
            self._trade_context_retry_at = 0.0
            return self._trade_ctx

    def close(self):
        """关闭本适配器创建的交易和行情上下文。"""
        with self._context_lock:
            trade_context = self._trade_ctx if self._owns_trade_ctx else None
            quote_context = self._quote_ctx if self._owns_quote_ctx else None
            if trade_context is not None:
                self._trade_ctx = None
                self.trade_ctx = None
                self.trd_ctx = None
                self._owns_trade_ctx = False
            if quote_context is not None:
                self._quote_ctx = None
                self.quote_ctx = None
                self._owns_quote_ctx = False
            self._quote_context_init_failed = False
            self._quote_context_retry_at = 0.0
            self._trade_context_init_failed = False
            self._trade_context_retry_at = 0.0

        for name, context in (('trade', trade_context), ('quote', quote_context)):
            if context is None:
                continue
            try:
                context.close()
            except Exception as exc:
                self._runtime_log(f'[FutuBroker] Failed to close {name} context: {exc}')

    def _trade_env_value(self):
        return _enum_value(
            TrdEnv,
            self._current_setting('FUTU_TRADE_ENV', self._trade_env),
            'SIMULATE',
        )

    def _account_id_value(self):
        return self._safe_int(self._current_setting('FUTU_ACCOUNT_ID', self._account_id), self._account_id)

    def _account_index_value(self):
        return self._safe_int(
            self._current_setting('FUTU_ACCOUNT_INDEX', self._account_index),
            self._account_index,
        )

    def _account_currency_value(self):
        try:
            from futu import Currency
        except Exception:
            Currency = None
        currency_name = _text(
            self._current_setting('FUTU_ACCOUNT_CURRENCY', self._account_currency),
            'HKD',
        ).upper()
        # Futu 账户 API 使用离岸人民币 CNH 枚举，不接受常见配置别名 CNY。
        if currency_name == 'CNY':
            currency_name = 'CNH'
        return _enum_value(
            Currency,
            currency_name,
            'HKD',
        )

    def get_contract_multiplier(self, data):
        """读取某合约的现金乘数；已识别期权缺少元数据时返回 0 以阻止误下单。"""
        if data is None:
            return 1.0
        symbol = _normalise_symbol(getattr(data, '_name', ''))
        is_option = self._is_option_symbol(symbol)
        cached = self._contract_multipliers.get(symbol)
        if cached is not None:
            return cached

        sources = (data, getattr(getattr(data, 'p', None), 'dataname', None))
        for names in (
            ('option_contract_multiplier', 'option_contract_size'),
            ('contract_multiplier', 'contract_size'),
        ):
            generic_multiplier_names = names == ('contract_multiplier', 'contract_size')
            for source in sources:
                if isinstance(source, pd.DataFrame):
                    attrs = getattr(source, 'attrs', {}) or {}
                    for name in names:
                        parsed = self._positive_decimal(attrs.get(name))
                        if parsed is not None:
                            if is_option and generic_multiplier_names and parsed <= 1:
                                continue
                            value = float(parsed)
                            if symbol:
                                self._contract_multipliers[symbol] = value
                            return value
                for name in names:
                    if isinstance(source, pd.DataFrame) and name in source.columns:
                        values = pd.to_numeric(source[name], errors='coerce').dropna()
                        for raw_value in reversed(values.tolist()):
                            parsed = self._positive_decimal(raw_value)
                            if parsed is not None:
                                if is_option and generic_multiplier_names and parsed <= 1:
                                    continue
                                value = float(parsed)
                                if symbol:
                                    self._contract_multipliers[symbol] = value
                                return value
                        continue
                    parsed = self._positive_decimal(_field(source, name, None))
                    if parsed is not None:
                        if is_option and names == ('contract_multiplier', 'contract_size') and parsed <= 1:
                            continue
                        value = float(parsed)
                        if symbol:
                            self._contract_multipliers[symbol] = value
                        return value
        return 0.0 if is_option else 1.0

    def _contract_multiplier(self, data):
        """安全读取乘数；缺少期权乘数时保持失败关闭，不回退为股票每股。"""
        symbol = _normalise_symbol(getattr(data, '_name', '')) if data is not None else ''
        if self._is_option_symbol(symbol):
            value = self.get_contract_multiplier(data)
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                return 0.0
            return parsed if math.isfinite(parsed) and parsed > 0 else 0.0
        return super()._contract_multiplier(data)

    def get_order_lot_size(self, data) -> float:
        """返回 Futu 真实数量步长；A 股普通证券买入默认按 100 股整手。"""
        configured = self._current_setting('LOT_SIZE', config.LOT_SIZE)
        parsed_configured = self._positive_decimal(configured) or Decimal('1')
        symbol = _normalise_symbol(getattr(data, '_name', '')) if data is not None else ''
        market = symbol.split('.', 1)[0]
        if market in {'SH', 'SZ'} and not self._is_option_symbol(symbol):
            # 可由行情/自定义 DataFeed 提供更准确的 lot_size；不能小于 A 股最低整手。
            for source in (data, getattr(getattr(data, 'p', None), 'dataname', None)):
                if isinstance(source, pd.DataFrame):
                    for name in ('lot_size', 'board_lot'):
                        if name in source.columns:
                            values = pd.to_numeric(source[name], errors='coerce').dropna()
                            for raw_value in reversed(values.tolist()):
                                candidate = self._positive_decimal(raw_value)
                                if candidate is not None:
                                    return float(max(candidate, Decimal('100')))
                            continue
                candidate = self._positive_decimal(_field(source, ('lot_size', 'board_lot'), None))
                if candidate is not None:
                    return float(max(candidate, Decimal('100')))
            return float(max(parsed_configured, Decimal('100')))
        return float(parsed_configured)

    def _cache_contract_multiplier(self, symbol, row):
        """从行情快照缓存期权乘数，不把股票的空字段误当成有效值。"""
        is_option = self._is_option_symbol(symbol)
        multiplier = None
        for name in (
            'option_contract_multiplier',
            'option_contract_size',
            'contract_multiplier',
            'contract_size',
        ):
            multiplier = self._positive_decimal(_field(row, name, None))
            if (
                is_option
                and name in {'contract_multiplier', 'contract_size'}
                and multiplier is not None
                and multiplier <= 1
            ):
                multiplier = None
            if multiplier is not None:
                break
        if multiplier is None or not symbol:
            return
        self._contract_multipliers[symbol] = float(multiplier)

    def _query_account_info(self):
        context = self._get_trade_context()
        if context is None:
            error = RuntimeError('Futu trade context is unavailable')
            self._last_account_snapshot_fetch_failed = True
            self._last_account_snapshot_fetch_error = error
            raise error
        try:
            with self._context_lock:
                response = context.accinfo_query(
                    trd_env=self._trade_env_value(),
                    acc_id=self._account_id_value(),
                    acc_index=self._account_index_value(),
                    refresh_cache=True,
                    currency=self._account_currency_value(),
                )
            if not isinstance(response, tuple) or len(response) < 2:
                raise RuntimeError('Futu accinfo_query returned an invalid response')
            ret_code, data = response[0], response[1]
            if ret_code != RET_OK:
                raise RuntimeError(f'Futu accinfo_query failed: {data}')
            rows = _rows(data)
            if not rows:
                raise RuntimeError('Futu account snapshot is empty')
            self._last_account_snapshot_fetch_failed = False
            self._last_account_snapshot_fetch_error = None
            return rows
        except Exception as exc:
            self._last_account_snapshot_fetch_failed = True
            self._last_account_snapshot_fetch_error = exc
            raise

    def is_account_snapshot_trusted(self) -> bool:
        """供 LiveTrader 判断账户摘要是否已同步。"""
        try:
            rows = self._query_account_info()
            if self._first_numeric(rows, self._cash_field_names() + ('total_assets',)) is None:
                error = RuntimeError('Futu account snapshot has no numeric balance fields')
                self._last_account_snapshot_fetch_failed = True
                self._last_account_snapshot_fetch_error = error
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def _first_numeric(rows, names, positive=False):
        # 富途账户表常同时返回 ``N/A`` 和有效字段，不能因第一个字段为空
        # 就跳过同一行后面的现金字段。
        if isinstance(names, str):
            names = (names,)
        zero_value = None
        for name in names:
            for row in rows:
                value = _decimal(_field(row, name), None)
                if value is not None:
                    if positive and value <= 0:
                        if zero_value is None:
                            zero_value = value
                        continue
                    return value
        return zero_value

    def _fetch_real_cash(self) -> float:
        """读取富途账户当前可用购买力，不维护本地资金缓存。"""
        rows = self._query_account_info()
        value = self._first_numeric(rows, self._cash_field_names(), positive=True)
        if value is None:
            error = RuntimeError('Futu account snapshot has no usable cash field')
            self._last_account_snapshot_fetch_failed = True
            self._last_account_snapshot_fetch_error = error
            raise error
        return float(value)

    def getcash(self):
        """提供 Backtrader 风格的现金别名。"""
        return self.get_cash()

    def getvalue(self):
        """读取账户净资产；缺少 total_assets 时用现金与市值兜底。"""
        rows = self._query_account_info()
        total_assets = self._first_numeric(rows, ('total_assets', 'net_assets', 'nav'))
        if total_assets is not None:
            return float(total_assets)
        cash = self._first_numeric(rows, self._cash_field_names(), positive=True)
        market_value = self._first_numeric(rows, ('market_val', 'market_value', 'securities_assets'))
        if cash is None or market_value is None:
            error = RuntimeError('Futu account snapshot has no total_assets or cash/market value')
            self._last_account_snapshot_fetch_failed = True
            self._last_account_snapshot_fetch_error = error
            raise error
        return float(cash + market_value)

    def _query_position_rows(self, code):
        context = self._get_trade_context()
        if context is None:
            error = RuntimeError('Futu trade context is unavailable')
            self._last_position_snapshot_fetch_failed = True
            self._last_position_snapshot_fetch_error = error
            raise error
        try:
            with self._context_lock:
                query_kwargs = {
                    'code': code,
                    'trd_env': self._trade_env_value(),
                    'acc_id': self._account_id_value(),
                    'acc_index': self._account_index_value(),
                    'refresh_cache': True,
                    'currency': self._account_currency_value(),
                }
                try:
                    response = context.position_list_query(**query_kwargs)
                except TypeError:
                    # 兼容未暴露 currency 参数的旧版 SDK；正式 SDK 会走上面的账户币种口径。
                    query_kwargs.pop('currency', None)
                    response = context.position_list_query(**query_kwargs)
            if not isinstance(response, tuple) or len(response) < 2:
                raise RuntimeError('Futu position_list_query returned an invalid response')
            ret_code, data = response[0], response[1]
            if ret_code != RET_OK:
                raise RuntimeError(f'Futu position_list_query failed: {data}')
            rows = _rows(data)
            self._last_position_snapshot_fetch_failed = False
            self._last_position_snapshot_fetch_error = None
            return rows
        except Exception as exc:
            self._last_position_snapshot_fetch_failed = True
            self._last_position_snapshot_fetch_error = exc
            raise

    @staticmethod
    def _position_record(row):
        raw_code = _field(row, ('code', 'symbol', 'stock_code'), '')
        market_hint = _field(row, ('position_market', 'market'), '')
        raw_qty = None
        qty = None
        for name in ('qty', 'volume', 'position'):
            candidate = _field(row, name, None)
            parsed = _decimal(candidate, None)
            if parsed is not None:
                raw_qty = candidate
                qty = parsed
                break
        if qty is None:
            raise RuntimeError(f'Futu position has invalid quantity: {raw_qty!r}')
        raw_sellable = None
        sellable = None
        invalid_sellable = None
        for name in ('can_sell_qty', 'sellable', 'available'):
            candidate = _field(row, name, None)
            if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
                continue
            parsed = _decimal(candidate, None)
            if parsed is not None:
                raw_sellable = candidate
                sellable = parsed
                break
            if invalid_sellable is None:
                invalid_sellable = candidate
        if (
            sellable is None
            and invalid_sellable is not None
            and _text(invalid_sellable).upper() not in {'', 'N/A', 'NONE', 'NAN', 'NA'}
        ):
            raw_sellable = invalid_sellable
            raise RuntimeError(f'Futu position has invalid sellable quantity: {raw_sellable!r}')
        raw_price = None
        price = None
        for name in ('average_cost', 'cost_price', 'price'):
            candidate = _field(row, name, None)
            parsed = _decimal(candidate, None)
            if parsed is not None:
                raw_price = candidate
                price = parsed
                break
        if price is None:
            raw_price = raw_price if raw_price is not None else _field(row, ('average_cost', 'cost_price', 'price'), None)
            raise RuntimeError(f'Futu position has invalid cost: {raw_price!r}')
        symbol = _normalise_symbol(raw_code, market_hint)
        if not symbol:
            raise RuntimeError('Futu position is missing code')
        position_side = _enum_name(_field(row, 'position_side', ''))
        if position_side not in {'', 'N/A', 'NONE', 'LONG'}:
            raise RuntimeError(
                f'Futu short position is unsupported for {symbol}: side={position_side}'
            )
        position_type = _enum_name(_field(row, 'position_type', ''))
        raw_combo_id = _field(row, 'combo_id', '')
        combo_id = _text(raw_combo_id)
        combo_number = _decimal(raw_combo_id, None)
        combo_is_empty = (
            combo_id.upper() in {'', '0', 'N/A', 'NONE'}
            or (combo_number is not None and combo_number == 0)
        )
        if position_type == 'COMBINED' or not combo_is_empty:
            raise RuntimeError(
                f'Futu combination position is unsupported for {symbol}: '
                f'position_type={position_type or "UNKNOWN"}, combo_id={combo_id or "UNKNOWN"}'
            )
        if sellable is not None and sellable < 0:
            raise RuntimeError(f'Futu position has negative sellable quantity: {raw_sellable!r}')
        return {
            'symbol': symbol,
            'qty': qty,
            'sellable': sellable,
            'price': price,
            'position_side': position_side,
            'position_type': position_type,
            'market_value': (
                parsed_market_value
                if (parsed_market_value := _decimal(
                    _field(row, ('market_val', 'market_value'), None), None
                )) is not None and parsed_market_value > 0
                else None
            ),
            'currency': _text(_field(row, 'currency', '')),
            'position_id': _text(_field(row, ('position_id', 'id'), '')),
            'market': _text(market_hint).upper(),
        }

    def get_position(self, data):
        """读取指定标的总仓位和可卖数量；查询失败时抛出以便引擎失败关闭。"""
        target = _normalise_symbol(getattr(data, '_name', ''))
        if not target:
            raise RuntimeError('Futu position query requires data._name')
        rows = self._query_position_rows(target)
        target_rows = []
        for row in rows:
            row_symbol = _normalise_symbol(
                _field(row, ('code', 'symbol', 'stock_code'), ''),
                _field(row, ('position_market', 'market'), ''),
            )
            if row_symbol == target or not row_symbol:
                # 目标行缺少可识别代码时仍交给严格解析，不能静默当作空仓。
                target_rows.append(row)
        try:
            matched = [self._position_record(row) for row in target_rows]
        except Exception as exc:
            self._last_position_snapshot_fetch_failed = True
            self._last_position_snapshot_fetch_error = exc
            raise
        for position in matched:
            if position['qty'] < 0:
                raise RuntimeError(f'Futu position has negative quantity: {target!r}')
            if position['price'] < 0:
                raise RuntimeError(f'Futu position has negative cost: {target!r}')
        matched = [row for row in matched if row['symbol'] == target]
        if not matched:
            return SimpleNamespace(size=0, price=0.0, sellable=0, position_id='')

        total_qty = sum((item['qty'] for item in matched), Decimal('0'))
        weighted_value = sum((item['qty'] * item['price'] for item in matched), Decimal('0'))
        cost = weighted_value / total_qty if total_qty > 0 else Decimal('0')
        sellable_values = [item['sellable'] for item in matched if item['sellable'] is not None]
        if sellable_values:
            sellable = sum(sellable_values, Decimal('0'))
        elif target.split('.', 1)[0] in {'SH', 'SZ'}:
            # A 股没有明确 can_sell_qty 时宁可禁止卖出，也不把 T+1 仓位误作可卖。
            sellable = Decimal('0')
        else:
            sellable = total_qty
        return SimpleNamespace(
            size=quantity_number(total_qty),
            price=float(cost),
            sellable=quantity_number(max(Decimal('0'), sellable)),
            position_id=next((item['position_id'] for item in matched if item['position_id']), ''),
        )

    def get_sellable_position(self, data):
        position = self.get_position(data)
        return positive_quantity(getattr(position, 'sellable', 0) or 0)

    def get_current_price(self, data) -> float:
        """查询 Futu 实时行情；实盘行情不可用时失败关闭，不使用旧 bar 下单。"""
        symbol = _normalise_symbol(getattr(data, '_name', ''))
        quote_context = self._get_quote_context() if symbol else None
        if quote_context is not None and symbol:
            try:
                with self._context_lock:
                    response = quote_context.get_market_snapshot([symbol])
                if not isinstance(response, tuple) or len(response) < 2 or response[0] != RET_OK:
                    detail = response[1] if isinstance(response, tuple) and len(response) >= 2 else response
                    raise RuntimeError(f'Futu market snapshot rejected: {detail}')
                rows = _rows(response[1])
                for row in rows:
                    row_symbol = _normalise_symbol(
                        _field(row, ('code', 'symbol'), ''),
                        _field(row, 'market', ''),
                    )
                    if row_symbol and row_symbol != symbol:
                        continue
                    self._cache_contract_multiplier(row_symbol or symbol, row)
                    price = _decimal(
                        _field(row, ('last_price', 'last', 'price', 'close', 'nominal_price'), None),
                        None,
                    )
                    if price is not None and price > 0:
                        return float(price)
                # 已提供行情上下文但柜台未返回可信报价时，不能把旧 bar 当作实时价。
                return 0.0
            except Exception as exc:
                if self._owns_quote_ctx:
                    try:
                        quote_context.close()
                    except Exception:
                        pass
                    with self._context_lock:
                        if self._quote_ctx is quote_context:
                            self._quote_ctx = None
                            self.quote_ctx = None
                            self._owns_quote_ctx = False
                self._quote_context_init_failed = True
                self._quote_context_retry_at = (
                    time.monotonic() + _QUOTE_CONTEXT_RETRY_BACKOFF_SECONDS
                )
                self._runtime_log(f'[FutuBroker] Realtime quote failed for {symbol}: {exc}')
                return 0.0
        if symbol:
            self._runtime_log(f'[FutuBroker] Realtime quote unavailable for {symbol}; order skipped.')
        return 0.0

    def is_trading_slot(self, now=None, slot_key=None, phase='slot', symbols=None) -> bool:
        """判断 Futu 调度槽位是否处于目标证券的可交易状态。"""
        try:
            timestamp = pd.Timestamp(now or datetime.datetime.now())
        except Exception:
            return False
        if timestamp.weekday() >= 5:
            return False
        if phase == 'prewarm':
            # 预热允许在开盘前执行，但周末不启动无意义的请求。
            return True

        raw_symbols = symbols
        if raw_symbols is None or (not isinstance(raw_symbols, str) and not raw_symbols):
            raw_symbols = [
                _text(getattr(data, '_name', ''))
                for data in getattr(self, 'datas', []) or []
                if _text(getattr(data, '_name', ''))
            ]
        if isinstance(raw_symbols, str):
            raw_symbols = [item for item in re.split(r'[\s,]+', raw_symbols) if item]
        codes = [_normalise_symbol(item) for item in (raw_symbols or [])]
        codes = [code for code in codes if code]
        if not codes:
            return False
        # 统一经过入口检查 CLOSED/CLOSING 状态，必要时重建自有行情连接。
        quote_context = self._get_quote_context()
        if quote_context is None:
            self._runtime_log(
                f'[FutuBroker] Cannot verify trading state for slot {slot_key or "unknown"}; '
                'quote context unavailable.'
            )
            return False
        try:
            with self._context_lock:
                response = quote_context.get_market_state(codes)
            if not isinstance(response, tuple) or len(response) < 2 or response[0] != RET_OK:
                return False
            states = {}
            for row in _rows(response[1]):
                code = _normalise_symbol(_field(row, ('code', 'symbol'), ''))
                state = _enum_name(_field(row, ('market_state', 'state'), ''))
                if code:
                    states[code] = state
            allowed_states = set(_TRADABLE_MARKET_STATES)
            if bool(self._current_setting('FUTU_FILL_OUTSIDE_RTH', False)):
                allowed_states.update(_EXTENDED_MARKET_STATES)
            return all(states.get(code) in allowed_states for code in codes)
        except Exception as exc:
            self._runtime_log(f'[FutuBroker] Trading-state query failed for slot {slot_key}: {exc}')
            return False

    def _query_order_rows(self):
        context = self._get_trade_context()
        if context is None:
            raise RuntimeError('Futu trade context is unavailable')
        with self._context_lock:
            response = context.order_list_query(
                status_filter_list=[],
                trd_env=self._trade_env_value(),
                acc_id=self._account_id_value(),
                acc_index=self._account_index_value(),
                refresh_cache=True,
            )
        if not isinstance(response, tuple) or len(response) < 2:
            raise RuntimeError('Futu order_list_query returned an invalid response')
        ret_code, data = response[0], response[1]
        if ret_code != RET_OK:
            raise RuntimeError(f'Futu order_list_query failed: {data}')
        return _rows(data)

    def get_pending_orders(self) -> list:
        """读取可信在途快照，失败时返回空列表并设置健康标记。"""
        if not self.is_live:
            self._last_pending_orders_fetch_failed = False
            self._last_pending_orders_fetch_error = None
            return []

        try:
            result = []
            for row in self._query_order_rows():
                order_id = _text(_field(row, ('order_id', 'orderId', 'id'), ''))
                symbol = _symbol_from_order(row)
                side = _enum_name(_field(row, ('trd_side', 'side', 'action'), ''))
                status = _enum_name(_field(row, ('order_status', 'status'), ''))
                if not order_id or order_id.upper() in {'N/A', 'NA', 'NONE', 'NAN', 'NULL'}:
                    raise RuntimeError('Futu pending order is missing order_id')
                if not symbol:
                    raise RuntimeError(f'Futu pending order is missing code: id={order_id!r}')
                if side not in {'BUY', 'SELL'}:
                    raise RuntimeError(
                        f'Futu pending order has unknown direction: id={order_id!r}, side={side!r}'
                    )
                if not status:
                    raise RuntimeError(f'Futu pending order is missing status: id={order_id!r}')
                if status in self._TERMINAL_STATUSES:
                    continue
                if status not in self._PENDING_STATUSES:
                    raise RuntimeError(
                        f'Futu pending order has unknown non-terminal status: id={order_id!r}, status={status!r}'
                    )

                requested = _decimal(_field(row, ('qty', 'volume', 'total_quantity'), None), None)
                dealt = _decimal(_field(row, ('dealt_qty', 'filled_qty', 'filled_volume'), 0), Decimal('0'))
                reported_remaining = _decimal(
                    _field(row, ('remaining', 'remaining_qty'), None),
                    None,
                )
                if reported_remaining is not None:
                    if reported_remaining < 0:
                        raise RuntimeError(
                            f'Futu pending order has negative remaining quantity: id={order_id!r}'
                        )
                    remaining = reported_remaining
                else:
                    if requested is None or dealt is None or requested < 0 or dealt < 0:
                        raise RuntimeError(f'Futu pending order has invalid quantity: id={order_id!r}')
                    remaining = requested - dealt
                if remaining < 0:
                    raise RuntimeError(f'Futu pending order has negative remaining quantity: id={order_id!r}')
                if remaining == 0:
                    if requested is not None and dealt is not None and requested > 0 and dealt >= requested:
                        continue
                    raise RuntimeError(
                        f'Futu pending order has no verifiable remaining quantity: id={order_id!r}'
                    )
                result.append({
                    'id': order_id,
                    'symbol': self._framework_symbol(symbol),
                    'direction': side,
                    'size': quantity_number(remaining),
                })
            self._last_pending_orders_fetch_failed = False
            self._last_pending_orders_fetch_error = None
            return result
        except Exception as exc:
            self._last_pending_orders_fetch_failed = True
            self._last_pending_orders_fetch_error = exc
            self._runtime_log(f'[FutuBroker] 获取在途订单失败: {exc}')
            return []

    def _framework_symbol(self, futu_symbol):
        """优先返回已加载 data 的原始名称，保证预期仓位精确匹配。"""
        for data in self.datas:
            raw_name = _text(getattr(data, '_name', ''))
            if raw_name and _normalise_symbol(raw_name) == futu_symbol:
                return raw_name
        return futu_symbol

    def cancel_pending_order(self, order_id: str) -> bool:
        """按最新可信快照撤销一笔富途在途订单。"""
        if not self.is_live:
            return False
        oid = _text(order_id)
        if not oid or oid.upper() in {'N/A', 'NA', 'NONE', 'NAN', 'NULL'}:
            return False
        try:
            pending = self.get_pending_orders()
            if self._last_pending_orders_fetch_failed:
                self._runtime_log(
                    f'[FutuBroker] cancel_pending_order skipped ({oid}): '
                    f'untrusted snapshot ({self._last_pending_orders_fetch_error})'
                )
                return False
            if not any(_text(item.get('id')) == oid for item in pending):
                return False
            context = self._get_trade_context()
            if context is None:
                return False
            cancel_op = _enum_value(ModifyOrderOp, 'CANCEL', 'CANCEL')
            with self._context_lock:
                try:
                    response = context.modify_order(
                        modify_order_op=cancel_op,
                        order_id=oid,
                        qty=0,
                        price=0,
                        trd_env=self._trade_env_value(),
                        acc_id=self._account_id_value(),
                        acc_index=self._account_index_value(),
                    )
                except TypeError:
                    # 兼容旧 SDK/轻量替身将操作枚举声明为首个位置参数的签名。
                    response = context.modify_order(
                        cancel_op,
                        order_id=oid,
                        qty=0,
                        price=0,
                        trd_env=self._trade_env_value(),
                        acc_id=self._account_id_value(),
                        acc_index=self._account_index_value(),
                    )
            if isinstance(response, tuple):
                return len(response) >= 1 and response[0] == RET_OK
            return response is True or response == RET_OK
        except Exception as exc:
            self._runtime_log(f'[FutuBroker] cancel_pending_order failed ({oid}): {exc}')
            return False

    def _order_type_value(self):
        return _enum_value(
            OrderType,
            self._current_setting('FUTU_ORDER_TYPE', 'NORMAL'),
            'NORMAL',
        )

    def _time_in_force_value(self):
        return _enum_value(
            TimeInForce,
            self._current_setting(
                'FUTU_TIME_IN_FORCE',
                'DAY',
            ),
            'DAY',
        )

    def _submit_order(self, data, volume, side, price):
        """将框架 BUY/SELL 翻译为 Futu place_order；适配器不自行拆单。"""
        side_name = _text(side).upper()
        if side_name not in {'BUY', 'SELL'}:
            self._last_order_target_skip_reason = 'invalid_order_side'
            return None
        symbol = _normalise_symbol(getattr(data, '_name', ''))
        quantity = _decimal(volume, None)
        if not symbol or quantity is None or quantity <= 0:
            self._last_order_target_skip_reason = 'invalid_order_quantity'
            return None

        lot_limit = _decimal(self._current_setting('BROKER_LOT_LIMITS', 0), Decimal('0'))
        if lot_limit is not None and lot_limit > 0 and quantity > lot_limit:
            self._last_order_target_skip_reason = 'broker_lot_limit_exceeded'
            self._runtime_log(
                f'[FutuBroker] {side_name} {symbol} quantity {quantity} exceeds '
                f'BROKER_LOT_LIMITS={lot_limit}; base layer should split first.'
            )
            return None

        order_type = self._order_type_value()
        order_type_name = _enum_name(order_type)
        order_price = _decimal(price, None)
        if order_type_name != 'MARKET' and (order_price is None or order_price <= 0):
            self._last_order_target_skip_reason = 'invalid_order_price'
            return None
        if order_price is None or order_price < 0:
            order_price = Decimal('0')

        market = symbol.split('.', 1)[0]
        if side_name == 'BUY' and market in {'SH', 'SZ'} and not self._is_option_symbol(symbol):
            lot_size = self._positive_decimal(self.get_order_lot_size(data)) or Decimal('100')
            if quantity % lot_size != 0:
                self._last_order_target_skip_reason = 'a_share_buy_lot_misaligned'
                self._runtime_log(
                    f'[FutuBroker] A-share BUY {symbol} quantity {quantity} is not '
                    f'aligned to lot size {lot_size}; order skipped.'
                )
                return None

        unit_value = self._order_unit_value(data, order_price)
        if side_name == 'BUY' and unit_value <= 0:
            self._last_order_target_skip_reason = 'invalid_order_unit_value'
            return None

        context = self._get_trade_context()
        if context is None:
            self._runtime_log(f'[FutuBroker] {side_name} {symbol} skipped: trade context unavailable')
            return None
        trd_side = _enum_value(TrdSide, side_name, side_name)
        fill_outside_rth = bool(
            self._current_setting(
                'FUTU_FILL_OUTSIDE_RTH',
                False,
            )
        )
        try:
            with self._context_lock:
                response = context.place_order(
                    price=float(order_price),
                    qty=float(quantity),
                    code=symbol,
                    trd_side=trd_side,
                    order_type=order_type,
                    trd_env=self._trade_env_value(),
                    acc_id=self._account_id_value(),
                    acc_index=self._account_index_value(),
                    time_in_force=self._time_in_force_value(),
                    fill_outside_rth=fill_outside_rth,
                )
            if not isinstance(response, tuple) or len(response) < 2:
                raise RuntimeError('Futu place_order returned an invalid response')
            ret_code, payload = response[0], response[1]
            if ret_code != RET_OK:
                message = f'Futu place_order rejected {symbol} {side_name}: {payload}'
                self._runtime_log(message)
                runtime_notifications.push_text(message, level='ERROR')
                return None

            record = _rows(payload)
            raw_order = _order_dict(record[0]) if record else {}
            enriched = dict(raw_order)
            enriched.setdefault('code', symbol)
            enriched.setdefault('trd_side', side_name)
            enriched.setdefault('qty', quantity_number(quantity))
            enriched.setdefault('price', float(order_price))
            enriched.setdefault('order_status', 'SUBMITTED')
            proxy = FutuOrderProxy(
                enriched,
                is_live=self.is_live,
                data=data,
                contract_multiplier=self._contract_multiplier(data),
            )
            if not proxy.id:
                message = f'Futu place_order returned no order_id for {symbol} {side_name}'
                self._runtime_log(message)
                runtime_notifications.push_text(message, level='ERROR')
                return None
            proxy.submitted_size = quantity_number(quantity)
            proxy.requested_size = quantity_number(quantity)
            if side_name == 'BUY':
                proxy.reserved_cash = float(
                    quantity * Decimal(str(unit_value)) * Decimal(str(self.safety_multiplier))
                )
            return proxy
        except Exception as exc:
            message = f'Futu place_order failed for {symbol} {side_name}: {exc}'
            self._runtime_log(message)
            runtime_notifications.push_text(message, level='ERROR')
            return None

    def convert_order_proxy(self, raw_order) -> 'BaseOrderProxy':
        """将 Futu 订单回调转换为代理，并按规范精确匹配 data。"""
        if isinstance(raw_order, pd.DataFrame):
            records = _rows(raw_order)
            raw_order = records[0] if records else {}
        elif isinstance(raw_order, (list, tuple)) and len(raw_order) == 1:
            raw_order = raw_order[0]

        target_symbol = _symbol_from_order(raw_order)
        matched_data = None
        for data in self.datas:
            data_symbol = _normalise_symbol(getattr(data, '_name', ''))
            if target_symbol and data_symbol == target_symbol:
                matched_data = data
                break
        multiplier = self._contract_multiplier(matched_data) if matched_data is not None else None
        return FutuOrderProxy(
            raw_order,
            is_live=self.is_live,
            data=matched_data,
            contract_multiplier=multiplier,
        )

    @staticmethod
    def is_live_mode(context) -> bool:
        """Futu Broker 只用于 OpenD 仿真或实盘交易。"""
        return True

    @staticmethod
    def extract_run_config(context) -> dict:
        return {}

    @staticmethod
    def _apply_connection_settings(host, port, rsa_path):
        """同步连接环境，确保独立创建的 FutuDataProvider 使用同一 OpenD。"""
        config.FUTU_HOST = host
        config.FUTU_PORT = port
        config.FUTU_RSA_KEY_PATH = _text(rsa_path)

    @classmethod
    def launch(cls, conn_cfg: dict, strategy_path: str, params: dict, **kwargs):
        """启动富途 Phoenix 实盘循环，支持 schedule 或行情订阅事件触发。"""
        conn_cfg = dict(conn_cfg or {})
        schedule_rule = conn_cfg.get('schedule') or kwargs.get('schedule')
        trigger_value = _text(
            conn_cfg.get(
                'trigger',
                conn_cfg.get('execution_mode', kwargs.get('trigger', '')),
            ),
            '',
        ).lower()
        subscription_mode = trigger_value in {
            'event', 'events', 'subscription', 'subscribe', 'quote', 'kline',
        }
        if subscription_mode and schedule_rule:
            raise ValueError(
                'Futu event subscription mode cannot be combined with schedule; '
                'choose trigger=subscription or a schedule rule.'
            )
        parsed_schedule = None
        if schedule_rule:
            parsed_schedule = SchedulePlanner.parse_schedule_rule(schedule_rule)
            if parsed_schedule is None:
                raise ValueError(
                    f'Unsupported Futu schedule format: {schedule_rule}; '
                    'expected 1d|Nm|Nh:HH:MM[:SS].'
                )

        timezone_name = conn_cfg.get('timezone')
        target_tz = None
        if timezone_name:
            try:
                import pytz
                target_tz = pytz.timezone(str(timezone_name))
            except Exception as exc:
                raise ValueError(f'Invalid Futu schedule timezone: {timezone_name}') from exc

        def now_value():
            return datetime.datetime.now(target_tz) if target_tz is not None else datetime.datetime.now()

        host = conn_cfg.get('host', getattr(config, 'FUTU_HOST', '127.0.0.1'))
        port = cls._safe_int(conn_cfg.get('port', getattr(config, 'FUTU_PORT', 11111)), 11111)
        rsa_path = conn_cfg.get('rsa_key_path', getattr(config, 'FUTU_RSA_KEY_PATH', ''))
        trade_env = conn_cfg.get(
            'trd_env',
            conn_cfg.get('trade_env', getattr(config, 'FUTU_TRADE_ENV', 'SIMULATE')),
        )
        filter_market = conn_cfg.get(
            'filter_trdmarket',
            conn_cfg.get('trd_market', getattr(config, 'FUTU_FILTER_TRDMARKET', 'N/A')),
        )
        account_id = conn_cfg.get(
            'account_id',
            conn_cfg.get('acc_id', getattr(config, 'FUTU_ACCOUNT_ID', 0)),
        )
        account_index = conn_cfg.get(
            'account_index',
            conn_cfg.get('acc_index', getattr(config, 'FUTU_ACCOUNT_INDEX', 0)),
        )
        cls._apply_connection_settings(host, port, rsa_path)

        class Context:
            now = None
            strategy_instance = None
            futu_trade_context = None
            futu_quote_context = kwargs.get('quote_ctx')

        ctx = Context()
        ctx.schedule_rule = schedule_rule
        ctx.use_schedule = bool(schedule_rule)
        ctx._futu_runtime_config = {
            'FUTU_HOST': host,
            'FUTU_PORT': port,
            'FUTU_RSA_KEY_PATH': rsa_path,
            'FUTU_TRADE_ENV': trade_env,
            'FUTU_FILTER_TRDMARKET': filter_market,
            'FUTU_ACCOUNT_ID': account_id,
            'FUTU_ACCOUNT_INDEX': account_index,
            'FUTU_SECURITY_FIRM': conn_cfg.get(
                'security_firm', getattr(config, 'FUTU_SECURITY_FIRM', 'N/A')
            ),
            'FUTU_ACCOUNT_CURRENCY': conn_cfg.get(
                'currency', getattr(config, 'FUTU_ACCOUNT_CURRENCY', 'HKD')
            ),
            'FUTU_ORDER_TYPE': conn_cfg.get(
                'order_type', getattr(config, 'FUTU_ORDER_TYPE', 'NORMAL')
            ),
            'FUTU_TIME_IN_FORCE': conn_cfg.get(
                'time_in_force', getattr(config, 'FUTU_TIME_IN_FORCE', 'DAY')
            ),
            'FUTU_FILL_OUTSIDE_RTH': conn_cfg.get(
                'fill_outside_rth', getattr(config, 'FUTU_FILL_OUTSIDE_RTH', False)
            ),
        }
        ctx.now = pd.Timestamp(now_value())

        symbols = kwargs.get('symbols', [])
        engine_config = config.__dict__.copy()
        # 将连接环境的账户/交易路由写入本轮配置快照，避免 LiveTrader
        # 在 broker 初始化后复制模块默认值而覆盖 sim/real 选择。
        engine_config.update(ctx._futu_runtime_config)
        engine_config.update({
            'strategy_name': strategy_path,
            'params': params,
            'platform': 'futu',
            'symbols': symbols,
            '_suppress_start_alarm': bool(kwargs.get('_suppress_start_alarm', False)),
        })
        for name in ('timeframe', 'compression', 'data_source', 'selection', 'risk', 'risk_params'):
            if kwargs.get(name) is not None:
                key = 'selection_name' if name == 'selection' else name
                engine_config[key] = kwargs.get(name)

        runtime_print(f'>>> Launching {cls.__name__} (OpenD {host}:{port}) <<<')
        from live_trader.engine import LiveTrader, on_order_status_callback

        trader = LiveTrader(engine_config)
        trader.init(ctx)
        ctx.strategy_instance = trader

        trade_context = getattr(trader.broker, '_trade_ctx', None)
        if trade_context is None:
            raise RuntimeError('Futu trade context did not initialize')

        if TradeOrderHandlerBase is not None:
            def on_order_update(order):
                on_order_status_callback(ctx, order)

            handler = _FutuTradeOrderHandler(on_order_update)
            set_handler = getattr(trade_context, 'set_handler', None)
            if callable(set_handler):
                set_handler(handler)
        start_context = getattr(trade_context, 'start', None)
        if callable(start_context):
            start_context()

        prewarm_lead_seconds = 0.0
        if parsed_schedule:
            try:
                prewarm_lead_seconds = SchedulePlanner.parse_schedule_prewarm_lead(
                    getattr(config, 'LIVE_SCHEDULE_PREWARM_LEAD', 0)
                )
            except Exception as exc:
                runtime_print(
                    f'[FutuBroker] Invalid LIVE_SCHEDULE_PREWARM_LEAD: {exc}; prewarm disabled.'
                )
            if prewarm_lead_seconds >= float(parsed_schedule.get('interval_seconds') or 0.0):
                prewarm_lead_seconds = 0.0

        def run_slot(now_snapshot, slot_key):
            """在 SDK 事件循环之外执行一个实盘调度槽位。"""
            run_context = copy.copy(ctx)
            run_context.now = now_snapshot
            run_context.strategy_instance = trader
            run_context.use_schedule = True
            trader.run(run_context)

        def run_prewarm(now_snapshot, slot_key):
            """执行单个 schedule 槽位的轻量数据预热。"""
            prewarm_symbols = [
                _text(getattr(data, '_name', ''))
                for data in getattr(trader.broker, 'datas', [])
                if _text(getattr(data, '_name', ''))
            ]
            if not prewarm_symbols:
                prewarm_symbols = list(symbols or [])
            summary = trader.broker.run_schedule_prewarm(
                schedule_rule=schedule_rule,
                data_provider=getattr(trader, 'data_provider', None),
                symbols=prewarm_symbols,
                timeframe=kwargs.get('timeframe', 'Days') or 'Days',
                compression=kwargs.get('compression', 1) or 1,
                now=now_snapshot,
            )
            runtime_print(f'[FutuBroker] Prewarm finished for slot {slot_key}: {summary}')
            return summary

        event_subtype_name = None
        event_symbols = []
        event_last_keys = {}
        event_run_lock = threading.Lock()
        event_run_thread = None

        def _resolve_event_symbols():
            """读取当前引擎实际管理的标的，并转换为 Futu 代码。"""
            raw_symbols = [
                _text(getattr(data, '_name', ''))
                for data in getattr(trader.broker, 'datas', []) or []
                if _text(getattr(data, '_name', ''))
            ]
            if not raw_symbols:
                raw_symbols = list(symbols or [])
            resolved = []
            seen = set()
            for raw_symbol in raw_symbols:
                code = _normalise_symbol(raw_symbol)
                if code and code not in seen:
                    resolved.append(code)
                    seen.add(code)
            return resolved

        def _event_rows_and_keys(content):
            """提取订阅批次的去重键和触发时间。"""
            rows = _rows(content)
            updates = []
            latest_timestamp = _futu_event_timestamp(content, target_tz=target_tz)
            for row in rows:
                code = _normalise_symbol(
                    _field(row, ('code', 'symbol', 'stock_code'), '')
                ) or '__batch__'
                raw_time = _field(row, _FUTU_EVENT_TIME_FIELDS, None)
                if raw_time is not None and _text(raw_time):
                    try:
                        marker = pd.Timestamp(raw_time)
                        if marker.tzinfo is None and target_tz is not None:
                            marker = marker.tz_localize(target_tz)
                        marker = marker.isoformat()
                    except Exception:
                        marker = _text(raw_time)
                elif latest_timestamp is not None:
                    marker = latest_timestamp.isoformat()
                else:
                    # 没有时间字段的 ticker/轻量替身按每次回调处理，不把事件永久锁死。
                    marker = f'callback:{time.monotonic_ns()}'
                if event_last_keys.get(code) == marker:
                    continue
                event_last_keys[code] = marker
                updates.append((code, marker))
            if len(event_last_keys) > 5000:
                event_last_keys.clear()
            return updates, latest_timestamp

        def _run_event_worker(now_snapshot, event_updates):
            """在 Futu 行情回调线程之外执行一次策略运行。"""
            nonlocal event_run_thread
            run_context = copy.copy(ctx)
            run_context.now = now_snapshot
            run_context.strategy_instance = trader
            run_context.use_schedule = False
            succeeded = False
            try:
                if not trader.broker.is_trading_slot(
                    now=now_snapshot,
                    phase='slot',
                    symbols=event_symbols,
                ):
                    runtime_print(
                        '[FutuBroker] subscription event arrived outside a verified '
                        'trading slot; skipping strategy run.'
                    )
                    succeeded = True
                    return
                trader.run(run_context)
                succeeded = True
            except Exception as exc:
                runtime_print(f'[FutuBroker] subscription event run failed: {exc}')
                runtime_notifications.push_text(
                    f'Futu subscription event run failed: {exc}',
                    level='ERROR',
                )
            finally:
                with event_run_lock:
                    if not succeeded:
                        for code, marker in event_updates:
                            if event_last_keys.get(code) == marker:
                                event_last_keys.pop(code, None)
                    if event_run_thread is threading.current_thread():
                        event_run_thread = None

        def on_quote_event(content):
            """处理 Futu 订阅推送并对同一事件批次去重。"""
            nonlocal event_run_thread
            updates, latest_timestamp = _event_rows_and_keys(content)
            if not updates:
                return
            event_now = latest_timestamp or pd.Timestamp(now_value())
            with event_run_lock:
                previous = event_run_thread
                if previous is not None and previous.is_alive():
                    runtime_print(
                        '[FutuBroker] previous subscription event run is still running; '
                        'skipping overlapping event.'
                    )
                    return
                worker = threading.Thread(
                    target=_run_event_worker,
                    args=(event_now, updates),
                    name='quantada-futu-event-run',
                    daemon=True,
                )
                event_run_thread = worker
            worker.start()

        def start_event_subscription():
            """建立 Futu 行情订阅，并把 SDK 推送绑定到事件 worker。"""
            nonlocal event_subtype_name, event_symbols
            event_timeframe = kwargs.get('timeframe') or engine_config.get('timeframe', 'Days')
            event_compression = kwargs.get('compression')
            if event_compression is None:
                event_compression = engine_config.get('compression', 1)
            event_subtype_name = _futu_event_subtype_name(
                conn_cfg.get('event_subtype', kwargs.get('event_subtype')),
                timeframe=event_timeframe,
                compression=event_compression,
            )
            event_symbols = _resolve_event_symbols()
            if not event_symbols:
                raise RuntimeError(
                    'Futu event subscription requires at least one managed symbol; '
                    'provide --symbols or a selector that loads data.'
                )
            quote_context = trader.broker._get_quote_context()
            if quote_context is None:
                raise RuntimeError(
                    'Futu quote context is unavailable for event subscription; '
                    'check OpenD, permissions, host/port and RSA settings.'
                )
            handler_base = _FUTU_EVENT_SUBTYPE_BASES.get(event_subtype_name)
            handler = _create_futu_quote_event_handler(handler_base, on_quote_event)
            set_handler = getattr(quote_context, 'set_handler', None)
            if not callable(set_handler):
                raise RuntimeError('Futu quote context does not expose set_handler()')
            handler_result = set_handler(handler)
            if handler_result not in (None, RET_OK, True):
                raise RuntimeError(f'Futu quote set_handler failed: {handler_result}')
            subscribe = getattr(quote_context, 'subscribe', None)
            if not callable(subscribe):
                raise RuntimeError('Futu quote context does not expose subscribe()')
            subscribe_result = subscribe(
                event_symbols,
                [_futu_event_subtype_value(event_subtype_name)],
                is_first_push=False,
                subscribe_push=True,
            )
            if (
                isinstance(subscribe_result, tuple)
                and subscribe_result
                and subscribe_result[0] != RET_OK
            ):
                detail = subscribe_result[1] if len(subscribe_result) > 1 else subscribe_result
                raise RuntimeError(f'Futu quote subscribe failed: {detail}')
            start_context = getattr(quote_context, 'start', None)
            if callable(start_context):
                start_result = start_context()
                if start_result not in (None, RET_OK, True):
                    raise RuntimeError(f'Futu quote context start failed: {start_result}')
            runtime_print(
                f'[FutuBroker] Event subscription enabled: {event_subtype_name} '
                f'for {len(event_symbols)} symbols.'
            )

        def on_slot_error(error, slot_key):
            runtime_print(f'[FutuBroker] schedule slot {slot_key} failed: {error}')
            runtime_notifications.push_text(
                f'Futu schedule slot {slot_key} failed: {error}',
                level='ERROR',
            )

        def on_prewarm_error(error, slot_key):
            runtime_print(f'[FutuBroker] Prewarm failed for slot {slot_key}: {error}')
            runtime_notifications.push_text(
                f'Futu prewarm failed for slot {slot_key}: {error}',
                level='ERROR',
            )

        def slot_filter(now_snapshot, slot_key, phase='slot'):
            managed_symbols = [
                _text(getattr(data, '_name', ''))
                for data in getattr(trader.broker, 'datas', []) or []
                if _text(getattr(data, '_name', ''))
            ]
            if not managed_symbols:
                managed_symbols = symbols
            return trader.broker.is_trading_slot(
                now=now_snapshot,
                slot_key=slot_key,
                phase=phase,
                symbols=managed_symbols,
            )

        schedule_runner = LiveScheduleRunner(
            schedule_rule=schedule_rule,
            parsed_schedule=parsed_schedule,
            on_slot=run_slot,
            on_prewarm=run_prewarm,
            on_slot_error=on_slot_error,
            on_prewarm_error=on_prewarm_error,
            slot_filter=slot_filter,
            prewarm_lead_seconds=prewarm_lead_seconds,
            clock=now_value,
            runtime_log=runtime_print,
            idle_interval_seconds=5.0,
        )

        if subscription_mode:
            runtime_print(
                '[FutuBroker] Schedule disabled; strategy will run from Futu quote subscriptions.'
            )
        elif not parsed_schedule:
            runtime_print(
                '[FutuBroker] No schedule configured; connection stays alive without automatic runs.'
            )

        try:
            if subscription_mode:
                start_event_subscription()
                # quote context 的回调线程由 SDK 驱动；主线程只负责保活，不执行策略逻辑。
                stop_event = threading.Event()
                while not stop_event.wait(5.0):
                    pass
            else:
                schedule_runner.run_forever()
        except KeyboardInterrupt:
            runtime_print('[FutuBroker] User interrupted; closing OpenD contexts.')
        finally:
            try:
                data_manager = getattr(trader, '_data_manager', None)
                for provider in getattr(data_manager, 'providers', ()) or ():
                    # 某些 Provider 通过 __getattr__ 暴露动态 API；直接 getattr(provider, 'close')
                    # 可能被解释为一次远程 ``close`` 查询。只调用类层实际声明的 close 方法。
                    close_declared = any(
                        callable(base.__dict__.get('close'))
                        for base in type(provider).__mro__
                    )
                    if close_declared:
                        close_provider = getattr(provider, 'close')
                        if callable(close_provider):
                            close_provider()
            except Exception as exc:
                runtime_print(f'[FutuBroker] Failed to close data-provider contexts: {exc}')
            finally:
                trader.broker.close()


if TradeOrderHandlerBase is not None:
    class _FutuTradeOrderHandler(TradeOrderHandlerBase):
        """将 Futu 推送订单逐行交给引擎。"""

        def __init__(self, callback):
            super().__init__()
            self._callback = callback

        def on_recv_rsp(self, rsp_pb):
            ret_code, content = super().on_recv_rsp(rsp_pb)
            if ret_code == RET_OK:
                for row in _rows(content):
                    try:
                        self._callback(row)
                    except Exception as exc:
                        runtime_print(f'[FutuBroker] order callback failed: {exc}')
            return ret_code, content
else:
    class _FutuTradeOrderHandler:
        """未安装 SDK 时的占位类型，避免导入 adapter 失败。"""

        def __init__(self, callback):
            self._callback = callback
