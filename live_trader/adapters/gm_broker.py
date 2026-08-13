import datetime
import math
import os
import sys
from decimal import Decimal, InvalidOperation

import pandas as pd

import config
from alarms.manager import AlarmManager
from common.live_execution_budget import resolve_live_run_budget_seconds
from common.live_process_supervisor import (
    LiveWorkerFailureKind,
    is_live_worker_process,
    is_live_worker_restart,
    report_live_worker_state,
    request_live_worker_restart,
)
from common.live_runtime import runtime_print
from common.log import coerce_dt
from common.order_quantity import align_quantity_down, normalize_quantity_step, quantity_number
from data_providers.gm_provider import GmDataProvider as UnifiedGmDataProvider
from live_trader.engine import LiveTrader, on_order_status_callback
from ..data_bridge.data_warm import DAILY_SCHEDULE_HEALTH_LEAD_SECONDS, SchedulePlanner
from .base_broker import BaseLiveBroker, BaseOrderProxy


_GM_SDK_HEALTH_DEADLINE_SECONDS = 180.0
_GM_AGGRESSIVE_RETRY_SECONDS = 10.0
_GM_QUIET_PROBE_SECONDS = 10 * 60.0
_GM_CONNECTIVITY_LOG_INTERVAL_SECONDS = 10 * 60.0


def _resolve_gm_connectivity_retry(
    now,
    parsed_schedule,
    prewarm_lead_seconds=0.0,
    active_after_seconds=600.0,
):
    """Return ``(quiet, retry_delay, wake_at)`` for GM connectivity recovery."""

    if not parsed_schedule:
        return False, _GM_AGGRESSIVE_RETRY_SECONDS, None

    now_ts = pd.Timestamp(now)
    try:
        prewarm_lead = max(0.0, float(prewarm_lead_seconds or 0.0))
    except (TypeError, ValueError, OverflowError):
        prewarm_lead = 0.0
    if not math.isfinite(prewarm_lead):
        prewarm_lead = 0.0
    try:
        active_after = max(0.0, float(active_after_seconds or 0.0))
    except (TypeError, ValueError, OverflowError):
        active_after = 600.0
    if not math.isfinite(active_after):
        active_after = 600.0

    current_slot = SchedulePlanner.resolve_current_schedule_slot(now_ts, parsed_schedule)
    if parsed_schedule.get('kind') == 'daily':
        recovery_lead = max(
            float(DAILY_SCHEDULE_HEALTH_LEAD_SECONDS),
            prewarm_lead,
        )
        if current_slot is None:
            return False, _GM_AGGRESSIVE_RETRY_SECONDS, None
        current_slot = pd.Timestamp(current_slot)
        active_start = current_slot - pd.Timedelta(seconds=recovery_lead)
        active_end = current_slot + pd.Timedelta(seconds=active_after)
        if active_start <= now_ts <= active_end:
            return False, _GM_AGGRESSIVE_RETRY_SECONDS, None
        target_slot = (
            current_slot
            if now_ts < active_start
            else SchedulePlanner.advance_schedule_slot(current_slot, parsed_schedule)
        )
        wake_at = pd.Timestamp(target_slot) - pd.Timedelta(seconds=recovery_lead)
    else:
        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            return False, _GM_AGGRESSIVE_RETRY_SECONDS, None
        recovery_lead = max(
            prewarm_lead,
            max(0.0, interval_seconds - active_after),
        )
        if current_slot is not None:
            current_slot = pd.Timestamp(current_slot)
            if now_ts <= current_slot + pd.Timedelta(seconds=active_after):
                return False, _GM_AGGRESSIVE_RETRY_SECONDS, None

        next_slot = SchedulePlanner.resolve_next_schedule_slot(now_ts, parsed_schedule)
        if next_slot is None:
            return False, _GM_AGGRESSIVE_RETRY_SECONDS, None
        wake_at = pd.Timestamp(next_slot) - pd.Timedelta(seconds=recovery_lead)
        if now_ts >= wake_at:
            return False, _GM_AGGRESSIVE_RETRY_SECONDS, None

    seconds_to_wake = max(0.0, (wake_at - now_ts).total_seconds())
    if seconds_to_wake <= 0:
        return False, _GM_AGGRESSIVE_RETRY_SECONDS, None
    return True, min(_GM_QUIET_PROBE_SECONDS, seconds_to_wake), wake_at

try:
    from gm.api import order_target_percent, order_target_value, order_volume, current, get_cash, get_position as gm_get_position, subscribe, history, OrderType_Market, OrderType_Limit, MODE_LIVE, MODE_BACKTEST, \
        OrderStatus_New, OrderStatus_PartiallyFilled, OrderStatus_Filled, \
        OrderStatus_Canceled, OrderStatus_Rejected, OrderStatus_PendingNew, \
        OrderSide_Buy, OrderSide_Sell
    gm_api = sys.modules.get('gm.api')
    OrderStatus_PendingCancel = getattr(gm_api, 'OrderStatus_PendingCancel', object())
    OrderStatus_Expired = getattr(gm_api, 'OrderStatus_Expired', object())
except ImportError:
    print("Warning: 'gm' module not found. GmAdapter core API will not be available.")
    order_target_percent = order_target_value = get_cash = gm_get_position = subscribe = history = OrderType_Market = OrderType_Limit = None
    MODE_LIVE = MODE_BACKTEST = None
    OrderStatus_New = OrderStatus_PartiallyFilled = OrderStatus_Filled = None
    OrderStatus_Canceled = OrderStatus_Rejected = OrderStatus_PendingNew = None
    OrderStatus_PendingCancel = OrderStatus_Expired = None
    OrderSide_Buy = OrderSide_Sell = None

try:
    from gm.api import set_serv_addr, set_token, ADJUST_PREV
except ImportError:
    set_serv_addr = set_token = ADJUST_PREV = None

try:
    from gm.csdk.c_sdk import (
        py_gmi_set_strategy_id, gmi_set_mode, py_gmi_set_data_callback,
        py_gmi_set_backtest_config, py_gmi_run, gmi_init, gmi_poll,
        py_gmi_set_backtest_intraday
    )
except ImportError:
    py_gmi_set_strategy_id = gmi_set_mode = py_gmi_set_data_callback = None
    py_gmi_set_backtest_config = py_gmi_run = gmi_init = gmi_poll = None
    py_gmi_set_backtest_intraday = None

try:
    from gm.model.storage import context  # 掘金全局上下文
except ImportError:
    context = None

try:
    from gm.callback import callback_controller  # 掘金回调控制器
except ImportError:
    callback_controller = None

try:
    from gm.api._errors import check_gm_status
except ImportError:
    check_gm_status = None


class GmOrderProxy(BaseOrderProxy):
    """掘金平台的订单代理具体实现"""

    def __init__(self, order, is_live, data=None):
        self.platform_order = order
        self.is_live = is_live
        self.data = data

    @property
    def id(self):
        return self.platform_order.cl_ord_id

    @property
    def status(self):
        return self.platform_order.status

    @property
    def executed(self):
        """
        构造一个临时对象，模拟 Backtrader order.executed 的接口
        供策略层读取 size, price, value, comm
        """

        # 定义一个简单的类来承载数据
        class ExecutedStats:
            def __init__(self, gm_order):
                # 1. 成交数量
                self.size = gm_order.filled_volume

                # 2. 成交均价 (filled_vwap 是掘金的成交均价字段)
                self.price = gm_order.filled_vwap

                # 3. 成交金额 (Cost/Value)
                # 掘金通常有 filled_amount，如果没有则用 数量*均价 计算
                if hasattr(gm_order, 'filled_amount'):
                    self.value = gm_order.filled_amount
                else:
                    self.value = gm_order.filled_volume * gm_order.filled_vwap

                # 4. 手续费
                self.comm = getattr(gm_order, 'commission', 0.0)
                self.dt = GmOrderProxy._extract_execution_dt(gm_order)

        return ExecutedStats(self.platform_order)

    @staticmethod
    def _extract_execution_dt(order):
        candidate_fields = (
            'filled_at',
            'filled_time',
            'updated_at',
            'updated_time',
            'transact_time',
            'transaction_time',
        )
        for field in candidate_fields:
            dt = coerce_dt(getattr(order, field, None))
            if dt is not None:
                return dt
        return None

    # 根据模式动态判断
    def is_completed(self) -> bool:
        if self.is_live:
            # 实盘模式：必须是最终成交
            return self.platform_order.status == OrderStatus_Filled
        else:
            # 回测模式：放行 PendingNew (兼容掘金回测)
            # 因为回测框架不负责实盘的回测，且掘金的下单是异步过程无法实时获取订单状态，因此修改is_completed检查的常量。
            # 在实盘环境下仅触发信号，因此暂且放行OrderStatus_PendingNew挂单状态
            return self.platform_order.status == OrderStatus_Filled \
                or self.platform_order.status == OrderStatus_PendingNew

    def is_canceled(self) -> bool: return self.platform_order.status == OrderStatus_Canceled

    def is_rejected(self) -> bool: return self.platform_order.status == OrderStatus_Rejected

    def is_pending(self) -> bool:
        status = self.platform_order.status
        # 回测兼容：PendingNew 被上层视为 completed
        if not self.is_live and status == OrderStatus_PendingNew:
            return False
        active_states = {
            OrderStatus_New,
            OrderStatus_PartiallyFilled,
            OrderStatus_PendingNew,
            OrderStatus_PendingCancel,
        }
        return status in active_states

    def is_accepted(self) -> bool:
        status = self.platform_order.status
        # 回测兼容：PendingNew 被上层视为 completed
        if not self.is_live and status == OrderStatus_PendingNew:
            return False
        active_states = {
            OrderStatus_New,
            OrderStatus_PartiallyFilled,
            OrderStatus_PendingNew,
            OrderStatus_PendingCancel,
        }
        return status in active_states

    def is_buy(self) -> bool:
        return hasattr(self.platform_order, 'side') and self.platform_order.side == OrderSide_Buy

    def is_sell(self) -> bool:
        return hasattr(self.platform_order, 'side') and self.platform_order.side == OrderSide_Sell

class GmDataProvider(UnifiedGmDataProvider):
    def get_history(self, symbol: str, start_date: str, end_date: str,
                    timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        # 直接透传调用父类的 get_data
        return self.get_data(symbol, start_date, end_date, timeframe, compression)

class GmBrokerAdapter(BaseLiveBroker):
    """掘金平台交易执行器（GM session 绑定单一账户）。"""
    _DEFAULT_LIVE_SLIPPAGE = 0.0001

    def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None):
        if slippage_override is None:
            slippage_override = self._DEFAULT_LIVE_SLIPPAGE
        super().__init__(context, cash_override, commission_override, slippage_override)
        self.is_live = self.is_live_mode(context)  # 保存当前是否为实盘

    @staticmethod
    def _to_nonnegative_int(value, default=0):
        try:
            return max(0, int(float(value)))
        except Exception:
            return default

    def _find_position(self, symbol):
        if self.is_live:
            try:
                # GM schedule callbacks and QuantAda's bounded settlement wait run
                # on the SDK poll thread.  The context account cache therefore
                # cannot consume fill callbacks while that wait is active.  Use
                # the SDK's synchronous counter query as the live source of truth.
                positions = self._query_live_positions()
                self._last_position_snapshot_fetch_failed = False
                self._last_position_snapshot_fetch_error = None
            except Exception as e:
                self._last_position_snapshot_fetch_failed = True
                self._last_position_snapshot_fetch_error = e
                raise RuntimeError(f"GM live position query failed: {e}") from e
        else:
            if not hasattr(self._context, 'account'):
                return None
            try:
                positions = self._context.account().positions()
            except Exception:
                return None

        for p in positions:
            position_symbol = p.get('symbol') if isinstance(p, dict) else getattr(p, 'symbol', None)
            if position_symbol == symbol:
                return p
        return None

    @staticmethod
    def _object_field(value, field, default=None):
        if isinstance(value, dict):
            return value.get(field, default)
        return getattr(value, field, default)

    def _ensure_single_live_account(self):
        """Fail closed if a GM session unexpectedly exposes more than one account."""
        if not self.is_live or context is None or not hasattr(context, 'accounts'):
            return
        try:
            accounts = list((context.accounts or {}).values())
        except Exception as e:
            raise RuntimeError(f"GM account registry query failed: {e}") from e
        if len(accounts) != 1:
            raise RuntimeError(
                "GM session must expose exactly one account; "
                f"visible_accounts={len(accounts)}"
            )
        if not str(self._object_field(accounts[0], 'id', '') or '').strip():
            raise RuntimeError("GM account registry contains an account without id")

    def _fetch_cash_snapshot(self):
        self._ensure_single_live_account()
        try:
            return get_cash()
        except Exception as e:
            raise RuntimeError(f"GM cash query failed: {e}") from e

    def _query_live_positions(self):
        self._ensure_single_live_account()
        try:
            positions = gm_get_position()
        except Exception as e:
            raise RuntimeError(f"GM position query failed: {e}") from e
        if positions is None:
            raise RuntimeError("GM position snapshot is unavailable")
        return positions

    def _resolve_sellable_volume(self, p, total_size):
        """
        官方字段优先级:
        1) available_now (实盘当前可用仓位)
        2) available (非挂单冻结仓位)
        3) volume - volume_today (available_now 在回测不可用时的兜底)
        """
        raw_available_now = self._object_field(p, 'available_now')
        if raw_available_now is not None:
            return min(total_size, self._to_nonnegative_int(raw_available_now, default=0))

        raw_available = self._object_field(p, 'available')
        if raw_available is not None:
            return min(total_size, self._to_nonnegative_int(raw_available, default=0))

        raw_volume_today = self._object_field(p, 'volume_today')
        if raw_volume_today is not None:
            volume_today = self._to_nonnegative_int(raw_volume_today, default=0)
            return max(0, min(total_size, total_size - volume_today))

        return total_size

    def getcash(self):
        """ 获取可用资金 (Backtrader 命名风格)"""
        return self._fetch_real_cash()

    def getvalue(self):
        """获取账户总资产 (NAV)"""
        # get_cash() 返回的是 AccountCash 对象，.nav 即为总资产
        cash = self._fetch_cash_snapshot()
        nav = cash.get('nav') if isinstance(cash, dict) else getattr(cash, 'nav', None)
        if nav is None:
            raise RuntimeError("GM account cash snapshot has no nav field")
        return nav

    def get_pending_orders(self) -> list:
        """掘金：获取在途订单"""
        if not self.is_live:
            self._last_pending_orders_fetch_failed = False
            self._last_pending_orders_fetch_error = None
            return []  # 回测模式下引擎自带瞬间成交，无视在途

        res = []
        try:
            from gm.api import get_unfinished_orders, OrderSide_Buy
            orders = self._query_unfinished_orders_single_account(get_unfinished_orders)
            if orders is None:
                # Lightweight SDK/test doubles may not expose the native
                # per-account query primitives.  The real GM session does.
                orders = get_unfinished_orders()
            for o in orders:
                order_id = str(self._object_field(o, 'cl_ord_id', '') or '').strip()
                symbol = str(self._object_field(o, 'symbol', '') or '').strip()
                side = self._object_field(o, 'side')
                if side == OrderSide_Buy:
                    direction = 'BUY'
                elif side == OrderSide_Sell:
                    direction = 'SELL'
                else:
                    raise RuntimeError(
                        f"GM pending order has unknown side: id={order_id!r}, side={side!r}"
                    )

                raw_volume = self._object_field(o, 'volume', 0)
                raw_filled_volume = self._object_field(o, 'filled_volume', 0)
                try:
                    volume = Decimal(str(raw_volume if raw_volume not in (None, '') else 0))
                    filled_volume = Decimal(
                        str(raw_filled_volume if raw_filled_volume not in (None, '') else 0)
                    )
                except (InvalidOperation, TypeError, ValueError) as e:
                    raise RuntimeError(
                        f"GM pending order has invalid quantity: id={order_id!r}"
                    ) from e
                if (
                    not volume.is_finite()
                    or not filled_volume.is_finite()
                    or volume < 0
                    or filled_volume < 0
                ):
                    raise RuntimeError(f"GM pending order has invalid quantity: id={order_id!r}")
                remaining = volume - filled_volume
                if not order_id:
                    raise RuntimeError("GM pending order is missing cl_ord_id")
                if not symbol:
                    raise RuntimeError(f"GM pending order is missing symbol: id={order_id!r}")
                if not remaining.is_finite() or remaining <= 0:
                    raise RuntimeError(
                        f"GM pending order has non-positive remaining size: "
                        f"id={order_id!r}, volume={volume!r}, filled_volume={filled_volume!r}"
                    )
                res.append({
                    'id': order_id,
                    'symbol': symbol,
                    'direction': direction,
                    # 未成交数量 = 委托总数 - 已成交数
                    'size': quantity_number(remaining),
                    # GM 官方撤单接口要求订单自身的 account_id；这是 SDK
                    # 返回的订单元数据，不是可配置的账户路由。
                    'account_id': self._object_field(o, 'account_id', ''),
                })
            self._last_pending_orders_fetch_failed = False
            self._last_pending_orders_fetch_error = None
        except Exception as e:
            # A partially collected list is unsafe: callers may treat a
            # missing BUY/SELL as proof that it can submit or clean up another
            # order.  The health flag is the authoritative failure signal, and
            # returning an empty list prevents accidental direct consumers from
            # using partial broker truth.
            res = []
            self._last_pending_orders_fetch_failed = True
            self._last_pending_orders_fetch_error = e
            self._runtime_log(f"[GmBroker] 获取在途订单失败: {e}")
        return res

    def _query_unfinished_orders_single_account(self, public_query):
        """Read the sole GM account without inheriting the SDK's silent skip.

        ``gm.api.get_unfinished_orders`` loops through accounts and continues
        when one native request fails.  GM is a single-account adapter here, so
        query that one account directly when the SDK exposes its primitives;
        this lets the pending health flag distinguish an empty book from a
        failed request.  ``None`` is reserved for stripped-down test doubles.
        """
        if context is None or not self.is_live:
            return None
        try:
            account_values = list((context.accounts or {}).values())
        except Exception as e:
            raise RuntimeError(f"GM account registry query failed: {e}") from e
        if len(account_values) != 1:
            raise RuntimeError(
                "GM session must expose exactly one account for pending queries; "
                f"visible_accounts={len(account_values)}"
            )
        account_id = str(self._object_field(account_values[0], 'id', '') or '').strip()
        if not account_id:
            raise RuntimeError("GM account registry contains an account without id")

        sdk_globals = getattr(public_query, '__globals__', None)
        if not isinstance(sdk_globals, dict):
            raise RuntimeError("GM SDK pending-order query primitives are unavailable")
        request_cls = sdk_globals.get('GetUnfinishedOrdersReq')
        query = sdk_globals.get('py_gmi_get_unfinished_orders')
        status_failed = sdk_globals.get('c_status_fail')
        orders_cls = sdk_globals.get('Orders')
        to_dict = sdk_globals.get('protobuf_to_dict')
        dict_like_order = sdk_globals.get('DictLikeOrder')
        if not all(callable(item) for item in (request_cls, query, status_failed, orders_cls, to_dict, dict_like_order)):
            raise RuntimeError("GM SDK pending-order query primitives are unavailable")

        request = request_cls()
        request.account_id = account_id
        status, raw_result = query(request.SerializeToString())
        if status_failed(status, 'py_gmi_get_unfinished_orders'):
            raise RuntimeError(
                f"GM pending-order query failed for the sole account={account_id!r}, status={status!r}"
            )
        if not raw_result:
            return []

        parsed = orders_cls()
        parsed.ParseFromString(raw_result)
        result_orders = []
        for raw_order in parsed.data:
            try:
                order = to_dict(
                    raw_order,
                    including_default_value_fields=True,
                    dcls=dict_like_order,
                )
            except TypeError:
                order = to_dict(raw_order, including_default_value_fields=True)
            if isinstance(order, dict) and not order.get('account_id'):
                order['account_id'] = account_id
            result_orders.append(order)
        return result_orders

    def cancel_pending_order(self, order_id: str) -> bool:
        """掘金：按委托ID取消在途单（最小兼容实现）"""
        if not self.is_live:
            return False
        oid = str(order_id or '').strip()
        if not oid:
            return False

        try:
            import gm.api as gm_api
            pending_orders = self.get_pending_orders()
            if getattr(self, '_last_pending_orders_fetch_failed', False):
                detail = getattr(self, '_last_pending_orders_fetch_error', None)
                self._runtime_log(
                    f"[GmBroker] cancel_pending_order skipped ({oid}): untrusted snapshot ({detail})."
                )
                return False

            target = next(
                (po for po in pending_orders if str(po.get('id', '') or '') == oid),
                None,
            )
            if target is None:
                return False

            cancel_payload = {
                'cl_ord_id': oid,
                'account_id': str(target.get('account_id', '') or ''),
            }
            cancel_funcs = [
                getattr(gm_api, 'order_cancel', None),
                getattr(gm_api, 'cancel_order', None),
            ]
            for fn in cancel_funcs:
                if not callable(fn):
                    continue
                for arg in (cancel_payload, oid):
                    try:
                        fn(arg)
                        return True
                    except TypeError:
                        continue
                    except Exception:
                        continue

            self._runtime_log(f"[GmBroker] No compatible cancel API found for order {oid}.")
            return False
        except Exception as e:
            self._runtime_log(f"[GmBroker] cancel_pending_order failed ({oid}): {e}")
            return False

    # 实盘引擎调用此方法设置当前时间时，我们将其转换为无时区的北京时间
    # 这样 engine.py 中对比 df.index (无时区) 和 current_dt (无时区) 就不会报错了
    def set_datetime(self, dt):
        if dt is not None:
            # 1. 掘金传回来的是 python datetime，先转为 pandas Timestamp
            #    这样才能使用 .tz_convert 方法
            dt = pd.Timestamp(dt)

            if dt.tzinfo is not None:
                # 2. 先转为北京时间 (确保数值是 +8 区的)
                # 3. 再剥离时区 (变成 Naive，适配 Backtrader)
                dt = dt.tz_convert('Asia/Shanghai').tz_localize(None)

        super().set_datetime(dt)

    @staticmethod
    def is_live_mode(context) -> bool:
        """掘金平台实盘模式的具体判断逻辑"""
        if MODE_LIVE is None: return False
        return hasattr(context, 'mode') and context.mode == MODE_LIVE

    @staticmethod
    def extract_run_config(context) -> dict:
        """从掘金的context中提取回测参数，并转换为框架的标准配置格式"""
        if MODE_BACKTEST is not None and hasattr(context, 'mode') and context.mode == MODE_BACKTEST:
            print("[GmAdapter] Backtest mode detected. Extracting parameters from context.")
            config = {
                'start_date': context.backtest_start_time,
                'end_date': context.backtest_end_time,
                'cash': context.account().cash.available,
            }
            return config
        return {}

    # 1. 查钱
    def _init_cash(self):
        """Avoid an account query while a live GM session is being constructed.

        The live engine performs an explicit, bounded account-health probe after
        the SDK session is initialized.  Backtests keep the synchronous base
        initialization path.
        """
        if self.is_live_mode(self._context):
            return 0.0
        return super()._init_cash()

    def _fetch_real_cash(self):
        cash = self._fetch_cash_snapshot()
        available = cash.get('available') if isinstance(cash, dict) else getattr(cash, 'available', None)
        if available is None:
            raise RuntimeError("GM account cash snapshot has no available field")
        return available

    def is_account_snapshot_trusted(self) -> bool:
        """Validate that the live GM session exposes current account data.

        A connected SDK can still have no account snapshot during terminal
        recovery.  The engine must fail closed for that run instead of treating
        missing cash/positions as a legitimate zero account.
        """
        if not self.is_live:
            return True
        try:
            cash = self._fetch_cash_snapshot()
            available = (
                cash.get('available') if isinstance(cash, dict)
                else getattr(cash, 'available', None)
            )
            if available is None:
                raise RuntimeError('GM account cash snapshot has no available field')
            try:
                available = float(available)
            except (TypeError, ValueError, OverflowError) as e:
                raise RuntimeError('GM account available cash is not numeric') from e
            if not math.isfinite(available):
                raise RuntimeError('GM account available cash is not finite')
            # An empty list is a valid flat account; None means the snapshot was
            # not returned at all and must not be interpreted as empty holdings.
            self._query_live_positions()
            self._last_account_snapshot_fetch_failed = False
            self._last_account_snapshot_fetch_error = None
            return True
        except Exception as e:
            self._last_account_snapshot_fetch_failed = True
            self._last_account_snapshot_fetch_error = e
            return False

    # 2. 查持仓
    def get_position(self, data):
        class Pos:
            size = 0; price = 0.0; sellable = 0

        p = self._find_position(data._name)
        if p is not None:
            o = Pos()
            o.size = self._to_nonnegative_int(self._object_field(p, 'volume', 0), default=0)
            o.price = self._object_field(p, 'vwap', 0.0) or 0.0
            o.sellable = self._resolve_sellable_volume(p, o.size)
            return o
        return Pos()

    def get_sellable_position(self, data):
        p = self._find_position(data._name)
        if p is not None:
            total_size = self._to_nonnegative_int(self._object_field(p, 'volume', 0), default=0)
            return self._resolve_sellable_volume(p, total_size)
        return 0

    # 3. 查价
    def get_current_price(self, data):
        ticks = current(symbols=data._name)
        return ticks[0]['price'] if ticks else 0.0

    # 4. 发单
    def _submit_order(self, data, volume, side, price):
        gm_side = OrderSide_Buy if side == 'BUY' else OrderSide_Sell

        # === 核心分歧逻辑 ===
        # 回测 (Backtest): 使用 市价单 (Market)。
        #   尽可能和backtester回测结果对齐，券商回测的唯一作用是：测试代码有没有 Bug（会不会报错）。 至于它跑出来的收益率是 59% 还是 86%，已经不重要。
        #   理由: 掘金回测引擎的市价单能以 Open 价成交，还原真实低开红利。限价单在回测中可能以 Limit 价成交，导致低开时买贵。
        # 实盘 (Live): 使用 限价单 (Limit)。backtester的真实结果，如果gm_broker实盘用市价则第二天成交、大幅降低收益
        #   理由: 实盘中限价单能以最优价成交，且能避免市价单导致的巨额资金冻结。

        if self.is_live:
            # --- 实盘逻辑 (Limit) ---
            slippage = self._slippage_override if self._slippage_override is not None else self._DEFAULT_LIVE_SLIPPAGE
            if side == 'BUY':
                actual_price = price * (1 + slippage)
                actual_price = float(round(actual_price, 4))  # 保留精度
                freeze_price = actual_price  # 实盘按委托价冻结
            else:
                actual_price = price * (1 - slippage)
                actual_price = float(round(actual_price, 4))
            order_type = OrderType_Limit

        else:
            # --- 回测逻辑 (Market) ---
            # 即使是 Market 单，我们也需要预估冻结资金来做 Auto-Downsize
            # A股通常冻结涨停价，回测中我们保守估算 1.1 倍 (10%涨停) 作为冻结基准
            freeze_buffer = 1.1
            if side == 'BUY':
                freeze_price = price * freeze_buffer
            else:
                freeze_price = 0  # 卖出不查钱

            # 回测中市价单不需要指定 price (或者传0)，引擎按 Open 撮合
            actual_price = 0
            order_type = OrderType_Market

        # 2. 资金预检查与自动降级 (仅买入)
        if side == 'BUY':
            # 拆单批次已经持有一次性可用预算，避免首笔被柜台冻结后又重复扣减
            # 本地虚拟占资。普通单仍沿用实时现金 + 虚拟账本的保守口径。
            batch_cash_budget = getattr(self, '_buy_batch_cash_budget', None)
            if batch_cash_budget is None:
                available_cash = self._fetch_real_cash() - getattr(self, '_virtual_spent_cash', 0.0)
            else:
                available_cash = float(batch_cash_budget)
            if available_cash < 0:
                available_cash = 0.0

            # 实盘下 freeze_price 已含滑点，此处仅补手续费，避免重复计入滑点。
            if self.is_live:
                comm = self._commission_override if self._commission_override is not None else 0.0003
                cost_multiplier = 1.0 + comm
            else:
                cost_multiplier = self.safety_multiplier
            estimated_cost = volume * freeze_price * cost_multiplier

            if freeze_price <= 0:
                self._runtime_log(
                    f"[GmBroker Warning] 无法获取 {data._name} 的有效价格 (price={price})，拒绝计算并跳过发单。"
                )
                return None

            if estimated_cost > available_cash:
                old_volume = volume
                lot_size = normalize_quantity_step(getattr(config, 'LOT_SIZE', 100) or 1)
                volume = align_quantity_down(
                    available_cash / (freeze_price * cost_multiplier),
                    lot_size,
                )
                min_volume = lot_size
                if volume < min_volume:
                    self._runtime_log(
                        f"[GmBroker Warning] Buy {data._name} skipped. "
                        f"Cash ({available_cash:.2f}) insufficient for minimum lot {min_volume} "
                        f"after cash-fit downsize."
                    )
                    return None

                # 仅在发生实质性降仓时打印
                if old_volume != volume:
                    self._runtime_log(
                        f"[GmBroker] Auto-Downsize {data._name}: {old_volume} -> {volume} "
                        f"(Reason: Cash Fit, Mode: {'Live' if self.is_live else 'Backtest'})"
                    )

        if volume <= 0: return None

        try:
            self._ensure_single_live_account()
            effect = 1 if side == 'BUY' else 2

            ords = order_volume(
                symbol=data._name, volume=volume, side=gm_side,
                order_type=order_type,
                position_effect=effect,
                price=actual_price,
            )
            return GmOrderProxy(ords[-1], self.is_live, data=data) if ords else None
        except Exception as e:
            self._runtime_log(f"[GM Error] {e}")
            return None

    # 5. 将券商的原始订单对象（raw_order）转换为框架标准的 BaseOrderProxy
    def convert_order_proxy(self, raw_order) -> 'BaseOrderProxy':
        """
        掘金专用实现：找到对应的 DataFeed 并包装成 GmOrderProxy
        """
        target_symbol = raw_order.symbol
        matched_data = None

        # 在 Broker 内部查找 data 对象
        for d in self.datas:
            if d._name == target_symbol:
                matched_data = d
                break

        # 返回包装好的对象
        return GmOrderProxy(raw_order, self.is_live, data=matched_data)

    # --- 实现 BaseLiveBroker 的启动协议 ---
    @classmethod
    def launch(cls, conn_cfg: dict, strategy_path: str, params: dict, **kwargs):
        """
        实现掘金启动逻辑：手动注册回调，绕过 SDK 的 filename 加载机制
        """
        import time
        import traceback

        _runtime_print = runtime_print

        _runtime_print(f">>> Launching {cls.__name__} (Phoenix Mode) <<<")

        token = conn_cfg.get('token')
        serv_addr = conn_cfg.get('serv_addr')
        strategy_id = conn_cfg.get('strategy_id')
        schedule_rule = conn_cfg.get('schedule')

        # 提取选股器和标的
        selection_name = kwargs.get('selection')
        symbols = kwargs.get('symbols')
        timeframe = kwargs.get('timeframe')
        compression = kwargs.get('compression')
        risk_name = kwargs.get('risk')
        risk_params = kwargs.get('risk_params')
        parsed_schedule = None
        if schedule_rule:
            try:
                parsed_schedule = SchedulePlanner.parse_schedule_rule(schedule_rule)
            except ValueError as e:
                _runtime_print(f"[GmBroker Error] Invalid schedule config: {schedule_rule}. Error: {e}")
                raise
        try:
            prewarm_lead_seconds = SchedulePlanner.parse_schedule_prewarm_lead(
                getattr(config, 'LIVE_SCHEDULE_PREWARM_LEAD', 0)
            )
        except Exception as e:
            _runtime_print(f"[GmBroker Warning] Invalid LIVE_SCHEDULE_PREWARM_LEAD: {e}. Prewarm disabled.")
            prewarm_lead_seconds = 0.0
        prewarm_time_rule = None
        if schedule_rule and prewarm_lead_seconds > 0:
            if parsed_schedule is None:
                _runtime_print(
                    "[GmBroker Warning] Prewarm currently supports schedule format "
                    "1d|Nm|Nh:HH:MM[:SS]. Prewarm disabled."
                )
            elif prewarm_lead_seconds >= float(parsed_schedule.get('interval_seconds') or 0.0):
                _runtime_print(
                    f"[GmBroker Warning] LIVE_SCHEDULE_PREWARM_LEAD={prewarm_lead_seconds:.0f}s is not smaller than "
                    f"schedule interval {float(parsed_schedule.get('interval_seconds') or 0.0):.0f}s. "
                    "Prewarm disabled."
                )
            else:
                prewarm_time_rule = SchedulePlanner.build_schedule_prewarm_time_rule(
                    schedule_rule,
                    prewarm_lead_seconds,
                )
                if prewarm_time_rule:
                    prewarm_rule_type = f"{parsed_schedule['freq_n']}{parsed_schedule['freq_unit']}"
                    _runtime_print(
                        f"[GmBroker] Prewarm enabled: trigger {prewarm_lead_seconds:.0f}s before schedule "
                        f"({prewarm_rule_type} @ {prewarm_time_rule})"
                    )

        def _pick_probe_symbol(raw_symbols):
            if isinstance(raw_symbols, (list, tuple)):
                for s in raw_symbols:
                    if isinstance(s, str) and s.strip():
                        return s.strip()
            elif isinstance(raw_symbols, str) and raw_symbols.strip():
                return raw_symbols.strip()
            return None

        def _clip_backtest_end_by_history(dt_end_value):
            probe_symbol = _pick_probe_symbol(symbols)
            if not probe_symbol or history is None:
                return dt_end_value

            try:
                probe_start = (dt_end_value - pd.Timedelta(days=180)).strftime('%Y-%m-%d 00:00:00')
                probe_end = dt_end_value.strftime('%Y-%m-%d 23:59:59')
                probe_df = history(
                    symbol=probe_symbol,
                    frequency='1d',
                    start_time=probe_start,
                    end_time=probe_end,
                    fields='eob',
                    adjust=ADJUST_PREV,
                    df=True,
                )

                if probe_df is None or probe_df.empty:
                    return dt_end_value

                latest_eob = pd.Timestamp(probe_df['eob'].iloc[-1])
                if latest_eob.tzinfo is not None:
                    latest_eob = latest_eob.tz_convert('Asia/Shanghai').tz_localize(None)
                latest_close = latest_eob.to_pydatetime().replace(hour=16, minute=0, second=0, microsecond=0)

                if dt_end_value > latest_close:
                    _runtime_print(
                        "[GmBroker] Backtest end clipped to latest GM history: "
                        f"{latest_close.strftime('%Y-%m-%d 16:00:00')} (requested: {dt_end_value.strftime('%Y-%m-%d 16:00:00')})"
                    )
                    return latest_close
            except Exception as e:
                _runtime_print(f"[GmBroker Warning] Failed to probe latest GM history date: {e}")

            return dt_end_value

        # --- 1. 处理回测参数与模式判断 ---
        start_date = kwargs.get('start_date')
        end_date = kwargs.get('end_date')
        mode = MODE_LIVE
        gm_start_time = ''
        gm_end_time = ''
        dt_start = None
        dt_end = None

        if start_date:
            mode = MODE_BACKTEST
            print(f"  Mode: BACKTEST")
            try:
                dt_start = pd.to_datetime(str(start_date)).to_pydatetime()
                if end_date:
                    dt_end = pd.to_datetime(str(end_date)).to_pydatetime()
                else:
                    dt_end = datetime.datetime.now()
                dt_end = dt_end.replace(hour=16, minute=0, second=0, microsecond=0)
            except Exception as e:
                print(f"[Error] Date format error: {e}")
                return
        else:
            print(f"  Mode: LIVE")

        # 资金与费率
        initial_cash = float(kwargs.get('cash')) if kwargs.get('cash') is not None else 100000.0
        commission = float(kwargs.get('commission')) if kwargs.get('commission') is not None else 0.0003
        slippage = float(kwargs.get('slippage')) if kwargs.get('slippage') is not None else 0.0001

        def _apply_gm_connection_config():
            """
            Re-apply GM global connection settings before every SDK init attempt.

            GM SDK keeps part of its connection state process-wide. If the first
            init happens while the terminal/broker is unavailable, re-binding the
            token/server on each Phoenix session keeps later retries equivalent
            to a fresh process start.
            """
            if serv_addr and set_serv_addr:
                set_serv_addr(serv_addr)
            if token and set_token:
                set_token(token)

        def _soft_reset_gm_sdk(reason, status=None, log_result=True):
            """
            Best-effort cleanup for GM SDK process-global state after init/poll
            failure. Function names differ across gm versions, so this probes
            both module globals and gm.csdk.c_sdk dynamically.
            """
            reset_names = (
                'gmi_close', 'gmi_stop', 'gmi_uninit', 'gmi_exit',
                'py_gmi_close', 'py_gmi_stop', 'py_gmi_uninit', 'py_gmi_exit',
            )
            sources = [globals()]
            try:
                import gm.csdk.c_sdk as c_sdk
                sources.append(vars(c_sdk))
            except Exception:
                pass

            called = []
            for source in sources:
                for name in reset_names:
                    fn = source.get(name) if isinstance(source, dict) else None
                    if not callable(fn) or name in called:
                        continue
                    try:
                        fn()
                        called.append(name)
                    except TypeError:
                        # Some versions may expose functions with required args;
                        # those are not safe to guess here.
                        continue
                    except SystemExit:
                        raise
                    except Exception as e:
                        _runtime_print(f"[GmBroker Warning] GM SDK reset hook {name} failed: {e}")

            try:
                setattr(context, '_quantada_gm_shutdown_requested', False)
            except Exception:
                pass

            if log_result:
                status_text = f" status={status}" if status is not None else ""
                hook_text = ", ".join(called) if called else "none"
                _runtime_print(
                    f"[Phoenix] GM SDK soft reset after {reason}.{status_text} hooks={hook_text}"
                )

        def _hard_reexec_current_process(reason):
            """
            Last-resort self-healing: replace the current Python process with a
            fresh one. This matches the manual kill/re-run recovery path while
            preserving nohup file descriptors.
            """
            _runtime_print(f"[Phoenix] {reason}. Re-exec current Python process to clear GM SDK state...")

            if is_live_worker_process():
                request_live_worker_restart(
                    reason,
                    failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
                )

            argv = [sys.executable, '-u'] + list(sys.argv)
            try:
                os.execv(sys.executable, argv)
            except Exception as e:
                _runtime_print(f"[Phoenix] GM self re-exec failed: {e}. Continue Phoenix retry loop.")

        # 设置全局配置
        _apply_gm_connection_config()

        if mode == MODE_BACKTEST:
            dt_end = _clip_backtest_end_by_history(dt_end)
            if dt_end <= dt_start:
                print("[Error] Invalid backtest period: start_date must be earlier than end_date.")
                print(
                    f"        start_date={dt_start.strftime('%Y-%m-%d 08:00:00')}, "
                    f"end_date={dt_end.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                return
            gm_start_time = dt_start.strftime('%Y-%m-%d 08:00:00')
            gm_end_time = dt_end.strftime('%Y-%m-%d %H:%M:%S')

        launch_state = {
            'start_alarm_sent': bool(mode == MODE_LIVE and is_live_worker_restart()),
            'consecutive_init_failures': 0,
            'connectivity_log': None,
            'connectivity_failure_seen': False,
        }

        effective_prewarm_lead_seconds = (
            prewarm_lead_seconds if prewarm_time_rule else 0.0
        )
        connectivity_active_after_seconds = resolve_live_run_budget_seconds({
            'schedule_rule': schedule_rule,
            'timeframe': timeframe,
            'compression': compression,
        })

        def _connectivity_retry_plan(now=None):
            return _resolve_gm_connectivity_retry(
                now=now or datetime.datetime.now(),
                parsed_schedule=parsed_schedule,
                prewarm_lead_seconds=effective_prewarm_lead_seconds,
                active_after_seconds=connectivity_active_after_seconds,
            )

        def _log_connectivity_failure(kind, message, quiet):
            now_monotonic = time.monotonic()
            phase = 'quiet' if quiet else 'active'
            key = (str(kind), str(message), phase)
            previous = launch_state.get('connectivity_log')
            if quiet:
                # Keep only a short-lived suppression record while maintenance
                # probing continues.  The first visible recovery log is emitted
                # when the schedule enters the active window or the session recovers.
                if previous and previous.get('key') == key:
                    previous['suppressed'] = int(previous.get('suppressed', 0) or 0) + 1
                    previous['last_at'] = now_monotonic
                    launch_state['connectivity_failure_seen'] = True
                    return False
                launch_state['connectivity_log'] = {
                    'key': key,
                    'last_at': now_monotonic,
                    'suppressed': 0,
                }
                launch_state['connectivity_failure_seen'] = True
                return False

            if previous and previous.get('key') == key:
                elapsed = now_monotonic - float(previous.get('last_at') or 0.0)
                if elapsed < _GM_CONNECTIVITY_LOG_INTERVAL_SECONDS:
                    previous['suppressed'] = int(previous.get('suppressed', 0) or 0) + 1
                    launch_state['connectivity_failure_seen'] = True
                    return False

            suppressed = int(previous.get('suppressed', 0) or 0) if previous else 0
            suffix = f" (suppressed {suppressed} repeated reports)" if suppressed else ""
            _runtime_print(f"{message}{suffix}")
            launch_state['connectivity_log'] = {
                'key': key,
                'last_at': now_monotonic,
                'suppressed': 0,
            }
            launch_state['connectivity_failure_seen'] = True
            return True

        def _log_connectivity_recovered(detail):
            if not launch_state.get('connectivity_failure_seen'):
                return
            previous = launch_state.get('connectivity_log') or {}
            suppressed = int(previous.get('suppressed', 0) or 0)
            suffix = f"; suppressed {suppressed} repeated reports" if suppressed else ""
            _runtime_print(f"[Phoenix] GM connectivity recovered: {detail}{suffix}.")
            launch_state['connectivity_log'] = None
            launch_state['connectivity_failure_seen'] = False

        # --- 2. 核心运行逻辑 ---
        def run_session():
            launch_state['retry_quiet'] = False
            launch_state['retry_delay_seconds'] = _GM_AGGRESSIVE_RETRY_SECONDS
            launch_state['retry_wake_at'] = None
            session_state = {'shutdown_requested': False}
            last_market_data_error_log_at = None
            last_poll_status_log_at = None
            market_data_error_log_interval_seconds = 600.0

            def _log_temporary_market_data_error(code, info):
                nonlocal last_market_data_error_log_at
                info_text = str(info or "").strip()
                try:
                    code_int = int(code)
                except Exception:
                    code_int = next(
                        (candidate for candidate in (1200, 1201) if str(candidate) in info_text),
                        None,
                    )
                is_market_data_failure = (
                    "实时行情服务连接失败" in info_text
                    or "行情服务连接失败" in info_text
                    or "实时行情服务连接断开" in info_text
                    or "行情服务连接断开" in info_text
                )
                if code_int not in {1200, 1201} or (info_text and not is_market_data_failure):
                    return False

                quiet, _, _ = _connectivity_retry_plan(datetime.datetime.now())
                if quiet:
                    return True

                now_ts = time.time()
                if (
                    last_market_data_error_log_at is None
                    or now_ts - last_market_data_error_log_at >= market_data_error_log_interval_seconds
                ):
                    info_text = info_text or "实时行情服务连接失败"
                    _runtime_print(f"[GM Warning] Code: {code}, Msg: {info_text}")
                    last_market_data_error_log_at = now_ts
                return True

            # 每次启动前重置 context 身份
            _apply_gm_connection_config()
            py_gmi_set_strategy_id(strategy_id)
            gmi_set_mode(mode)
            context.mode = mode
            context.strategy_id = strategy_id


            def init(ctx):
                if session_state.get('init_completed') and getattr(ctx, 'strategy_instance', None) is not None:
                    _runtime_print("[GmBroker] Duplicate init callback ignored for current GM session.")
                    return

                if mode == MODE_LIVE:
                    report_live_worker_state(
                        "gm_sdk_running",
                        detail="GM init callback received",
                    )

                _runtime_print(f"[Phoenix] Initializing Strategy '{strategy_path}'...")
                engine_config = config.__dict__.copy()
                engine_config['strategy_name'] = strategy_path
                engine_config['params'] = params
                engine_config['platform'] = 'gm'
                engine_config['_suppress_start_alarm'] = launch_state.get('start_alarm_sent', False)
                if schedule_rule:
                    engine_config['schedule_rule'] = schedule_rule
                ctx.use_schedule = False

                # 将资金和费率头传到 LiveTrader 引擎
                if kwargs.get('cash') is not None: engine_config['cash'] = initial_cash
                if kwargs.get('commission') is not None: engine_config['commission'] = commission
                if kwargs.get('slippage') is not None: engine_config['slippage'] = slippage
                if timeframe is not None: engine_config['timeframe'] = timeframe
                if compression is not None: engine_config['compression'] = compression
                if kwargs.get('data_source'): engine_config['data_source'] = kwargs.get('data_source')

                # 注入选股器或标的
                if selection_name:
                    engine_config['selection_name'] = selection_name
                if symbols:
                    engine_config['symbols'] = symbols
                if risk_name:
                    engine_config['risk'] = risk_name
                if risk_params is not None:
                    engine_config['risk_params'] = risk_params

                if mode == MODE_BACKTEST:
                    engine_config['start_date'] = start_date

                # 实例化引擎
                trader = LiveTrader(engine_config)
                trader.init(ctx)
                launch_state['start_alarm_sent'] = True
                ctx.strategy_instance = trader
                session_state['init_completed'] = True

                # 订阅行情
                current_symbols = [d._name for d in ctx.strategy_instance.broker.datas]
                if not current_symbols:
                    current_symbols = ctx.strategy_instance._determine_symbols()
                if current_symbols:
                    sub_tf = ctx.strategy_instance.config.get('timeframe', 'Days')
                    sub_cp = int(ctx.strategy_instance.config.get('compression', 1) or 1)
                    if sub_tf == 'Minutes':
                        sub_freq = f"{sub_cp * 60}s"
                    elif sub_tf == 'Seconds':
                        sub_freq = f"{sub_cp}s"
                    else:
                        sub_freq = '1d'
                    _runtime_print(f"[GmBroker] Subscribing to {len(current_symbols)} symbols...")
                    try:
                        subscribe(symbols=current_symbols, frequency=sub_freq, count=1, wait_group=True)
                    except Exception as e:
                        err_code = getattr(e, 'code', None)
                        if _log_temporary_market_data_error(err_code, str(e)):
                            _runtime_print(
                                "[GmBroker Warning] Realtime market subscription unavailable; "
                                "schedule registration will continue."
                            )
                        else:
                            raise

                # 实盘定时任务配置
                if mode == MODE_LIVE and schedule_rule:
                    try:
                        from gm.api import schedule
                        if parsed_schedule:
                            try:
                                SchedulePlanner.print_schedule_preview(
                                    now=datetime.datetime.now(),
                                    parsed_schedule=parsed_schedule,
                                    prewarm_lead_seconds=prewarm_lead_seconds,
                                    tz_info="Server Local Time",
                                    count=3,
                                    prefix="[GmBroker]",
                                )
                            except Exception as e:
                                _runtime_print(f"[GmBroker Warning] Failed to compute schedule preview: {e}")
                        # 解析格式 "1d:14:50:00" -> freq="1d", time="14:50:00"
                        if ':' in schedule_rule:
                            rule_type, rule_time = schedule_rule.split(':', 1)
                            _runtime_print(f"[GmBroker] Schedule enabled (from config): {rule_type} @ {rule_time}")
                            _runtime_print("            策略将在指定时间主动运行，忽略 on_bar 事件。")

                            if prewarm_time_rule:
                                def _run_prewarm(schedule_ctx):
                                    if parsed_schedule:
                                        now_value = getattr(schedule_ctx, 'now', None) or datetime.datetime.now()
                                        try:
                                            should_prewarm, seconds_to_schedule, prewarm_slot_key = (
                                                SchedulePlanner.should_trigger_schedule_prewarm_for_rule(
                                                    now=pd.Timestamp(now_value),
                                                    parsed_schedule=parsed_schedule,
                                                    lead_seconds=prewarm_lead_seconds,
                                                    last_prewarm_run_key=launch_state.get('last_prewarm_run_key'),
                                                    last_schedule_run_key=launch_state.get('last_schedule_run_key'),
                                                )
                                            )
                                        except Exception as e:
                                            should_prewarm = True
                                            seconds_to_schedule = None
                                            prewarm_slot_key = None
                                            _runtime_print(f"[GmBroker Warning] Failed to evaluate prewarm slot: {e}")

                                        if not should_prewarm:
                                            skip_key = prewarm_slot_key or 'unknown'
                                            if launch_state.get('last_prewarm_skip_log_key') != skip_key:
                                                delta_text = (
                                                    f"{float(seconds_to_schedule):.1f}s"
                                                    if seconds_to_schedule is not None else "N/A"
                                                )
                                                _runtime_print(
                                                    "[GmBroker] Duplicate/early prewarm callback ignored "
                                                    f"for slot {skip_key}. seconds_to_schedule={delta_text}"
                                                )
                                                launch_state['last_prewarm_skip_log_key'] = skip_key
                                            return

                                    strategy = getattr(schedule_ctx, 'strategy_instance', None)
                                    if strategy is None:
                                        skip_key = prewarm_slot_key or 'unknown'
                                        if launch_state.get('last_prewarm_unavailable_log_key') != skip_key:
                                            _runtime_print("[GmBroker Warning] Prewarm skipped: strategy instance unavailable.")
                                            launch_state['last_prewarm_unavailable_log_key'] = skip_key
                                        return
                                    prewarm_symbols = [d._name for d in getattr(strategy.broker, 'datas', [])]
                                    if not prewarm_symbols:
                                        prewarm_symbols = current_symbols or symbols
                                    summary = strategy.broker.run_schedule_prewarm(
                                        schedule_rule=schedule_rule,
                                        data_provider=strategy.data_provider,
                                        symbols=prewarm_symbols,
                                        timeframe=strategy.config.get('timeframe', 'Days'),
                                        compression=strategy.config.get('compression', 1),
                                        now=getattr(schedule_ctx, 'now', None),
                                    )
                                    if prewarm_slot_key:
                                        launch_state['last_prewarm_run_key'] = prewarm_slot_key
                                    _runtime_print(
                                        "[GmBroker] Prewarm Finished. "
                                        f"source={summary.get('source')}, "
                                        f"symbol={summary.get('symbol')}, "
                                        f"extras={summary.get('extras')}, "
                                        f"errors={summary.get('errors')}"
                                    )

                                schedule(schedule_func=_run_prewarm, date_rule=rule_type, time_rule=prewarm_time_rule)

                            def _run_scheduled(schedule_ctx):
                                if parsed_schedule:
                                    now_value = getattr(schedule_ctx, 'now', None) or datetime.datetime.now()
                                    now_ts = pd.Timestamp(now_value)
                                    slot_dt = SchedulePlanner.resolve_current_schedule_slot(now_ts, parsed_schedule)
                                    if slot_dt is not None:
                                        slot_ts = pd.Timestamp(slot_dt)
                                        delta = (now_ts - slot_ts).total_seconds()
                                        slot_key = SchedulePlanner.format_schedule_slot_key(slot_ts)
                                        if delta < 0:
                                            return
                                        if launch_state.get('last_schedule_run_key') == slot_key:
                                            if launch_state.get('last_schedule_skip_log_key') != slot_key:
                                                _runtime_print(f"[GmBroker] Duplicate schedule callback ignored for slot {slot_key}.")
                                                launch_state['last_schedule_skip_log_key'] = slot_key
                                            return
                                        launch_state['last_schedule_run_key'] = slot_key

                                trader.run(schedule_ctx)

                            schedule(schedule_func=_run_scheduled, date_rule=rule_type, time_rule=rule_time)
                            ctx.use_schedule = True
                        else:
                            _runtime_print(f"[GmBroker Warning] 定时配置格式错误 (应为 freq:time): {schedule_rule}")

                    except Exception as e:
                        _runtime_print(f"[GmBroker Error] 定时任务注册失败: {e}")

            def on_bar(ctx, bars):
                if getattr(ctx, 'use_schedule', False):
                    return
                if hasattr(ctx, 'strategy_instance'):
                    ctx.strategy_instance.run(ctx)

            def on_order_status(ctx, order):
                on_order_status_callback(ctx, order)

            def on_error(ctx, code, info):
                msg = f"Code: {code}, Msg: {info}"
                if _log_temporary_market_data_error(code, info):
                    return

                try:
                    code_int = int(code)
                except (TypeError, ValueError):
                    code_int = None
                if mode == MODE_LIVE and code_int == 1100:
                    now_value = datetime.datetime.now()
                    quiet, _, wake_at = _connectivity_retry_plan(now_value)
                    if quiet and wake_at is not None:
                        seconds_to_wake = max(
                            0.1,
                            (pd.Timestamp(wake_at) - pd.Timestamp(now_value)).total_seconds(),
                        )
                        report_live_worker_state(
                            "gm_connectivity_quiet_wait",
                            unhealthy_after_seconds=seconds_to_wake,
                            detail=(
                                f"GM trade service unavailable; aggressive recovery at "
                                f"{pd.Timestamp(wake_at).strftime('%Y-%m-%d %H:%M:%S')}"
                            ),
                            failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
                            refresh_deadline=True,
                        )
                    else:
                        report_live_worker_state(
                            "gm_trade_service_unavailable",
                            unhealthy_after_seconds=_GM_SDK_HEALTH_DEADLINE_SECONDS,
                            detail=msg,
                            failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
                        )
                    if _log_connectivity_failure(
                        "gm_trade_service_unavailable",
                        f"[GM Warning] {msg}",
                        quiet,
                    ) and not quiet:
                        AlarmManager().push_schedule_api_unavailable("GmBroker", msg)
                    return

                _runtime_print(f"[GM Error] {msg}")

                # 【报警接入】异常推送
                # 过滤掉一些非致命的错误码 (视情况而定)
                AlarmManager().push_exception("GM Kernel Error", msg)

            def on_shutdown(ctx):
                session_state['shutdown_requested'] = True
                try:
                    setattr(ctx, '_quantada_gm_shutdown_requested', True)
                except Exception:
                    pass
                _runtime_print("[System] Strategy Shutdown")

                # 【报警接入】停止推送
                if mode == MODE_LIVE and not is_live_worker_process():
                    AlarmManager().push_status("INFO", "GM Session Shutdown (Preparing to Restart)")

            def on_backtest_finished(ctx, indicator):
                print("\n" + "=" * 50)
                print("[System] Backtest Finished Report")
                print("=" * 50)

                # 直接展示原生指标，不画蛇添足
                pnl_ratio = indicator.get('pnl_ratio', 0)
                pnl_ratio_annual = indicator.get('pnl_ratio_annual', 0)
                sharpe_ratio = indicator.get('sharpe_ratio', 0)
                max_drawdown = indicator.get('max_drawdown', 0)
                win_ratio = indicator.get('win_ratio', 0)
                open_count = indicator.get('open_count', 0)

                print(f"  Total Return:                 {pnl_ratio:>.2%}")
                print(f"  Annual Return:                {pnl_ratio_annual:>.2%}")
                print(f"  Max Drawdown:                 {max_drawdown:>.2%}")
                print(f"  Win Rate:                     {win_ratio:>.2%}")
                print(f"  Trade Count:                  {int(open_count)}")

                print("-" * 50)
                print("  注意: 详细的回测报告（包含资金曲线、Alpha等）请登录掘金终端后查看。")
                print("=" * 50 + "\n")

            # --- 3. 绑定回调 ---
            context.init_fun = init
            context.on_bar_fun = on_bar
            context.on_order_status_fun = on_order_status
            context.on_error_fun = on_error
            context.on_shutdown_fun = on_shutdown
            context.on_backtest_finished_fun = on_backtest_finished

            py_gmi_set_data_callback(callback_controller)

            # --- 4. 启动运行 ---
            if mode == MODE_BACKTEST:
                print(f"  Period: {gm_start_time} -> {gm_end_time}")
                print(f"  Cash: {initial_cash}")

                py_gmi_set_backtest_config(
                    start_time=gm_start_time,
                    end_time=gm_end_time,
                    initial_cash=initial_cash,
                    transaction_ratio=1,
                    commission_ratio=commission,
                    commission_unit=0,
                    slippage_ratio=slippage,
                    option_float_margin_ratio1=0.2,  # 补全参数防止报错
                    option_float_margin_ratio2=0.4,
                    adjust=ADJUST_PREV,
                    check_cache=1,
                    match_mode=0
                )
                status = py_gmi_run()
                try:
                    check_gm_status(status)
                except Exception as e:
                    msg = str(e)
                    if status == 1027 and "开始时间要在结束时间之前" in msg:
                        print("[GmBroker Warning] Backtest reached GM data boundary. Exiting gracefully.")
                        return False
                    raise
                return False

            else:  # 实盘模式
                _runtime_print("  Status: Connecting to GM terminal...")
                report_live_worker_state(
                    "gm_sdk_initializing",
                    unhealthy_after_seconds=_GM_SDK_HEALTH_DEADLINE_SECONDS,
                    detail="gmi_init has not completed",
                    failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
                )
                status = gmi_init()
                if status != 0:
                    launch_state['consecutive_init_failures'] = int(
                        launch_state.get('consecutive_init_failures', 0) or 0
                    ) + 1
                    now_value = datetime.datetime.now()
                    quiet, retry_delay, wake_at = _connectivity_retry_plan(now_value)
                    launch_state['retry_quiet'] = quiet
                    launch_state['retry_delay_seconds'] = retry_delay
                    launch_state['retry_wake_at'] = wake_at
                    if quiet and wake_at is not None:
                        seconds_to_wake = max(
                            0.1,
                            (pd.Timestamp(wake_at) - pd.Timestamp(now_value)).total_seconds(),
                        )
                        retry_detail = (
                            f"quiet probe in {retry_delay:.0f}s; aggressive recovery at "
                            f"{pd.Timestamp(wake_at).strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        report_live_worker_state(
                            "gm_connectivity_quiet_wait",
                            unhealthy_after_seconds=seconds_to_wake,
                            detail=retry_detail,
                            failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
                            refresh_deadline=True,
                        )
                    else:
                        retry_detail = f"retrying in {retry_delay:.0f}s"
                    message = f"[Phoenix] Init failed (Code: {status}); {retry_detail}."
                    emitted = _log_connectivity_failure(
                        "gm_init_failed",
                        message,
                        quiet,
                    )
                    if emitted:
                        AlarmManager().push_schedule_api_unavailable(
                            "GmBroker",
                            f"GM terminal init failed (Code: {status})",
                        )
                    _soft_reset_gm_sdk(
                        "init failure",
                        status=status,
                        log_result=emitted,
                    )
                    return True  # 初始化失败，要求重试

                check_gm_status(status)
                launch_state['consecutive_init_failures'] = 0
                _log_connectivity_recovered("gmi_init succeeded")
                report_live_worker_state(
                    "gm_sdk_running",
                    detail="gmi_init completed",
                )

                _runtime_print("[Phoenix] Entering Event Loop (Ctrl+C to stop)...")

                try:
                    # GM's official run() loop intentionally ignores gmi_poll's
                    # return value. Session lifecycle is reported by callbacks.
                    while True:
                        try:
                            poll_status = gmi_poll()
                        except SystemExit as e:
                            restart_reason = (
                                "shutdown callback"
                                if session_state.get('shutdown_requested') or getattr(
                                    context, '_quantada_gm_shutdown_requested', False
                                )
                                else "unmarked SDK SystemExit"
                            )
                            _runtime_print(
                                f"[Phoenix] GM SDK requested process exit ({e}; {restart_reason}). "
                                "Restarting session instead..."
                            )
                            if session_state.get('shutdown_requested') or getattr(
                                context, '_quantada_gm_shutdown_requested', False
                            ):
                                try:
                                    setattr(context, '_quantada_gm_shutdown_requested', False)
                                except Exception:
                                    pass
                            else:
                                try:
                                    AlarmManager().push_exception(
                                        "GM SDK SystemExit",
                                        f"gmi_poll raised SystemExit({e}) without shutdown callback. Restarting session.",
                                    )
                                except Exception:
                                    pass
                            return True

                        if session_state.get('shutdown_requested') or getattr(
                            context, '_quantada_gm_shutdown_requested', False
                        ):
                            _runtime_print("[Phoenix] GM shutdown callback received. Restarting session...")
                            try:
                                setattr(context, '_quantada_gm_shutdown_requested', False)
                            except Exception:
                                pass
                            return True

                        if poll_status not in (None, 0):
                            if _log_temporary_market_data_error(poll_status, ""):
                                time.sleep(1)
                                continue

                            quiet, _, _ = _connectivity_retry_plan(datetime.datetime.now())
                            if not quiet:
                                now_ts = time.time()
                                if (
                                    last_poll_status_log_at is None
                                    or now_ts - last_poll_status_log_at
                                    >= market_data_error_log_interval_seconds
                                ):
                                    _runtime_print(
                                        f"[GM Warning] gmi_poll returned transient status {poll_status}; "
                                        "continuing the SDK event loop."
                                    )
                                    last_poll_status_log_at = now_ts
                            # Avoid a CPU spin if a particular SDK build returns
                            # immediately while idle. Explicit shutdown/SystemExit
                            # and bounded connection-health callbacks still restart.
                            time.sleep(0.05)
                            continue

                        # 稍微休眠，释放 CPU，同时检测外部中断
                        time.sleep(1)

                except Exception as e:
                    _runtime_print(f"[Phoenix] Event Loop Crashed: {e}")
                    raise e  # 抛出异常给外层处理


        # --- 3. 守护进程主循环 (The Phoenix Loop) ---
        # 只要不是回测或手动停止，这里会永远运行
        while True:
            try:
                should_retry = run_session()
                if not should_retry:
                    _runtime_print(">>> GM Broker Exited Normally.")
                    break  # 回测结束或正常退出

                init_failures = int(launch_state.get('consecutive_init_failures', 0) or 0)
                if mode == MODE_LIVE and init_failures >= 12:
                    _hard_reexec_current_process(
                        f"GM init failed {init_failures} consecutive times; SDK may be stuck after terminal startup"
                    )
                    launch_state['consecutive_init_failures'] = 0

                quiet_retry = bool(launch_state.get('retry_quiet'))
                if mode == MODE_LIVE and is_live_worker_process() and not quiet_retry:
                    request_live_worker_restart(
                        "GM SDK session ended; supervisor will start a clean worker process",
                        failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
                    )

                # 如果 run_session 返回 True，说明是异常退出或断线，需要冷却后重启
                retry_delay = float(
                    launch_state.get('retry_delay_seconds') or _GM_AGGRESSIVE_RETRY_SECONDS
                )
                if not quiet_retry:
                    _runtime_print(f"[Phoenix] Waiting {retry_delay:.0f}s before restart...")
                time.sleep(retry_delay)
                if not quiet_retry:
                    _runtime_print("[Phoenix] Restarting now...")

            except KeyboardInterrupt:
                _runtime_print("[Stop] User interrupted (Ctrl+C). Exiting Phoenix Loop.")
                if mode == MODE_LIVE and not is_live_worker_process():
                    AlarmManager().push_status("STOPPED", "User Manually Stopped")
                break

            except Exception as e:
                _runtime_print(f"[CRITICAL] Unexpected Crash: {e}")
                traceback.print_exc()

                # 严重错误推送
                if mode == MODE_LIVE:
                    try:
                        AlarmManager().push_exception("Phoenix Crash", str(e))
                    except Exception:
                        pass

                _runtime_print("[Phoenix] Critical error. Restarting in 15s...")
                time.sleep(15)
                continue
