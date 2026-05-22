from abc import ABC, abstractmethod
from types import SimpleNamespace

import pandas as pd

from alarms.manager import AlarmManager
from common.log import extract_order_execution_dt


class BaseStrategy(ABC):
    """
    策略抽象基类
    策略作者只需要继承这个类，并实现其核心逻辑。
    'broker'对象将由外部引擎（回测或实盘）注入，它提供了所有交易和数据访问的接口。
    """

    params = {}

    def __init__(self, broker, params=None):
        """
        初始化策略参数
        :param params:
        """
        self.broker = broker
        # 1. 合并类级别定义的默认参数和实例化时传入的参数
        final_params = self.params.copy()
        if params:
            final_params.update(params)

        # 2. 使用辅助类将最终的参数字典转换为一个对象
        self.params = SimpleNamespace(**final_params)

        # 3. 创建 'p' 作为 'params' 的快捷方式，以符合Backtrader的惯例
        self.p = self.params

    def log(self, txt, dt=None):
        """
        通用日志记录
        """
        self.broker.log(txt, dt)

    @abstractmethod
    def init(self):
        """
        策略初始化，在这里准备指标等
        !!!注意，初始化方法只会执行一次，如果将计算逻辑写到这里实盘会有不重新计算的风险，请抽象计算方法并放置于next中!!!
        """
        pass

    @abstractmethod
    def next(self):
        """
        每个K线周期调用的核心逻辑。
        """
        pass

    def notify_order(self, order):
        """
        订单状态通知
        """
        exec_dt = extract_order_execution_dt(order)
        if order.is_completed() and order.executed.size > 0:
            if order.is_buy():
                self.log(
                    f'BUY EXECUTED, Size: {order.executed.size:.2f}, Price: {order.executed.price:.2f}, Cost: {order.executed.value:.2f}, Comm: {order.executed.comm:.5f}',
                    dt=exec_dt)
            elif order.is_sell():
                self.log(
                    f'SELL EXECUTED, Size: {order.executed.size:.2f}, Price: {order.executed.price:.2f}, Cost: {order.executed.value:.2f}, Comm: {order.executed.comm:.5f}',
                    dt=exec_dt)
        elif order.is_rejected():
            self.log(f'Order Canceled/Rejected/Margin')

    def notify_trade(self, trade):
        """
        交易成交通知
        """
        if trade.is_closed():
            self.log(f'OPERATION PROFIT, GROSS {trade.pnl:.2f}, NET {trade.pnlcomm:.2f}')

    def register_indicator(self, data_name: str, indicator_name: str, series: pd.Series):
        """
        [框架层 API] 注册策略的 Pandas Series 指标，自动为其生成回测极速缓存。
        子策略只需在计算出指标后调用此方法即可。
        """
        # 懒加载初始化字典，防止破坏子类的 __init__
        if not hasattr(self, '_indicator_registry'):
            self._indicator_registry = {}
            self._fast_dict_registry = {}

        if data_name not in self._indicator_registry:
            self._indicator_registry[data_name] = {}
            self._fast_dict_registry[data_name] = {}

        # 标准化时区，确保回测与实盘的时间戳格式绝对一致
        if hasattr(series.index, 'tz') and series.index.tz is not None:
            series.index = series.index.tz_localize(None)
        else:
            series.index = pd.to_datetime(series.index)

        # 存入原生的 Pandas Series (供实盘 asof 使用)
        self._indicator_registry[data_name][indicator_name] = series

        # 仅回测模式下，将其转化为 O(1) 的字典缓存
        if not getattr(self.broker, 'is_live', False):
            idx = [dt.to_pydatetime() for dt in series.index]
            self._fast_dict_registry[data_name][indicator_name] = dict(zip(idx, series.values))

    def get_indicator(self, data, indicator_name: str, current_dt):
        """
        [框架层 API] 安全、极速地获取指标值。自动路由双轨制。
        """
        if not hasattr(self, '_indicator_registry'):
            return None

        data_name = data._name

        # 防弹级多维度实盘嗅探
        # 1. 尝试读取 broker 的 is_live 标记
        # 2. 鸭子类型检测：如果 data 没有 datetime 属性，那它 100% 是实盘的 DataFeedProxy
        is_live_mode = getattr(self.broker, 'is_live', False) or not hasattr(data, 'datetime')

        # --- 分支 A：实盘模式 (Live) ---
        if is_live_mode:
            series = self._indicator_registry.get(data_name, {}).get(indicator_name)
            if series is not None:
                # 原汁原味的 asof 保障毫秒级错位容错率
                return series.asof(current_dt)
            return None

        # --- 分支 B：回测极速模式 (Backtest) ---
        else:
            # 只有在确认为原生的 Backtrader DataFeed 时，才调用其专属的 datetime 属性
            data_dt = data.datetime.datetime(0)
            if getattr(data_dt, 'tzinfo', None) is not None:
                data_dt = data_dt.replace(tzinfo=None)

            fast_dict = self._fast_dict_registry.get(data_name, {}).get(indicator_name)
            if fast_dict is not None:
                # O(1) 字典极速提取，100% 命中
                return fast_dict.get(data_dt)

            return None

    def get_strategy_isolated_capital(self):
        """
        获取策略隔离的真实可用资金 (Bottom-Up 盘点法)
        返回: (allocatable_capital, current_positions_dict)
        """
        current_positions = {}
        managed_market_value = 0.0

        # 1. 抓取券商真实在途订单 (降维成大写的字典，方便极速查表)
        pending_map = {}
        if getattr(self.broker, 'is_live', False) and hasattr(self.broker, 'get_pending_orders'):
            try:
                for po in self.broker.get_pending_orders():
                    sym = str(po['symbol']).upper()
                    if sym not in pending_map:
                        pending_map[sym] = {'BUY': 0.0, 'SELL': 0.0}
                    pending_map[sym][po['direction']] += po['size']
            except Exception as e:
                self.log(f"获取在途订单异常: {e}")

        # 辅助查表函数 (支持 IBKR 截断后缀模糊匹配，如 'QQQ.ISLAND' 匹配 'QQQ')
        def get_pending(data_name, direction):
            exact = data_name.upper()
            base = exact.split('.')[0]
            if exact in pending_map: return pending_map[exact][direction]
            if base in pending_map: return pending_map[base][direction]
            return 0.0

        # 2. 盘点所有数据源
        for d in self.broker.datas:
            # 获取券商已结算仓位
            pos = self.broker.getposition(d)
            settled_size = pos.size

            # 【防爆仓核心】计算预期仓位 (Expected Size)
            expected_size = settled_size + get_pending(d._name, 'BUY') - get_pending(d._name, 'SELL')

            # 只要预期仓位 > 0，就纳入市值计算 (交给 Rebalancer 识别)
            if expected_size > 0:
                if hasattr(self.broker, 'get_current_price'):
                    price = self.broker.get_current_price(d)
                elif len(d) > 0:
                    price = d.close[0]
                else:
                    price = pos.price

                market_value = expected_size * price

                # “欺骗” Rebalancer：告诉它当前持仓是 Expected，防止它因未结算而重复发单
                current_positions[d] = market_value
                managed_market_value += market_value

        # 3. 资金盘点
        # - get_cash: 当前可立即下单资金口径（可能包含券商杠杆语义）
        # - get_rebalance_cash: 调仓计划总资金口径（可由子类实现为更保守口径）
        # 策略层统一使用 get_rebalance_cash，避免计划口径与下单口径发生语义撕裂。
        available_cash = self.broker.get_cash()
        rebalance_cash = available_cash
        if hasattr(self.broker, 'get_rebalance_cash'):
            try:
                rebalance_cash = float(self.broker.get_rebalance_cash())
            except Exception as e:
                self.log(f"获取调仓资金口径异常，回退 get_cash: {e}")
                rebalance_cash = available_cash

        allocatable_capital = rebalance_cash + managed_market_value

        return allocatable_capital, current_positions

    @staticmethod
    def _normalize_rebalance_when(raw_value):
        raw = str(raw_value or 'bar').strip().lower()
        allowed = {'bar', 'daily', 'weekly', 'monthly', 'next', 'skip'}
        if raw not in allowed:
            raise ValueError(
                f"Invalid rebalance_when={raw_value!r}. "
                "Expected one of: 'bar', 'daily', 'weekly', 'monthly', 'next', 'skip'."
            )
        return raw

    @staticmethod
    def _normalize_bar_dt(dt_value):
        if dt_value is None:
            return None
        ts = pd.Timestamp(dt_value)
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts

    def _extract_bar_datetimes(self, data):
        current_dt = None
        prev_dt = None

        dt_accessor = getattr(getattr(data, 'datetime', None), 'datetime', None)
        if callable(dt_accessor):
            try:
                current_dt = self._normalize_bar_dt(dt_accessor(0))
            except Exception:
                current_dt = None

            data_len = None
            try:
                data_len = len(data)
            except Exception:
                data_len = None

            if data_len and data_len > 1:
                try:
                    prev_dt = self._normalize_bar_dt(dt_accessor(-1))
                except Exception:
                    prev_dt = None

            if current_dt is not None:
                return current_dt, prev_dt

        df = getattr(getattr(data, 'p', None), 'dataname', None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            current_dt = self._normalize_bar_dt(df.index[-1])
            if len(df.index) > 1:
                prev_dt = self._normalize_bar_dt(df.index[-2])
            return current_dt, prev_dt

        return None, None

    def _get_rebalance_reference_datetimes(self, target_symbols=None):
        candidates = []
        for data in target_symbols or []:
            if data is not None:
                candidates.append(data)
        for data in getattr(self.broker, 'datas', []) or []:
            if data not in candidates:
                candidates.append(data)

        for data in candidates:
            current_dt, prev_dt = self._extract_bar_datetimes(data)
            if current_dt is not None:
                return current_dt, prev_dt

        return None, None

    def should_execute_rebalance(
        self,
        target_symbols=None,
        rebalance_when=None,
    ):
        """
        无状态调仓时点门控:
        统一入口 rebalance_when 支持:
        - 'bar': 每个策略周期都允许调仓（兼容旧行为）
        - 'daily': 仅当进入新交易日时允许调仓
        - 'weekly': 仅当进入新交易周时允许调仓
        - 'monthly': 仅当进入新交易月时允许调仓
        - 'next': 本次就是正式调仓，立即执行
        - 'skip': 本次不是正式调仓，直接跳过
        """
        if rebalance_when is None:
            rebalance_when = getattr(self.p, 'rebalance_when', 'bar')

        frequency = self._normalize_rebalance_when(rebalance_when)
        if frequency == 'next':
            return True
        if frequency == 'skip':
            return False
        if frequency == 'bar':
            return True

        current_dt, prev_dt = self._get_rebalance_reference_datetimes(target_symbols=target_symbols)
        if current_dt is None or prev_dt is None:
            return True

        if frequency == 'daily':
            return current_dt.date() != prev_dt.date()

        if frequency == 'weekly':
            current_iso = current_dt.isocalendar()
            prev_iso = prev_dt.isocalendar()
            return (current_iso.year, current_iso.week) != (prev_iso.year, prev_iso.week)

        if frequency == 'monthly':
            return (current_dt.year, current_dt.month) != (prev_dt.year, prev_dt.month)

        return True

    # 声明式全自动调仓接口
    def execute_rebalance(
        self,
        target_symbols,
        top_k,
        rebalance_threshold=0.2,
        rebalance_when=None,
    ):
        """
        框架级自动调仓流水线。
        包含：自动底层隔离盘点 -> 计划生成 -> 智能发单。
        策略端只需提供目标标的列表，其余一概不用操心。
        rebalance_when 为统一调仓时点入口:
        - 'bar' / 'daily' / 'weekly' / 'monthly'
        - 'next' / 'skip'
        """
        if not self.should_execute_rebalance(
            target_symbols=target_symbols,
            rebalance_when=rebalance_when,
        ):
            return None

        # 延迟导入以防止循环依赖
        from common.order_executor import OrderExecutor
        from common.rebalancer import PortfolioRebalancer

        symbol_map = {}
        for data in getattr(self.broker, 'datas', []) or []:
            full_name = str(getattr(data, '_name', '') or '').strip().upper()
            if not full_name:
                continue
            symbol_map.setdefault(full_name, data)
            symbol_map.setdefault(full_name.split('.')[0], data)

        resolved_targets = []
        seen_targets = set()
        unknown_targets = []
        for raw_target in target_symbols or []:
            raw_name = getattr(raw_target, '_name', raw_target)
            full_name = str(raw_name or '').strip().upper()
            if not full_name:
                continue
            resolved = symbol_map.get(full_name) or symbol_map.get(full_name.split('.')[0])
            if resolved is None:
                unknown_targets.append(str(raw_name))
                continue

            resolved_name = getattr(resolved, '_name', str(resolved))
            if resolved_name in seen_targets:
                continue
            seen_targets.add(resolved_name)
            resolved_targets.append(resolved)

        if unknown_targets:
            msg = (
                "[Rebalance Warning] execute_rebalance 发现标的池外目标，当前调仓已跳过这些目标，不会对其发单。"
                f" unknown_targets={unknown_targets}"
            )
            self.log(msg)
            try:
                AlarmManager().push_text(msg, level='WARNING')
            except Exception:
                pass

        # 1. 底层框架全自动盘点真实可用资金 (已完美无视所有豁免底仓)
        allocatable_capital, current_positions = self.get_strategy_isolated_capital()

        # 2. 生成调仓计划
        plan = PortfolioRebalancer.calculate_plan(
            current_positions=current_positions,
            target_symbols=resolved_targets,
            total_capital=allocatable_capital,
            select_top_k=top_k,
            rebalance_threshold=rebalance_threshold
        )

        # 3. 执行发单
        if not hasattr(self, 'executor'):
            self.executor = OrderExecutor(self.broker)

        self.executor.execute_plan(plan)
