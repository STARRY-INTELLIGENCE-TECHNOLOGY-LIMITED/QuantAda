import math
import threading
from abc import ABC, abstractmethod
from decimal import Decimal

import pandas as pd

import config
from common import log, runtime_notifications
from common.live_execution_budget import (
    get_live_run_deadline,
    live_run_budget_expired,
)
from common.live_runtime import runtime_print
from common.order_quantity import (
    align_quantity_down,
    decimal_quantity,
    format_quantity,
    normalize_quantity_step,
    positive_quantity,
    quantity_chunk_plan,
    quantity_number,
    subtract_quantities,
    sum_quantities,
)

from ..data_bridge.data_warm import BrokerDataWarmBridge


class BaseOrderProxy(ABC):
    """
    订单代理的抽象基类。
    所有平台的具体订单代理都必须实现这些与 backtrader 兼容的方法。
    """

    @property
    @abstractmethod
    def id(self): pass
    @abstractmethod
    def is_completed(self) -> bool: pass

    @abstractmethod
    def is_canceled(self) -> bool: pass

    @abstractmethod
    def is_rejected(self) -> bool: pass

    @abstractmethod
    def is_pending(self) -> bool: pass

    @abstractmethod
    def is_accepted(self) -> bool: pass

    @abstractmethod
    def is_buy(self) -> bool: pass

    @abstractmethod
    def is_sell(self) -> bool: pass


class BaseLiveDataProvider(ABC):
    """数据提供者适配器的抽象基类"""

    @abstractmethod
    def get_history(self, symbol: str, start_date: str, end_date: str,
                    timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        """获取指定标的的历史日线数据"""
        pass


class BaseLiveBroker(ABC):
    """交易执行器适配器的抽象基类，模拟 backtrader 的 broker 接口"""

    _BUY_LOT_STEP_RETRIES = 5
    _BUY_GEOMETRIC_RETRIES = 5

    def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None,):
        self.is_live = True
        self._context = context
        self.datas = []
        self._datetime = None
        self._keep_overnight_orders = bool(getattr(config, 'KEEP_OVERNIGHT_ORDERS', False))
        self._cash_override = cash_override
        self._commission_override = commission_override
        self._slippage_override = slippage_override
        # 内部状态机
        self._cash = self._init_cash()
        self._pending_sells = set()
        self._last_pending_orders_fetch_failed = False
        self._last_pending_orders_fetch_error = None
        # 虚拟账本，类似backtester能快速回笼资金
        self._virtual_spent_cash = 0.0
        # 活跃买单追踪器，用于被拒单时的降级重试
        self._active_buys = {}
        # 虚拟账本读写锁
        self._ledger_lock = threading.RLock()
        # 风控锁定黑名单
        self._risk_locked_symbols = set()
        # 预热属于组合能力，不进入 broker 继承语义。
        self._data_warm = BrokerDataWarmBridge(self)

    @property
    def safety_multiplier(self):
        """
        买入资金成本估算倍率：1.0 + 委托滑点 + 手续费率。
        """
        comm = self._commission_override if self._commission_override is not None else 0.0003
        slip = self._slippage_override if self._slippage_override is not None else 0.001
        return 1.0 + slip + comm

    def log(self, txt, dt=None):
        """
        兼容 Backtrader 的日志接口。
        供策略层调用 (self.broker.log)。
        在实盘模式下，如果没有传入时间，log.info 会自动使用当前系统时间。
        """
        # 回测沿用模拟时间；实盘未提供柜台时间时由 log.info 使用当前墙钟时间。
        if dt is None and not self.is_live:
            dt = getattr(self, '_datetime', None)

        log.info(txt, dt=dt)

    def _runtime_log(self, message):
        """Timestamp live execution diagnostics without changing backtest time semantics."""
        if self.is_live:
            runtime_print(message)
        else:
            print(message)

    def _runtime_setting(self, name, default=None):
        """读取本轮配置快照；没有快照时回退到 ``config``。

        实盘启动器会把命令行生效配置复制到引擎。数量决策始终优先使用
        该快照，可以避免长进程重连或模块级配置变更静默丢失
        ``LOT_SIZE``/``BROKER_LOT_LIMITS``。
        """
        runtime_config = getattr(self, '_runtime_config', None)
        if isinstance(runtime_config, dict) and name in runtime_config:
            return runtime_config[name]
        return getattr(config, name, default)

    # =========================================================
    #  用户只需实现下述原子接口 (The Minimum Set)
    # =========================================================
    @abstractmethod
    def getvalue(self):
        """
        兼容 Backtrader 接口: 获取当前账户总权益 (Net Liquidation Value)
        默认实现: 现金 + 所有持仓的市值
        """
        return self._get_portfolio_nav()

    @abstractmethod
    def _fetch_real_cash(self) -> float:
        """子类必须实现，用于获取真实账户的可用资金"""
        pass

    @abstractmethod
    def get_position(self, data):
        """子类必须实现，用于获取指定标的的持仓"""
        pass

    def get_sellable_position(self, data):
        """
        获取当前可卖仓位。
        默认实现回退到 get_position().size，适配不区分可卖/总仓位的平台。
        """
        pos = self.get_position(data)
        return positive_quantity(getattr(pos, 'size', 0) or 0)

    @abstractmethod
    def get_current_price(self, data) -> float:
        """子类必须实现，用于获取指定标的实时价格"""
        pass

    @abstractmethod
    def get_pending_orders(self) -> list:
        """
        [实盘防爆仓] 子类必须实现。获取所有未完成的在途订单。
        返回统一格式: [{'id': '123', 'symbol': 'SHSE.510300', 'direction': 'BUY', 'size': 1000}, ...]
        """
        pass

    def prewarm_additional_connections(self, now=None):
        """
        预热附加连接钩子。
        默认无操作，子类可按需预热行情/汇率等轻量连接。
        """
        return []

    def alarm_schedule_prewarm_issue_once(self, schedule_rule, now=None, slot_key=None, summary=None,
                                          error=None, level='ERROR') -> bool:
        return self._data_warm.alarm_schedule_prewarm_issue_once(
            schedule_rule=schedule_rule,
            now=now,
            slot_key=slot_key,
            summary=summary,
            error=error,
            level=level,
        )

    def run_schedule_prewarm(self, schedule_rule, data_provider=None, symbols=None,
                             timeframe='Days', compression=1, now=None) -> dict:
        return self._data_warm.run_schedule_prewarm(
            schedule_rule=schedule_rule,
            data_provider=data_provider,
            symbols=symbols,
            timeframe=timeframe,
            compression=compression,
            now=now,
        )

    def prewarm_before_schedule(self, data_provider=None, symbols=None,
                                timeframe='Days', compression=1, now=None) -> dict:
        return self._data_warm.prewarm_before_schedule(
            data_provider=data_provider,
            symbols=symbols,
            timeframe=timeframe,
            compression=compression,
            now=now,
        )

    # --- 隔日委托清理协议（默认无操作，子类按需覆盖） ---
    def cancel_pending_order(self, order_id: str) -> bool:
        """
        取消单笔在途委托。
        子类应返回是否发起了取消请求（True/False）。
        """
        return False

    def cleanup_overnight_orders(self) -> dict:
        """
        清理当前在途委托（用于交易日首次运行前的无状态自愈）。
        约定:
        - 依赖 get_pending_orders 返回的 'id' 字段
        - 无 'id' 时跳过，不抛异常
        """
        summary = {'total': 0, 'canceled': 0, 'failed': 0, 'skipped': 0}
        if live_run_budget_expired(self):
            self._runtime_log("[Broker] cleanup_overnight_orders skipped: live run execution budget exhausted.")
            return summary
        try:
            pending_orders = self.get_pending_orders() or []
        except Exception as e:
            summary['failed'] = 1
            self._runtime_log(f"[Broker] cleanup_overnight_orders skipped: failed to fetch pending orders ({e})")
            return summary

        if getattr(self, '_last_pending_orders_fetch_failed', False):
            summary['failed'] = 1
            err = getattr(self, '_last_pending_orders_fetch_error', None)
            self._runtime_log(f"[Broker] cleanup_overnight_orders pending fetch untrusted: {err}")
            return summary

        summary['total'] = len(pending_orders)
        if not pending_orders:
            return summary

        for index, po in enumerate(pending_orders):
            if live_run_budget_expired(self):
                summary['skipped'] += len(pending_orders) - index
                self._runtime_log(
                    "[Broker] cleanup_overnight_orders stopped at the live run deadline; "
                    "remaining orders were left unchanged."
                )
                break
            oid = ''
            if isinstance(po, dict):
                oid = str(po.get('id', '') or '').strip()
            if not oid:
                summary['skipped'] += 1
                continue

            try:
                if self.cancel_pending_order(oid):
                    summary['canceled'] += 1
                else:
                    summary['failed'] += 1
            except Exception as e:
                summary['failed'] += 1
                self._runtime_log(f"[Broker] cleanup_overnight_orders cancel failed ({oid}): {e}")

        return summary

    @abstractmethod
    def _submit_order(self, data, volume, side, price):
        """子类必须实现，用于提交指定标的买入或卖出操作"""
        pass

    @abstractmethod
    def convert_order_proxy(self, raw_order) -> 'BaseOrderProxy':
        """
        将券商的原始订单对象（raw_order）转换为框架标准的 BaseOrderProxy。
        Engine 会调用此方法，从而无需知道具体券商的实现细节。
        """
        raise NotImplementedError("Broker adapter must implement convert_order_proxy(raw_order)")


    # 实盘启动协议
    @classmethod
    def launch(cls, conn_cfg: dict, strategy_path: str, params: dict, **kwargs):
        """
        [可选协议] 实盘启动入口。

        如果通过 `run.py --connect` 启动，框架会调用此方法。
        如果是被动模式或不需要启动器，子类可以不覆盖此方法。
        """
        raise NotImplementedError(
            f"Broker '{cls.__name__}' has not implemented the 'launch' method.\n"
            f"It cannot be started via the 'run.py --connect' command."
        )

    @staticmethod
    @abstractmethod
    def is_live_mode(context) -> bool:
        """
        判断当前是否为实盘模式
        """
        pass

    @staticmethod
    def extract_run_config(context) -> dict:
        """
        静态方法：从特定平台的上下文中提取运行配置。
        默认返回空字典，子类应重写此方法以实现特定逻辑。
        """
        return {}

    def order_target_percent(self, data, target, **kwargs):
        self._last_order_target_skip_reason = None
        if self.is_live and live_run_budget_expired(self):
            self._runtime_log(f"[Broker Warning] {data._name} target-percent order skipped: live run execution budget exhausted.")
            return None

        # 1. 原子操作：查价
        price = self.get_current_price(data)
        if not price or price <= 0: return None
        if self.is_live and live_run_budget_expired(self):
            return None

        # 2. 通用逻辑：算净值 (支持子类覆盖优化)
        portfolio_value = self._get_portfolio_nav()
        if self.is_live and live_run_budget_expired(self):
            return None

        # 3. 核心算法：算股数
        target_value = portfolio_value * target
        expected_shares = target_value / price

        # 改用预期仓位计算差额
        current_size = self.get_expected_size(data)
        if current_size is None:
            msg = (
                f"[Broker Error] {data._name} order skipped because the live pending-order "
                "snapshot is unavailable or untrusted."
            )
            self._runtime_log(msg)
            runtime_notifications.push_text(msg, level='ERROR')
            return None
        delta_shares = expected_shares - current_size

        # 风控拦截：Percent 模式与 Value 模式保持一致
        if data._name in self._risk_locked_symbols and delta_shares > 0:
            self._runtime_log(f"[Broker Risk Block] 🚫 风控拦截: {data._name} 触发风控，买单已被底层静默吃掉。")
            return None

        # 4. 决策分发
        if delta_shares > 0:
            return self._smart_buy(data, delta_shares, price, target, **kwargs)
        elif delta_shares < 0:
            return self._smart_sell(data, abs(delta_shares), price, **kwargs)
        self._last_order_target_skip_reason = 'target_already_met'
        return None

    def order_target_value(self, data, target, **kwargs):
        """
        按目标市值金额下单
        target: 目标持仓金额 (例如 1000 USD)
        """
        self._last_order_target_skip_reason = None
        if self.is_live and live_run_budget_expired(self):
            self._runtime_log(f"[Broker Warning] {data._name} target-value order skipped: live run execution budget exhausted.")
            return None

        # 1. 原子操作：查价
        price = self.get_current_price(data)
        if not price or price <= 0: return None
        if self.is_live and live_run_budget_expired(self):
            return None

        # 2. 核心算法：直接用目标金额除以价格
        expected_shares = target / price

        # 改用预期仓位计算差额
        current_size = self.get_expected_size(data)
        if current_size is None:
            msg = (
                f"[Broker Error] {data._name} order skipped because the live pending-order "
                "snapshot is unavailable or untrusted."
            )
            self._runtime_log(msg)
            runtime_notifications.push_text(msg, level='ERROR')
            return None
        delta_shares = expected_shares - current_size

        # 风控拦截
        if data._name in self._risk_locked_symbols and delta_shares > 0:
            self._runtime_log(f"[Broker Risk Block] 🚫 风控拦截: {data._name} 触发风控，买单已被底层静默吃掉。")
            return None

        # 3. 决策分发
        if delta_shares > 0:
            # 使用针对 Value 模式的智能买入逻辑
            return self._smart_buy_value(data, delta_shares, price, target, **kwargs)
        elif delta_shares < 0:
            return self._smart_sell(data, abs(delta_shares), price, **kwargs)
        self._last_order_target_skip_reason = 'target_already_met'
        return None

    # =========================================================
    #  智能执行逻辑 (Smart Execution)
    # =========================================================

    @classmethod
    def _align_shares_down(cls, shares, lot_size):
        return align_quantity_down(shares, normalize_quantity_step(lot_size))

    def _live_order_chunk_limit(self, data, lot_size, side):
        if not self.is_live:
            return 0

        raw_limit = self._runtime_setting('BROKER_LOT_LIMITS', 0)
        configured_limit = positive_quantity(raw_limit)
        if raw_limit not in (None, '', 0, 0.0, '0') and configured_limit <= 0:
            log.warning(
                f"[Broker] Invalid BROKER_LOT_LIMITS; expected positive number, got "
                f"{raw_limit!r}. Limit ignored.",
                dt=self._datetime,
            )
            return 0

        if configured_limit <= 0:
            return 0

        normalized_lot = normalize_quantity_step(lot_size)
        chunk_limit = self._align_shares_down(configured_limit, normalized_lot)
        if chunk_limit <= 0:
            self._last_order_target_skip_reason = 'broker_lot_limit_exceeded'
            log.warning(
                f"[Broker] BROKER_LOT_LIMITS={configured_limit} is below "
                f"LOT_SIZE={normalized_lot}; {side} {data._name} skipped.",
                dt=self._datetime,
            )
            return None
        return chunk_limit

    def _smart_buy_core(self, data, shares, price, lot_size):
        """智能买入核心逻辑：资金检查 + 自动降级 + 提交记账"""
        run_deadline = get_live_run_deadline(self)
        if run_deadline is None:
            run_deadline = math.inf
        if live_run_budget_expired(self, deadline=run_deadline):
            self._runtime_log(f"[Broker Warning] BUY {data._name} skipped: live run execution budget exhausted.")
            return None

        cash = self.get_cash()

        cost_multiplier = self.safety_multiplier
        estimated_cost = shares * price * cost_multiplier

        cash_limited = cash < estimated_cost
        if cash_limited:
            # 无状态优先：不排队，直接按当前可用现金降级尝试
            max_shares = cash / (price * cost_multiplier)
            shares = min(shares, max_shares)
            min_lot = normalize_quantity_step(lot_size)
            if decimal_quantity(shares, absolute=True) < decimal_quantity(min_lot, absolute=True):
                self._last_order_target_skip_reason = 'insufficient_cash_for_min_lot'
                warning_msg = (
                    f"[Broker Warning] Buy {data._name} skipped. Cash ({cash:.2f}) "
                    f"insufficient for minimum lot {min_lot}; this is not a LOT_SIZE rounding error."
                )
                self._runtime_log(warning_msg)
                runtime_notifications.push_text(warning_msg, level='WARNING')
                return None

        chunk_count = 1
        normalized_shares = self._align_shares_down(shares, lot_size)
        if normalized_shares <= 0:
            return self._finalize_and_submit(
                data,
                shares,
                price,
                lot_size,
                retry_sync_failure=self.is_live,
                run_deadline=run_deadline,
            )

        final_chunk = normalized_shares
        chunk_limit = self._live_order_chunk_limit(data, lot_size, 'BUY')
        if chunk_limit is None:
            return None
        if chunk_limit > 0 and normalized_shares > chunk_limit:
            chunk_count, final_chunk = quantity_chunk_plan(normalized_shares, chunk_limit)
            log.info(
                f"[Broker] Live BUY split {data._name}: total={normalized_shares}, "
                f"limit={chunk_limit}, orders={chunk_count}, final={final_chunk}",
                dt=self._datetime,
            )

        # 将整个当前调用内的拆单、提交和记账包裹在同一把锁内，避免并发抢占。
        with self._ledger_lock:
            first_proxy = None
            submitted_chunks = 0
            use_batch_cash_budget = self.is_live
            if use_batch_cash_budget:
                # GM 等适配器会二次做 cash-fit。调用级预算避免柜台已冻结前一笔后
                # 又减一次虚拟占资；该状态只在本次同步调用内存在。
                self._buy_batch_cash_budget = float(cash)

            try:
                remaining = normalized_shares
                while remaining > 0:
                    if live_run_budget_expired(self, deadline=run_deadline):
                        if first_proxy is not None:
                            error_msg = (
                                f"[Broker] Live BUY split stopped at run deadline for {data._name}: "
                                f"accepted_children={submitted_chunks}; accepted children remain active "
                                "and remaining intent is not persisted."
                            )
                            log.error(error_msg, dt=self._datetime)
                            runtime_notifications.push_text(error_msg, level='ERROR')
                        return first_proxy
                    chunk_shares = min(chunk_limit, remaining) if chunk_limit > 0 else remaining
                    proxy = self._finalize_and_submit(
                        data,
                        chunk_shares,
                        price,
                        lot_size,
                        retry_sync_failure=self.is_live,
                        run_deadline=run_deadline,
                    )
                    if not proxy:
                        if first_proxy is not None:
                            error_msg = (
                                f"[Broker] Live BUY split stopped for {data._name}: "
                                f"accepted_children={submitted_chunks}; "
                                f"downgrade retries exhausted; remaining intent is not persisted."
                            )
                            log.error(error_msg, dt=self._datetime)
                            runtime_notifications.push_text(error_msg, level='ERROR')
                        return first_proxy

                    if first_proxy is None:
                        first_proxy = proxy
                    submitted_chunks += 1

                    submitted_shares = min(
                        chunk_shares,
                        self._infer_submitted_shares(proxy, chunk_shares),
                    )
                    if submitted_shares <= 0:
                        return first_proxy
                    if use_batch_cash_budget:
                        submitted_cost = self._reserved_cash_for_proxy(
                            proxy,
                            submitted_shares,
                            price,
                        )
                        self._buy_batch_cash_budget = max(
                            0.0,
                            self._buy_batch_cash_budget - submitted_cost,
                        )
                    remaining = subtract_quantities(remaining, submitted_shares)
                    # Backtests execute the planned order synchronously. Adapter-side
                    # partial acceptance must not create a live-style tail-order loop.
                    if not self.is_live:
                        remaining = 0

                return first_proxy
            finally:
                if use_batch_cash_budget:
                    self.__dict__.pop('_buy_batch_cash_budget', None)

    def _smart_buy(self, data, shares, price, target_pct, **kwargs):
        """智能买入 (Percent模式)：资金检查 + 自动降级"""
        lot_size = self._runtime_setting('LOT_SIZE', config.LOT_SIZE)
        return self._smart_buy_core(data, shares, price, lot_size)

    def _smart_buy_value(self, data, shares, price, target_value, **kwargs):
        """智能买入 (Value模式)：资金检查 + 自动降级"""
        lot_size = self._runtime_setting('LOT_SIZE', config.LOT_SIZE)
        return self._smart_buy_core(data, shares, price, lot_size)

    def _infer_submitted_shares(self, proxy, fallback_shares):
        """
        推断券商最终受理的委托数量。
        某些适配器会在 _submit_order 内做二次降仓，必须以真实数量记账。
        """
        fallback = positive_quantity(fallback_shares)

        if not proxy:
            return fallback

        def _read_path(obj, path):
            cur = obj
            for attr in path:
                if not hasattr(cur, attr):
                    return None
                cur = getattr(cur, attr)
            return cur

        candidate_paths = [
            ('submitted_size',),              # 适配器可选显式字段
            ('requested_size',),              # 适配器可选显式字段
            ('trade', 'order', 'totalQuantity'),
            ('platform_order', 'volume'),
            ('raw_order', 'volume'),
            ('order', 'totalQuantity'),
        ]

        for path in candidate_paths:
            raw = _read_path(proxy, path)
            val = positive_quantity(raw)
            if val > 0:
                # An adapter may downsize before submission, but it cannot
                # legitimately accept more than this base-layer request.
                return min(val, fallback)

        return fallback

    def _reserved_cash_for_proxy(self, proxy, shares, price):
        """读取适配器按实际保护价估算的买单占资。

        适配器若没有提供该字段，沿用基础层的价格与安全倍率估算；
        这样不会破坏其他券商，同时允许市价保护价高于策略基准价时
        保持虚拟账本、拆单预算和退款口径一致。
        """
        try:
            reserved_cash = float(getattr(proxy, 'reserved_cash', 0.0) or 0.0)
            if math.isfinite(reserved_cash) and reserved_cash > 0:
                return reserved_cash
        except (TypeError, ValueError, OverflowError):
            pass
        return positive_quantity(shares) * float(price) * self.safety_multiplier

    def _reserved_cash_for_buy_info(self, buy_info):
        try:
            reserved_cash = float(buy_info.get('reserved_cash', 0.0) or 0.0)
            if math.isfinite(reserved_cash) and reserved_cash > 0:
                return reserved_cash
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
        return positive_quantity(buy_info.get('shares', 0)) * float(
            buy_info.get('price', 0.0) or 0.0
        ) * self.safety_multiplier

    @staticmethod
    def _read_order_state(proxy):
        """
        Safely read the common order-state contract from a broker proxy.

        A broken adapter method must not make a synchronous terminal/rejected
        order look accepted by accident. Unknown states therefore default to
        False and are handled conservatively by submit finalization.
        """
        try:
            completed = bool(proxy.is_completed())
        except Exception:
            completed = False
        try:
            canceled = bool(proxy.is_canceled())
        except Exception:
            canceled = False
        try:
            rejected = bool(proxy.is_rejected())
        except Exception:
            rejected = False
        try:
            pending = bool(proxy.is_pending())
        except Exception:
            pending = False
        try:
            accepted = bool(proxy.is_accepted())
        except Exception:
            accepted = False

        return {
            'completed': completed,
            'canceled': canceled,
            'rejected': rejected,
            'pending': pending,
            'accepted': accepted,
        }

    def _geometric_downgrade_shares(self, old_shares, lot_size, retries):
        """
        当资金重算不可用时，按倍数（几何）降级股数。
        采用“先缓后急”曲线：早期尽量保持组合一致性，后期加速收敛。
        """
        old_quantity = decimal_quantity(old_shares, absolute=True)
        lot_quantity = decimal_quantity(normalize_quantity_step(lot_size), absolute=True)
        try:
            retries_int = int(retries)
        except Exception:
            retries_int = 0
        if old_quantity <= 0 or lot_quantity <= 0:
            return 0
        factors = (Decimal('0.95'), Decimal('0.90'), Decimal('0.82'), Decimal('0.72'), Decimal('0.60'))
        idx = min(max(0, retries_int), len(factors) - 1)
        factor = factors[idx]

        raw_new = old_quantity * factor
        new_shares = align_quantity_down(raw_new, lot_quantity)
        upper_bound = quantity_number(max(Decimal('0'), old_quantity - lot_quantity))
        return min(new_shares, upper_bound)

    def _lot_step_downgrade_shares(self, old_shares, lot_size):
        """
        线性降级：每次仅减少一个 lot，便于实盘排查和复盘解释。
        """
        old_quantity = decimal_quantity(old_shares, absolute=True)
        lot_quantity = decimal_quantity(normalize_quantity_step(lot_size), absolute=True)
        return quantity_number(max(Decimal('0'), old_quantity - lot_quantity))

    def _next_order_downgrade(self, old_shares, lot_size, retries):
        if retries < self._BUY_LOT_STEP_RETRIES:
            return self._lot_step_downgrade_shares(old_shares, lot_size), "LOT_SIZE阶梯降级"

        geo_retry_idx = retries - self._BUY_LOT_STEP_RETRIES
        return self._geometric_downgrade_shares(
            old_shares,
            lot_size,
            geo_retry_idx,
        ), "几何倍数降级"

    def _finalize_and_submit(
        self,
        data,
        shares,
        price,
        lot_size,
        retries=0,
        retry_sync_failure=False,
        run_deadline=None,
    ):
        """通用的下单收尾逻辑：取整 + 提交"""
        if run_deadline is None:
            run_deadline = get_live_run_deadline(self)
        if run_deadline is None:
            run_deadline = math.inf
        if live_run_budget_expired(self, deadline=run_deadline):
            self._runtime_log(f"[Broker Warning] BUY {data._name} skipped: live run execution budget exhausted.")
            return None

        raw_shares = shares
        shares = self._align_shares_down(shares, lot_size)

        # lot取整异常
        if raw_shares > 0 >= shares:
            self._last_order_target_skip_reason = 'below_min_lot_delta'
            error_msg = (f"🚨 [Broker Warning] {data._name} 订单取整后数量为0！\n"
                         f"原始需求: {format_quantity(raw_shares)}\n"
                         f"当前最小交易单位 (LotSize): {lot_size}\n"
                         f"原因: 原始需求不足一个最小交易单位，订单已自动取消。请检查 LOT_SIZE 配置。")

            self._runtime_log(f"\n{'-' * 30}\n{error_msg}\n{'-' * 30}")

            if not runtime_notifications.push_text(error_msg, level='WARNING'):
                self._runtime_log("[Notification Warning] 无法发送截断警告")

            return None

        if shares > 0:
            # 根据是否为重试改变日志标签
            tag = "实盘降级重试" if retries > 0 else "实盘信号"

            with self._ledger_lock:
                proxy = self._submit_order(data, shares, 'BUY', price)
                if not proxy:
                    # 资金连一手都买不起、或数量本身小于最小交易单位时，
                    # 继续做 10 次降级只会重复同一个必然失败的请求；
                    # 这类短期事实交给执行器本轮过滤，不进入重试风暴。
                    if getattr(self, '_last_order_target_skip_reason', None) in {
                        'insufficient_cash_for_min_lot',
                        'below_min_lot_delta',
                        'invalid_buy_price',
                        'broker_lot_limit_exceeded',
                    }:
                        return None
                    max_retries = self._BUY_LOT_STEP_RETRIES + self._BUY_GEOMETRIC_RETRIES
                    if retry_sync_failure and retries < max_retries:
                        new_shares, downgrade_reason = self._next_order_downgrade(
                            shares,
                            lot_size,
                            retries,
                        )
                        self._runtime_log(
                            f"⚠️ [Broker] 买单 {data._name} 同步提交失败。"
                            f"触发自动降级 {retries + 1}/{max_retries}..."
                        )
                        self._runtime_log(
                            f"   => {data._name} 尝试数量: {shares} -> {new_shares} "
                            f"({downgrade_reason})"
                        )
                        if new_shares > 0:
                            return self._finalize_and_submit(
                                data,
                                new_shares,
                                price,
                                lot_size,
                                retries + 1,
                                retry_sync_failure=True,
                                run_deadline=run_deadline,
                            )
                        self._runtime_log(f"❌ [Broker] 降级终止: {data._name} 数量已降至 0。")
                    elif retry_sync_failure:
                        self._runtime_log(
                            f"❌ [Broker] 降级终止: {data._name} 已达到最大重试次数 "
                            f"{max_retries}，放弃本K。"
                        )
                    return None

                oid = str(getattr(proxy, 'id', '') or '').strip()
                if not oid:
                    self._runtime_log(
                        f"[Broker Warning] BUY {data._name} returned proxy without order id. "
                        f"status={getattr(proxy, 'status', 'Unknown')}"
                    )
                    return None

                final_submitted_shares = self._infer_submitted_shares(proxy, shares)
                reserved_cash = self._reserved_cash_for_proxy(
                    proxy,
                    final_submitted_shares,
                    price,
                )
                # 注册到活跃买单库，记录当前的参数和重试次数。
                self._active_buys[oid] = {
                    'data': data,
                    'shares': final_submitted_shares,
                    'price': price,
                    'reserved_cash': reserved_cash,
                    'lot_size': lot_size,
                    'retries': retries,
                    'retry_sync_failure': retry_sync_failure,
                    'run_deadline': run_deadline,
                }
                self._virtual_spent_cash += reserved_cash

                order_state = self._read_order_state(proxy)

                if order_state['rejected']:
                    self._runtime_log(
                        f"[Broker Warning] BUY {data._name} was synchronously rejected "
                        f"by broker. status={getattr(proxy, 'status', 'Unknown')}"
                    )
                    return self.on_order_status(proxy)

                if order_state['canceled'] or not (
                    order_state['completed'] or order_state['pending'] or order_state['accepted']
                ):
                    self._runtime_log(
                        f"[Broker Warning] BUY {data._name} was not accepted by broker. "
                        f"status={getattr(proxy, 'status', 'Unknown')}"
                    )
                    self.on_order_status(proxy)
                    return None

                log.signal(
                    'BUY', data._name, final_submitted_shares, price, tag=tag,
                    dt=None if self.is_live else self._datetime,
                )

                if order_state['completed']:
                    self.on_order_status(proxy)

                return proxy
        return None

    def _smart_sell(self, data, shares, price, **kwargs):
        """智能卖出：自动注册监控"""
        run_deadline = get_live_run_deadline(self)
        if run_deadline is None:
            run_deadline = math.inf
        if live_run_budget_expired(self, deadline=run_deadline):
            self._runtime_log(f"[Broker Warning] SELL {data._name} skipped: live run execution budget exhausted.")
            return None

        lot_size = self._runtime_setting('LOT_SIZE', config.LOT_SIZE)

        # 获取当前【真实的已结算仓位】
        pos_obj = None
        try:
            pos_obj = self.get_position(data)
            current_pos = positive_quantity(getattr(pos_obj, 'size', 0) or 0)
        except Exception as e:
            msg = (
                f"[Broker Error] SELL {data._name} skipped because the broker position "
                f"snapshot is unavailable or untrusted: {e}"
            )
            self._runtime_log(msg)
            runtime_notifications.push_text(msg, level='ERROR')
            return None
        if live_run_budget_expired(self, deadline=run_deadline):
            return None

        # 获取当前【可卖仓位】；A股 T+1 场景下可卖量可能远小于已结算仓位
        try:
            sellable_hint = None
            if pos_obj is not None:
                pos_dict = getattr(pos_obj, '__dict__', {})
                if isinstance(pos_dict, dict) and 'sellable' in pos_dict:
                    sellable_hint = pos_dict.get('sellable')

            if sellable_hint is not None:
                sellable_pos = positive_quantity(sellable_hint or 0)
            else:
                sellable_pos = positive_quantity(self.get_sellable_position(data) or 0)
        except Exception as e:
            msg = (
                f"[Broker Error] SELL {data._name} skipped because the broker sellable-position "
                f"snapshot is unavailable or untrusted: {e}"
            )
            self._runtime_log(msg)
            runtime_notifications.push_text(msg, level='ERROR')
            return None
        if live_run_budget_expired(self, deadline=run_deadline):
            return None
        sellable_pos = min(sellable_pos, current_pos)

        # T+1 防护：有持仓但不可卖，直接跳过，避免反复触发“仓位不足”拒单。
        if current_pos > 0 and sellable_pos <= 0:
            self._runtime_log(f"[Broker] T+1 sell guard: {data._name} settled={current_pos}, sellable=0. Skip sell.")
            return None

        # 防止做空。你最多只能卖出现有【可卖】持仓！(防止在途买单导致超额卖出)
        shares = min(shares, sellable_pos)

        # 碎股放行逻辑。如果是清仓(或卖出量等于当前可卖仓)，无视 A股 100手 限制，直接全卖
        if shares >= sellable_pos > 0:
            shares = sellable_pos
        else:
            shares = self._align_shares_down(shares, lot_size)

        if shares > 0:
            chunk_limit = self._live_order_chunk_limit(data, lot_size, 'SELL')
            if chunk_limit is None:
                return None

            chunk_count = 1
            final_chunk = shares
            if chunk_limit > 0 and shares > chunk_limit:
                chunk_count, final_chunk = quantity_chunk_plan(shares, chunk_limit)
                log.info(
                    f"[Broker] Live SELL split {data._name}: total={shares}, "
                    f"limit={chunk_limit}, orders={chunk_count}, final={final_chunk}",
                    dt=self._datetime,
                )

            with self._ledger_lock:
                first_proxy = None
                batch_order_ids = []
                total_submitted_shares = 0
                batch_submit_failed = False
                remaining = shares

                while remaining > 0:
                    chunk_shares = min(chunk_limit, remaining) if chunk_limit > 0 else remaining
                    if live_run_budget_expired(self, deadline=run_deadline):
                        batch_submit_failed = True
                        if first_proxy is not None:
                            log.error(
                                f"[Broker] Live SELL split stopped at run deadline for {data._name}: "
                                f"accepted_children={len(batch_order_ids)}; accepted children remain active "
                                "and remaining intent is not persisted.",
                                dt=self._datetime,
                            )
                        break

                    proxy = None
                    candidate_shares = chunk_shares
                    max_retries = self._BUY_LOT_STEP_RETRIES + self._BUY_GEOMETRIC_RETRIES
                    retry_limit = max_retries if self.is_live else 0
                    for retries in range(retry_limit + 1):
                        if live_run_budget_expired(self, deadline=run_deadline):
                            break
                        proxy = self._submit_order(data, candidate_shares, 'SELL', price)
                        if proxy:
                            oid = str(getattr(proxy, 'id', '') or '').strip()
                            if not oid:
                                self._runtime_log(
                                    f"[Broker Warning] SELL {data._name} returned proxy without order id. "
                                    f"status={getattr(proxy, 'status', 'Unknown')}"
                                )
                                proxy = None
                                break
                            order_state = self._read_order_state(proxy)
                            if order_state['completed'] or order_state['pending'] or order_state['accepted']:
                                break
                            self._runtime_log(
                                f"[Broker Warning] SELL {data._name} was not accepted by broker. "
                                f"status={getattr(proxy, 'status', 'Unknown')}"
                            )
                            self.on_order_status(proxy)
                            proxy = None

                        if retries >= retry_limit:
                            break
                        aligned_candidate = self._align_shares_down(candidate_shares, lot_size)
                        if 0 < aligned_candidate < candidate_shares:
                            new_shares = aligned_candidate
                            downgrade_reason = "LOT_SIZE对齐降级"
                        else:
                            new_shares, downgrade_reason = self._next_order_downgrade(
                                candidate_shares,
                                lot_size,
                                retries,
                            )
                        self._runtime_log(
                            f"[Broker] SELL {data._name} 同步提交失败/拒绝，"
                            f"当前run内降级重试 {retries + 1}/{max_retries}: "
                            f"{candidate_shares} -> {new_shares} ({downgrade_reason})"
                        )
                        if new_shares <= 0:
                            break
                        candidate_shares = new_shares

                    if not proxy:
                        batch_submit_failed = True
                        error_msg = (
                            f"[Broker] Live SELL submission stopped for {data._name}: "
                            f"accepted_children={len(batch_order_ids)}; downgrade retries exhausted "
                            "or run deadline reached; remaining sell intent is not persisted."
                        )
                        log.error(error_msg, dt=self._datetime)
                        runtime_notifications.push_text(error_msg, level='ERROR')
                        break

                    oid = str(getattr(proxy, 'id', '') or '').strip()
                    final_submitted_shares = min(
                        remaining,
                        self._infer_submitted_shares(proxy, candidate_shares),
                    )
                    order_state = self._read_order_state(proxy)
                    if final_submitted_shares <= 0:
                        batch_submit_failed = True
                        break

                    if first_proxy is None:
                        first_proxy = proxy
                    batch_order_ids.append(oid)
                    total_submitted_shares = sum_quantities(
                        (total_submitted_shares, final_submitted_shares)
                    )

                    log.signal(
                        'SELL', data._name, final_submitted_shares, price,
                        tag="实盘信号", dt=None if self.is_live else self._datetime,
                    )

                    if order_state['completed']:
                        self.on_order_status(proxy)
                    else:
                        self._pending_sells.add(oid)
                    remaining = subtract_quantities(remaining, final_submitted_shares)
                    # Keep backtests and optimizations single-submit and non-blocking.
                    if not self.is_live:
                        remaining = 0

                if first_proxy is None:
                    return None

                # 保持返回首笔代理的兼容性，并把整批信息交给本轮调仓执行器。
                first_proxy.batch_order_ids = tuple(batch_order_ids)
                first_proxy.batch_submitted_size = total_submitted_shares
                first_proxy.batch_submit_failed = batch_submit_failed
                return first_proxy
        return None

    def on_order_status(self, proxy: BaseOrderProxy):
        """由 Engine 回调，自动维护在途单状态与降级重试"""
        oid = str(getattr(proxy, 'id', '') or '').strip()

        # 整个回调必须排队，防止抢占主线程刚发出的订单
        with self._ledger_lock:
            try:
                is_buy_order = bool(proxy.is_buy())
            except Exception:
                is_buy_order = False
            order_state = self._read_order_state(proxy)

            # ==========================================
            # 1. 买单异步降级逻辑 (Buy Order Downgrade)
            # ==========================================
            if is_buy_order:
                if order_state['completed']:
                    # 买单终态(Filled): 物理现金已结算，必须回退本地虚拟预扣，避免双重扣减可用资金
                    buy_info = self._active_buys.pop(oid, None)
                    if buy_info:
                        refund_amount = self._reserved_cash_for_buy_info(buy_info)
                        symbol = getattr(buy_info.get('data'), '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                        self._virtual_spent_cash = max(
                            0.0,
                            getattr(self, '_virtual_spent_cash', 0.0) - refund_amount
                        )
                        self._runtime_log(f"[Broker] ✅ 买单 {symbol} 已成交。已释放虚拟扣款: {refund_amount:.2f}")
                    return proxy

                elif order_state['canceled']:
                    # 撤单防御：精准回退被冻结的虚拟预扣资金
                    buy_info = self._active_buys.pop(oid, None)
                    if buy_info:
                        refund_amount = self._reserved_cash_for_buy_info(buy_info)
                        symbol = getattr(buy_info.get('data'), '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                        self._virtual_spent_cash = max(
                            0.0,
                            getattr(self, '_virtual_spent_cash', 0.0) - refund_amount
                        )
                        self._runtime_log(f"[Broker] ⚠️ 买单 {symbol} 被撤销。已回退虚拟扣款: {refund_amount:.2f}")
                    return None

                elif order_state['rejected']:
                    buy_info = self._active_buys.pop(oid, None)
                    if buy_info:
                        retries = buy_info['retries']
                        max_retries = self._BUY_LOT_STEP_RETRIES + self._BUY_GEOMETRIC_RETRIES

                        # A. 退回上一笔订单预扣的虚拟资金 (使用动态滑点)
                        refund_amount = self._reserved_cash_for_buy_info(buy_info)
                        self._virtual_spent_cash = max(0.0, getattr(self, '_virtual_spent_cash', 0.0) - refund_amount)

                        run_deadline = buy_info.get('run_deadline')
                        if run_deadline is None:
                            run_deadline = math.inf
                        if live_run_budget_expired(self, deadline=run_deadline):
                            symbol = (
                                getattr(buy_info.get('data'), '_name', None)
                                or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                            )
                            msg = (
                                f"[Broker Warning] BUY {symbol} rejected after its originating live run "
                                "deadline; virtual cash was refunded and downgrade retry was skipped."
                            )
                            self._runtime_log(msg)
                            runtime_notifications.push_text(msg, level='ERROR')
                            return None

                        # B. 检查是否还有重试机会
                        if retries < max_retries:
                            lot_size = buy_info['lot_size']
                            data = buy_info['data']
                            symbol = getattr(data, '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                            price = buy_info['price']

                            old_shares = buy_info['shares']
                            new_shares, downgrade_reason = self._next_order_downgrade(
                                old_shares,
                                lot_size,
                                retries,
                            )

                            self._runtime_log(f"⚠️ [Broker] 买单 {symbol} 被拒绝。触发自动降级 {retries + 1}/{max_retries}...")
                            self._runtime_log(f"   => {symbol} 尝试数量: {old_shares} -> {new_shares} ({downgrade_reason})")

                            if new_shares > 0:
                                # 无状态优先：不入队，拒单后当场按更小数量重提。
                                new_proxy = self._finalize_and_submit(
                                    data,
                                    new_shares,
                                    price,
                                    lot_size,
                                    retries + 1,
                                    retry_sync_failure=bool(
                                        buy_info.get('retry_sync_failure', False)
                                    ),
                                    run_deadline=run_deadline,
                                )

                                if not new_proxy:
                                    self._runtime_log(f"❌ [Broker] 降级发单同步失败，原订单资金已回退。")
                                return new_proxy
                            else:
                                self._runtime_log(f"❌ [Broker] 降级终止: {data._name} 数量已降至 0。")
                        else:
                            symbol = getattr(buy_info.get('data'), '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                            self._runtime_log(f"❌ [Broker] 降级终止: {symbol} 已达到最大重试次数 {max_retries}，放弃本K。")
                    return None
                elif not (order_state['pending'] or order_state['accepted']):
                    buy_info = self._active_buys.pop(oid, None)
                    if buy_info:
                        refund_amount = self._reserved_cash_for_buy_info(buy_info)
                        symbol = getattr(buy_info.get('data'), '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                        self._virtual_spent_cash = max(
                            0.0,
                            getattr(self, '_virtual_spent_cash', 0.0) - refund_amount
                        )
                        self._runtime_log(
                            f"[Broker] ⚠️ 买单 {symbol} 进入非在途终态({getattr(proxy, 'status', 'Unknown')})。"
                            f"已回退虚拟扣款: {refund_amount:.2f}"
                        )
                    return None
                return proxy

            # ==========================================
            # 2. 卖单在途维护逻辑 (Sell Order Pending)
            # ==========================================
            try:
                is_sell_order = bool(proxy.is_sell())
            except Exception:
                is_sell_order = False
            if not is_sell_order:
                return None

            if order_state['completed']:
                self._pending_sells.discard(oid)

            elif order_state['canceled'] or order_state['rejected']:
                self._pending_sells.discard(oid)
            elif order_state['pending'] or order_state['accepted']:
                self._pending_sells.add(oid)
            else:
                self._pending_sells.discard(oid)

    def get_expected_size(self, data):
        """获取包含在途订单的【预期仓位】，防止底层下单方法出现认知撕裂"""
        if self.is_live and live_run_budget_expired(self):
            return None
        try:
            pos_size = self.get_position(data).size
        except Exception as e:
            self._runtime_log(f"[Broker] 获取预期仓位失败: 持仓快照不可信 ({e})")
            return None
        if not self.is_live:
            return pos_size
        if live_run_budget_expired(self):
            return None

        pending_orders = None
        last_error = None
        for attempt in range(1, 4):
            if live_run_budget_expired(self):
                last_error = "live run execution budget exhausted"
                break
            try:
                candidate_orders = self.get_pending_orders() or []
                if getattr(self, '_last_pending_orders_fetch_failed', False):
                    last_error = getattr(self, '_last_pending_orders_fetch_error', None)
                    self._runtime_log(
                        f"[Broker] 获取预期仓位: 在途订单快照不可信 "
                        f"({attempt}/3, {last_error})"
                    )
                    continue
                pending_orders = candidate_orders
                break
            except Exception as e:
                last_error = e
                self._runtime_log(f"[Broker] 获取预期仓位异常 ({attempt}/3): {e}")

        if pending_orders is None:
            self._runtime_log(f"[Broker] 获取预期仓位失败: 已耗尽在途订单快照重试 ({last_error})")
            return None

        try:
            expected_parts = [pos_size]
            for po in pending_orders:
                sym = str(po['symbol']).upper()
                data_name = data._name.upper()
                # 兼容 QQQ.ISLAND 和 QQQ 的匹配
                if sym == data_name or sym == data_name.split('.')[0]:
                    pending_size = decimal_quantity(po.get('size', 0), absolute=True)
                    if str(po.get('direction', '')).upper() == 'BUY':
                        expected_parts.append(pending_size)
                    if str(po.get('direction', '')).upper() == 'SELL':
                        expected_parts.append(-pending_size)
        except Exception as e:
            self._runtime_log(f"[Broker] 获取预期仓位异常: {e}")
            return None
        return sum_quantities(expected_parts)

    def get_cash(self):
        """公有接口：获取资金"""
        # 扣除本地已经花掉的钱，防止穿透
        with self._ledger_lock:
            real_cash = self._fetch_real_cash() - getattr(self, '_virtual_spent_cash', 0.0)
            if real_cash < 0:
                real_cash = 0.0

        if self._cash_override is not None:
            return min(real_cash, self._cash_override)
        return real_cash

    def get_rebalance_cash(self):
        """
        策略层用于“调仓计划总资金”的现金口径。
        默认与 get_cash 一致，子类可覆盖为更保守或更贴合券商语义的实现。
        """
        return self.get_cash()

    def sync_balance(self):
        self._cash = self._fetch_real_cash()

    def _get_portfolio_nav(self):
        """默认 NAV 计算 (Cash + MtM)"""
        val = self.get_cash()
        for d in self.datas:
            pos = self.get_position(d)
            if pos.size:
                p = self.get_current_price(d)
                val += pos.size * p
        return val

    def _init_cash(self):
        real_cash = self._fetch_real_cash()
        if self._cash_override is not None:
            return min(real_cash, self._cash_override)
        return real_cash

    def getposition(self, data):
        """
        [API兼容写法]为了与backtrader的API（self.getposition()）保持一致
        策略代码应不感知实盘系统，直接调用此代码，自动调用子类实现的get_position()
        """
        return self.get_position(data)

    def set_datas(self, datas):
        self.datas = datas

    def lock_for_risk(self, symbol: str):
        """风控专用：锁定标的，禁止买入"""
        self._risk_locked_symbols.add(symbol)

    def unlock_for_risk(self, symbol: str):
        """风控专用：解除标的锁定"""
        self._risk_locked_symbols.discard(symbol)

    def set_datetime(self, dt):
        """设置当前时间，并进行跨周期检查"""
        # 检查时间是否推进 (进入了新的 Bar/Day，跨周期)
        if self._datetime and dt > self._datetime:
            # 不要因为 tick/bar 的更新就清理订单（会误杀 HFT 买单）。
            # 只有在以下两种情况才清理：
            # 1. 跨日了 (New Trading Day) -> 昨天的单子肯定是死单
            # 2. 两次心跳间隔太久 (例如 > 10分钟) -> 说明程序可能断线重启过，状态不可信

            is_new_day = dt.date() > self._datetime.date()

            keep_overnight = bool(getattr(self, '_keep_overnight_orders', False))
            # 24x7 市场可保留跨午夜委托；其本地占资/跟踪也必须一并保留。
            if is_new_day and not keep_overnight:
                self._virtual_spent_cash = 0.0

            # 计算时间差 (秒)
            time_delta = (dt - self._datetime).total_seconds()
            context_attrs = getattr(self._context, '__dict__', {}) or {}
            has_schedule = bool(
                isinstance(context_attrs, dict)
                and (context_attrs.get('schedule_rule') or context_attrs.get('use_schedule'))
            )
            is_long_gap = (
                time_delta > 600
                and not has_schedule
                and not keep_overnight
            )  # schedule/24x7 bar 间隔可能天然超过 10 分钟

            if (is_new_day and not keep_overnight) or is_long_gap:
                has_stale_state = bool(
                    self._pending_sells
                    or self._active_buys
                    or self._virtual_spent_cash > 0
                )
                if has_stale_state:
                    self._runtime_log(f"[Broker] {'New Day' if is_new_day else 'Long Gap'} detected. "
                          f"Resetting stale broker state.")
                    self._reset_stale_state(new_dt=dt)

        self._datetime = dt

    @property
    def datetime(self):
        """模拟 backtrader 的 datetime 属性，使 asof() 等能工作"""
        class dt_proxy:
            def __init__(self, dt): self._dt = dt
            def datetime(self, ago=0): return self._dt
        return dt_proxy(self._datetime)

    def _reset_stale_state(self, new_dt):
        """
        清理陈旧/卡死的状态，防止死锁。
        被 set_datetime 内部调用。
        """
        self._runtime_log(f"[Broker Recovery] Resetting stale state at {new_dt}...")

        # 1. 清理积压的卖单监控
        # 如果发生了跨日或长中断，旧的卖单监控大概率也失效了，重置以防误判
        if self._pending_sells:
            count = len(self._pending_sells)
            self._pending_sells.clear()
            self._runtime_log(f"  >>> Auto-cleared {count} pending sell monitors (Reset).")

        # 2. 清理买单跟踪器
        if hasattr(self, '_active_buys'):
            self._active_buys.clear()

        # 3. 清理虚拟占资，避免长中断后出现幽灵冻结资金
        self._virtual_spent_cash = 0.0
        self._runtime_log("  >>> Broker state reset completed.")

    def force_reset_state(self):
        """
        外部强制重置接口。
        供 Engine 在捕获到 CRITICAL 异常时调用，进行兜底恢复。
        """
        self._runtime_log("[Broker] Force reset state requested by Engine...")
        self._pending_sells.clear()

        # 补丁：彻底清空买单追踪器和虚拟账本占资，防止幽灵占资残留
        if hasattr(self, '_active_buys'):
            self._active_buys.clear()
        self._virtual_spent_cash = 0.0

        try:
            self.sync_balance()
            self._runtime_log(f"  >>> Balance re-synced: {self.get_cash():.2f}")
        except Exception as e:
            self._runtime_log(f"  >>> Warning: Failed to sync balance during reset: {e}")
        self._runtime_log("[Broker] Force reset state completed.")
