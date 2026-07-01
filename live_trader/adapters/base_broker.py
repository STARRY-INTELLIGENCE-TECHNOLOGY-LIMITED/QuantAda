import threading
from abc import ABC, abstractmethod

import pandas as pd

import config
from common import log

from alarms.manager import AlarmManager
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

    def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None,):
        self.is_live = True
        self._context = context
        self.datas = []
        self._datetime = None
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
        # 如果没有传入时间，优先使用当前 Broker 所在的仿真时间
        if dt is None:
            dt = getattr(self, '_datetime', None)

        log.info(txt, dt=dt)

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
        try:
            pos = self.get_position(data)
            return max(0, int(float(getattr(pos, 'size', 0) or 0)))
        except Exception:
            return 0

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
        try:
            pending_orders = self.get_pending_orders() or []
        except Exception as e:
            summary['failed'] = 1
            print(f"[Broker] cleanup_overnight_orders skipped: failed to fetch pending orders ({e})")
            return summary

        if getattr(self, '_last_pending_orders_fetch_failed', False):
            summary['failed'] = 1
            err = getattr(self, '_last_pending_orders_fetch_error', None)
            print(f"[Broker] cleanup_overnight_orders pending fetch untrusted: {err}")

        summary['total'] = len(pending_orders)
        if not pending_orders:
            return summary

        for po in pending_orders:
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
                print(f"[Broker] cleanup_overnight_orders cancel failed ({oid}): {e}")

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
        # 1. 原子操作：查价
        price = self.get_current_price(data)
        if not price or price <= 0: return None

        # 2. 通用逻辑：算净值 (支持子类覆盖优化)
        portfolio_value = self._get_portfolio_nav()

        # 3. 核心算法：算股数
        target_value = portfolio_value * target
        expected_shares = target_value / price

        # 改用预期仓位计算差额
        current_size = self.get_expected_size(data)
        delta_shares = expected_shares - current_size

        # 风控拦截：Percent 模式与 Value 模式保持一致
        if data._name in self._risk_locked_symbols and delta_shares > 0:
            print(f"[Broker Risk Block] 🚫 风控拦截: {data._name} 触发风控，买单已被底层静默吃掉。")
            return None

        # 4. 决策分发
        if delta_shares > 0:
            return self._smart_buy(data, delta_shares, price, target, **kwargs)
        elif delta_shares < 0:
            return self._smart_sell(data, abs(delta_shares), price, **kwargs)
        return None

    def order_target_value(self, data, target, **kwargs):
        """
        按目标市值金额下单
        target: 目标持仓金额 (例如 1000 USD)
        """
        # 1. 原子操作：查价
        price = self.get_current_price(data)
        if not price or price <= 0: return None

        # 2. 核心算法：直接用目标金额除以价格
        expected_shares = target / price

        # 改用预期仓位计算差额
        current_size = self.get_expected_size(data)
        delta_shares = expected_shares - current_size

        # 风控拦截
        if data._name in self._risk_locked_symbols and delta_shares > 0:
            print(f"[Broker Risk Block] 🚫 风控拦截: {data._name} 触发风控，买单已被底层静默吃掉。")
            return None

        # 3. 决策分发
        if delta_shares > 0:
            # 使用针对 Value 模式的智能买入逻辑
            return self._smart_buy_value(data, delta_shares, price, target, **kwargs)
        elif delta_shares < 0:
            return self._smart_sell(data, abs(delta_shares), price, **kwargs)
        return None

    # =========================================================
    #  智能执行逻辑 (Smart Execution)
    # =========================================================

    def _smart_buy_core(self, data, shares, price, lot_size):
        """智能买入核心逻辑：资金检查 + 自动降级 + 提交记账"""
        cash = self.get_cash()

        cost_multiplier = self.safety_multiplier
        estimated_cost = shares * price * cost_multiplier

        if cash < estimated_cost:
            # 无状态优先：不排队，直接按当前可用现金降级尝试
            max_shares = cash / (price * cost_multiplier)
            shares = min(shares, max_shares)
            if shares < 1:
                print(f"[Broker Warning] Buy {data._name} skipped. Cash ({cash:.2f}) insufficient.")

        # 将提交和记账包裹在同一把锁内，拒绝间隙抢占
        with self._ledger_lock:
            proxy = self._finalize_and_submit(data, shares, price, lot_size)
            # 记账到虚拟账本
            if proxy:
                submitted_shares = self._active_buys.get(proxy.id, {}).get('shares', shares)
                self._virtual_spent_cash += (submitted_shares * price * cost_multiplier)
        return proxy

    def _smart_buy(self, data, shares, price, target_pct, **kwargs):
        """智能买入 (Percent模式)：资金检查 + 自动降级"""
        lot_size = config.LOT_SIZE
        return self._smart_buy_core(data, shares, price, lot_size)

    def _smart_buy_value(self, data, shares, price, target_value, **kwargs):
        """智能买入 (Value模式)：资金检查 + 自动降级"""
        lot_size = config.LOT_SIZE
        return self._smart_buy_core(data, shares, price, lot_size)

    def _infer_submitted_shares(self, proxy, fallback_shares):
        """
        推断券商最终受理的委托数量。
        某些适配器会在 _submit_order 内做二次降仓，必须以真实数量记账。
        """
        try:
            fallback = int(abs(float(fallback_shares)))
        except Exception:
            fallback = 0

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
            try:
                val = int(abs(float(raw)))
                if val > 0:
                    return val
            except Exception:
                continue

        return fallback

    def _geometric_downgrade_shares(self, old_shares, lot_size, retries):
        """
        当资金重算不可用时，按倍数（几何）降级股数。
        采用“先缓后急”曲线：早期尽量保持组合一致性，后期加速收敛。
        """
        try:
            old_int = int(abs(float(old_shares)))
            lot_int = int(abs(float(lot_size)))
            retries_int = int(retries)
        except Exception:
            return 0

        if old_int <= 0:
            return 0

        lot_int = max(1, lot_int)
        factors = (0.95, 0.90, 0.82, 0.72, 0.60)
        idx = min(max(0, retries_int), len(factors) - 1)
        factor = factors[idx]

        raw_new = old_int * factor
        if lot_int > 1:
            new_shares = int(raw_new // lot_int) * lot_int
        else:
            new_shares = int(raw_new)

        # 保证比原单更小，防止无效重复提交
        upper_bound = old_int - lot_int
        new_shares = min(new_shares, upper_bound)
        return max(0, new_shares)

    def _lot_step_downgrade_shares(self, old_shares, lot_size):
        """
        线性降级：每次仅减少一个 lot，便于实盘排查和复盘解释。
        """
        try:
            old_int = int(abs(float(old_shares)))
            lot_int = int(abs(float(lot_size)))
        except Exception:
            return 0

        if old_int <= 0:
            return 0

        lot_int = max(1, lot_int)
        return max(0, old_int - lot_int)

    def _finalize_and_submit(self, data, shares, price, lot_size, retries=0):
        """通用的下单收尾逻辑：取整 + 提交"""
        raw_shares = shares
        if lot_size > 1:
            shares = int(shares // lot_size) * lot_size
        else:
            shares = int(shares)

        # lot取整异常
        if raw_shares > 0 >= shares:
            error_msg = (f"🚨 [Broker Warning] {data._name} 订单取整后股数为0！\n"
                         f"原始需求: {raw_shares:.2f} 股\n"
                         f"当前最小交易单位 (LotSize): {lot_size}\n"
                         f"原因: 原始需求不足一手，订单已自动取消。请检查 LOT_SIZE 配置。")

            print(f"\n{'-' * 30}\n{error_msg}\n{'-' * 30}")

            try:
                AlarmManager().push_text(error_msg, level='WARNING')
            except Exception as e:
                print(f"[Alarm Error] 无法发送截断警告: {e}")

            return None

        if shares > 0:
            # 根据是否为重试改变日志标签
            tag = "实盘降级重试" if retries > 0 else "实盘信号"
            log.signal('BUY', data._name, shares, price, tag=tag, dt=self._datetime)

            with self._ledger_lock:
                proxy = self._submit_order(data, shares, 'BUY', price)
                if proxy:
                    final_submitted_shares = self._infer_submitted_shares(proxy, shares)
                    # 注册到活跃买单库，记录当前的参数和重试次数
                    self._active_buys[proxy.id] = {
                        'data': data,
                        'shares': final_submitted_shares,
                        'price': price,
                        'lot_size': lot_size,
                        'retries': retries
                    }
            return proxy
        return None

    def _smart_sell(self, data, shares, price, **kwargs):
        """智能卖出：自动注册监控"""
        lot_size = config.LOT_SIZE

        # 获取当前【真实的已结算仓位】
        pos_obj = None
        try:
            pos_obj = self.get_position(data)
            current_pos = max(0, int(float(getattr(pos_obj, 'size', 0) or 0)))
        except Exception:
            current_pos = 0

        # 获取当前【可卖仓位】；A股 T+1 场景下可卖量可能远小于已结算仓位
        try:
            sellable_hint = None
            if pos_obj is not None:
                pos_dict = getattr(pos_obj, '__dict__', {})
                if isinstance(pos_dict, dict) and 'sellable' in pos_dict:
                    sellable_hint = pos_dict.get('sellable')

            if sellable_hint is not None:
                sellable_pos = max(0, int(float(sellable_hint or 0)))
            else:
                sellable_pos = max(0, int(float(self.get_sellable_position(data) or 0)))
        except Exception:
            sellable_pos = 0
        sellable_pos = min(sellable_pos, current_pos)

        # T+1 防护：有持仓但不可卖，直接跳过，避免反复触发“仓位不足”拒单。
        if current_pos > 0 and sellable_pos <= 0:
            print(f"[Broker] T+1 sell guard: {data._name} settled={current_pos}, sellable=0. Skip sell.")
            return None

        # 防止做空。你最多只能卖出现有【可卖】持仓！(防止在途买单导致超额卖出)
        shares = min(shares, sellable_pos)

        # 碎股放行逻辑。如果是清仓(或卖出量等于当前可卖仓)，无视 A股 100手 限制，直接全卖
        if shares >= sellable_pos > 0:
            shares = sellable_pos
        else:
            if lot_size > 1:
                shares = int(shares // lot_size) * lot_size
            else:
                shares = int(shares)

        if shares > 0:
            log.signal('SELL', data._name, shares, price, tag="实盘信号", dt=self._datetime)
            with self._ledger_lock:
                proxy = self._submit_order(data, shares, 'SELL', price)
                if proxy:
                    self._pending_sells.add(proxy.id)
            return proxy
        return None

    def on_order_status(self, proxy: BaseOrderProxy):
        """由 Engine 回调，自动维护在途单状态与降级重试"""
        oid = proxy.id

        # 整个回调必须排队，防止抢占主线程刚发出的订单
        with self._ledger_lock:
            # ==========================================
            # 1. 买单异步降级逻辑 (Buy Order Downgrade)
            # ==========================================
            if proxy.is_buy():
                if proxy.is_completed():
                    # 买单终态(Filled): 物理现金已结算，必须回退本地虚拟预扣，避免双重扣减可用资金
                    buy_info = self._active_buys.pop(oid, None)
                    if buy_info:
                        refund_amount = buy_info['shares'] * buy_info['price'] * self.safety_multiplier
                        symbol = getattr(buy_info.get('data'), '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                        self._virtual_spent_cash = max(
                            0.0,
                            getattr(self, '_virtual_spent_cash', 0.0) - refund_amount
                        )
                        print(f"[Broker] ✅ 买单 {symbol} 已成交。已释放虚拟扣款: {refund_amount:.2f}")

                elif proxy.is_canceled():
                    # 撤单防御：精准回退被冻结的虚拟预扣资金
                    buy_info = self._active_buys.pop(oid, None)
                    if buy_info:
                        refund_amount = buy_info['shares'] * buy_info['price'] * self.safety_multiplier
                        symbol = getattr(buy_info.get('data'), '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                        self._virtual_spent_cash = max(
                            0.0,
                            getattr(self, '_virtual_spent_cash', 0.0) - refund_amount
                        )
                        print(f"[Broker] ⚠️ 买单 {symbol} 被撤销。已回退虚拟扣款: {refund_amount:.2f}")

                elif proxy.is_rejected():
                    buy_info = self._active_buys.pop(oid, None)
                    if buy_info:
                        retries = buy_info['retries']
                        lot_step_retries = 5
                        geometric_retries = 5
                        max_retries = lot_step_retries + geometric_retries

                        # A. 退回上一笔订单预扣的虚拟资金 (使用动态滑点)
                        refund_amount = buy_info['shares'] * buy_info['price'] * self.safety_multiplier
                        self._virtual_spent_cash = max(0.0, getattr(self, '_virtual_spent_cash', 0.0) - refund_amount)

                        # B. 检查是否还有重试机会
                        if retries < max_retries:
                            lot_size = buy_info['lot_size']
                            data = buy_info['data']
                            symbol = getattr(data, '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                            price = buy_info['price']

                            # 可解释性优先:
                            # - 前 5 次按 lot 线性降级
                            # - 后 5 次按几何倍数降级
                            old_shares = buy_info['shares']
                            if retries < lot_step_retries:
                                new_shares = self._lot_step_downgrade_shares(old_shares, lot_size)
                                downgrade_reason = "LOT_SIZE阶梯降级"
                            else:
                                geo_retry_idx = retries - lot_step_retries
                                new_shares = self._geometric_downgrade_shares(old_shares, lot_size, geo_retry_idx)
                                downgrade_reason = "几何倍数降级"

                            print(f"⚠️ [Broker] 买单 {symbol} 被拒绝。触发自动降级 {retries + 1}/{max_retries}...")
                            print(f"   => {symbol} 尝试数量: {old_shares} -> {new_shares} ({downgrade_reason})")

                            if new_shares > 0:
                                # 无状态优先：不入队，拒单后当场按更小数量重提。
                                deduct_amount = new_shares * price * self.safety_multiplier
                                self._virtual_spent_cash += deduct_amount

                                new_proxy = self._finalize_and_submit(data, new_shares, price, lot_size,
                                                                      retries + 1)

                                # 如果同步发单失败(比如断网)，必须把预扣的钱退回来
                                if not new_proxy:
                                    self._virtual_spent_cash = max(
                                        0.0,
                                        getattr(self, '_virtual_spent_cash', 0.0) - deduct_amount
                                    )
                                    print(f"❌ [Broker] 降级发单同步失败，资金已回退。")
                            else:
                                print(f"❌ [Broker] 降级终止: {data._name} 数量已降至 0。")
                        else:
                            symbol = getattr(buy_info.get('data'), '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                            print(f"❌ [Broker] 降级终止: {symbol} 已达到最大重试次数 {max_retries}，放弃本K。")
                elif not proxy.is_pending():
                    buy_info = self._active_buys.pop(oid, None)
                    if buy_info:
                        refund_amount = buy_info['shares'] * buy_info['price'] * self.safety_multiplier
                        symbol = getattr(buy_info.get('data'), '_name', None) or getattr(getattr(proxy, 'data', None), '_name', 'Unknown')
                        self._virtual_spent_cash = max(
                            0.0,
                            getattr(self, '_virtual_spent_cash', 0.0) - refund_amount
                        )
                        print(
                            f"[Broker] ⚠️ 买单 {symbol} 进入非在途终态({getattr(proxy, 'status', 'Unknown')})。"
                            f"已回退虚拟扣款: {refund_amount:.2f}"
                        )
                return

            # ==========================================
            # 2. 卖单在途维护逻辑 (Sell Order Pending)
            # ==========================================
            if not proxy.is_sell(): return

            if proxy.is_completed():
                self._pending_sells.discard(oid)

            elif proxy.is_canceled() or proxy.is_rejected():
                self._pending_sells.discard(oid)
            elif proxy.is_pending():
                self._pending_sells.add(oid)
            else:
                self._pending_sells.discard(oid)

    def get_expected_size(self, data):
        """获取包含在途订单的【预期仓位】，防止底层下单方法出现认知撕裂"""
        pos_size = self.get_position(data).size
        try:
            pending_orders = self.get_pending_orders()
            for po in pending_orders:
                sym = str(po['symbol']).upper()
                data_name = data._name.upper()
                # 兼容 QQQ.ISLAND 和 QQQ 的匹配
                if sym == data_name or sym == data_name.split('.')[0]:
                    if po['direction'] == 'BUY': pos_size += po['size']
                    if po['direction'] == 'SELL': pos_size -= po['size']
        except Exception as e:
            print(f"[Broker] 获取预期仓位异常: {e}")
        return pos_size

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

            # 仅跨日清空虚拟占资，避免日内 bar 推进误释放占资保护。
            if is_new_day:
                self._virtual_spent_cash = 0.0

            # 计算时间差 (秒)
            time_delta = (dt - self._datetime).total_seconds()
            context_attrs = getattr(self._context, '__dict__', {}) or {}
            has_schedule = bool(
                isinstance(context_attrs, dict)
                and (context_attrs.get('schedule_rule') or context_attrs.get('use_schedule'))
            )
            is_long_gap = (time_delta > 600) and not has_schedule  # schedule 间隔可能天然超过 10 分钟

            if is_new_day or is_long_gap:
                has_stale_state = bool(
                    self._pending_sells
                    or self._active_buys
                    or self._virtual_spent_cash > 0
                )
                if has_stale_state:
                    print(f"[Broker] {'New Day' if is_new_day else 'Long Gap'} detected. "
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
        print(f"[Broker Recovery] Resetting stale state at {new_dt}...")

        # 1. 清理积压的卖单监控
        # 如果发生了跨日或长中断，旧的卖单监控大概率也失效了，重置以防误判
        if self._pending_sells:
            count = len(self._pending_sells)
            self._pending_sells.clear()
            print(f"  >>> Auto-cleared {count} pending sell monitors (Reset).")

        # 2. 清理买单跟踪器
        if hasattr(self, '_active_buys'):
            self._active_buys.clear()

        # 3. 清理虚拟占资，避免长中断后出现幽灵冻结资金
        self._virtual_spent_cash = 0.0
        print("  >>> Broker state reset completed.")

    def force_reset_state(self):
        """
        外部强制重置接口。
        供 Engine 在捕获到 CRITICAL 异常时调用，进行兜底恢复。
        """
        print("[Broker] Force reset state requested by Engine...")
        self._pending_sells.clear()

        # 补丁：彻底清空买单追踪器和虚拟账本占资，防止幽灵占资残留
        if hasattr(self, '_active_buys'):
            self._active_buys.clear()
        self._virtual_spent_cash = 0.0

        try:
            self.sync_balance()
            print(f"  >>> Balance re-synced: {self.get_cash():.2f}")
        except Exception as e:
            print(f"  >>> Warning: Failed to sync balance during reset: {e}")
        print("[Broker] Force reset state completed.")
