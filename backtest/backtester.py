import datetime
import math

import backtrader as bt
import pandas as pd

import config
from backtest.closed_trade_tracker import ClosedTradeTracker
from backtest.plotting import (
    apply_data_feed_plot_scope,
    configure_plot_observers,
    create_cerebro,
    parse_plot_scopes,
    plot_cerebro,
)
from backtest.reporting import format_backtest_results_report
from backtest.trade_attribution import format_trade_micro_attribution_report
from common import log, runtime_command, runtime_notifications
from common.order_quantity import align_quantity_down, normalize_quantity_step


def _extract_contract_multiplier(data) -> float:
    """从 DataFeed 或其原始 DataFrame 读取现金名义乘数。"""
    try:
        from common.options.analytics import parse_option_symbol

        is_option = bool(parse_option_symbol(getattr(data, '_name', '')).get('option_type'))
    except Exception:
        is_option = False
    sources = [data, getattr(data, 'p', None)]
    dataframe = getattr(getattr(data, 'p', None), 'dataname', None)
    # 期权专属字段优先，避免 DataFeed 默认 contract_multiplier=1 吞掉原始表中的真实乘数。
    name_groups = (
        ('option_contract_multiplier', 'option_contract_size'),
        ('contract_multiplier', 'contract_size'),
    )

    def read_values(source, names):
        if source is None:
            return []
        values = []
        if isinstance(source, dict):
            for name in names:
                if name in source:
                    values.append(source[name])
        for name in names:
            try:
                value = getattr(source, name)
            except Exception:
                continue
            if value is not None:
                values.append(value)
        return values

    def parse_values(values):
        for value in values:
            try:
                multiplier = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(multiplier) and multiplier > 0:
                return multiplier
        return None

    for names in name_groups:
        for source in sources:
            multiplier = parse_values(read_values(source, names))
            if multiplier is not None:
                if is_option and names == ('contract_multiplier', 'contract_size') and multiplier <= 1:
                    continue
                return multiplier
        if not isinstance(dataframe, pd.DataFrame):
            continue
        attrs = getattr(dataframe, 'attrs', {}) or {}
        multiplier = parse_values(read_values(attrs, names))
        if multiplier is not None:
            if is_option and names == ('contract_multiplier', 'contract_size') and multiplier <= 1:
                continue
            return multiplier
        for name in names:
            if name not in dataframe.columns:
                continue
            values = pd.to_numeric(dataframe[name], errors='coerce').dropna()
            multiplier = parse_values(reversed(values.tolist()))
            if multiplier is not None:
                if is_option and names == ('contract_multiplier', 'contract_size') and multiplier <= 1:
                    continue
                return multiplier
    # 期权缺少现金名义乘数时必须失败关闭，不能把普通股票的 1 倍
    # 默认值带入担保金额、手续费或持仓估值。
    if is_option:
        return 0.0
    return 1.0


def _validate_backtest_risk_leg(data, risk_leg) -> None:
    """校验回测风险腿与当前 DataFeed 的静态合约元数据一致。"""
    from common.options.analytics import parse_expiry, parse_option_symbol, underlying_key

    try:
        strike = float(risk_leg.strike)
        multiplier = float(risk_leg.contract_multiplier)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError('invalid option risk leg metadata') from None
    if not math.isfinite(strike) or strike <= 0 or not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError('invalid option risk leg metadata')
    actual_multiplier = float(_extract_contract_multiplier(data))
    if not math.isfinite(actual_multiplier) or actual_multiplier <= 0:
        raise ValueError('option contract multiplier is unavailable')
    if abs(multiplier - actual_multiplier) > max(1e-9, abs(actual_multiplier) * 1e-9):
        raise ValueError('risk leg multiplier does not match option contract')

    parsed = parse_option_symbol(getattr(data, '_name', ''))
    # 自定义 DataFeed 可能使用不可解析的显示名称（例如 ``AAPL_OPTION``），
    # 此时仍需用其静态元数据校验风险腿，不能让错误执行价/方向进入现金模型。
    dataframe = getattr(getattr(data, 'p', None), 'dataname', None)
    if isinstance(dataframe, pd.DataFrame) and not dataframe.empty:
        def last_value(names):
            for name in names:
                if name not in dataframe.columns:
                    continue
                for value in reversed(dataframe[name].tolist()):
                    try:
                        if value is not None and not pd.isna(value):
                            return value
                    except (TypeError, ValueError):
                        if value is not None:
                            return value
            return None

        metadata_type = str(last_value(('option_type', 'right', 'cp', 'put_call')) or '').strip().upper()
        if metadata_type in {'P', 'PUT', 'C', 'CALL'}:
            expected_type = 'PUT' if metadata_type in {'P', 'PUT'} else 'CALL'
            option_type = str(getattr(risk_leg, 'option_type', '') or '').strip().upper()
            if option_type not in {expected_type, expected_type[:1]}:
                raise ValueError('risk leg option type does not match DataFeed metadata')
        metadata_strike = last_value(('strike', 'strike_price', 'exercise_price'))
        if metadata_strike is not None:
            try:
                parsed_metadata_strike = float(metadata_strike)
            except (TypeError, ValueError, OverflowError):
                raise ValueError('DataFeed option strike is invalid') from None
            if not math.isfinite(parsed_metadata_strike) or parsed_metadata_strike <= 0:
                raise ValueError('DataFeed option strike is invalid')
            if abs(strike - parsed_metadata_strike) > max(1e-9, abs(parsed_metadata_strike) * 1e-9):
                raise ValueError('risk leg strike does not match DataFeed metadata')
        metadata_expiry = parse_expiry(last_value(('expiry', 'expiration', 'expiration_date')))
        provided_expiry = parse_expiry(getattr(risk_leg, 'expiry', None))
        if pd.notna(metadata_expiry) and pd.notna(provided_expiry):
            if pd.Timestamp(metadata_expiry).normalize() != pd.Timestamp(provided_expiry).normalize():
                raise ValueError('risk leg expiry does not match DataFeed metadata')
        metadata_underlying = last_value(('underlying', 'underlying_symbol', 'underlying_code'))
        if metadata_underlying is not None:
            if underlying_key(metadata_underlying) != underlying_key(getattr(risk_leg, 'underlying', '')):
                raise ValueError('risk leg underlying does not match DataFeed metadata')

    if not parsed.get('option_type'):
        return
    option_type = str(getattr(risk_leg, 'option_type', '') or '').upper()
    expected_type = str(parsed['option_type']).upper()
    if option_type not in {expected_type, expected_type[:1]}:
        raise ValueError('risk leg option type does not match option symbol')
    parsed_strike = float(parsed['strike'])
    if abs(strike - parsed_strike) > max(1e-9, abs(parsed_strike) * 1e-9):
        raise ValueError('risk leg strike does not match option symbol')
    provided_underlying_raw = str(getattr(risk_leg, 'underlying', '') or '').upper()
    expected_underlying = str(parsed.get('underlying') or '').upper()
    if expected_underlying and underlying_key(provided_underlying_raw) != expected_underlying:
        raise ValueError('risk leg underlying does not match option symbol')
    provided_expiry = parse_expiry(getattr(risk_leg, 'expiry', None))
    if pd.notna(provided_expiry) and pd.Timestamp(provided_expiry).normalize() != pd.Timestamp(parsed['expiry']).normalize():
        raise ValueError('risk leg expiry does not match option symbol')


class _ContractMultiplierCommInfo(bt.CommInfoBase):
    """将每份合约的价格、手续费和持仓估值按现金乘数放大。"""

    def getoperationcost(self, size, price):
        return super().getoperationcost(size, price) * self.p.mult

    def getvaluesize(self, size, price):
        return super().getvaluesize(size, price) * self.p.mult

    def getvalue(self, position, price):
        return super().getvalue(position, price) * self.p.mult

    def _getcommission(self, size, price, pseudoexec):
        commission = super()._getcommission(size, price, pseudoexec)
        if self._commtype == self.COMM_PERC:
            return commission * self.p.mult
        return commission


class OrderProxy:
    def __init__(self, bt_order): self._order = bt_order

    def is_buy(self): return self._order.isbuy()

    def is_sell(self): return self._order.issell()

    def is_pending(self): return self._order.status in [self._order.Submitted, self._order.Accepted]

    def is_completed(self): return self._order.status == self._order.Completed

    def is_rejected(self): return self._order.status in [self._order.Canceled, self._order.Margin,
                                                         self._order.Rejected]

    def getstatusname(self):
        """将调用转发给原始 backtrader 订单对象。"""
        return self._order.getstatusname()

    @property
    def executed(self): return self._order.executed

    @property
    def data(self):
        """暴露订单关联的 data feed。"""
        return self._order.data


class TradeProxy:
    """将backtrader的trade对象适配成通用接口"""

    def __init__(self, bt_trade): self._trade = bt_trade

    def is_closed(self): return self._trade.isclosed

    @property
    def pnl(self): return self._trade.pnl

    @property
    def pnlcomm(self): return self._trade.pnlcomm

    @property
    def data(self): return self._trade.data


class BacktraderStrategyWrapper(bt.Strategy):
    """
    Backtrader的包装器策略
    唯一职责是加载我们的纯策略，并将Backtrader的环境传递给它
    """

    def __init__(
        self,
        strategy_class,
        params=None,
        risk_control_classes=None,
        risk_control_params=None,
        recorder=None,
        verbose=True,
        slippage=0.0,
        indicator_cache=None,
    ):
        self.is_live = False
        self.recorder = recorder
        self.verbose = verbose
        self.slippage = max(0.0, float(slippage or 0.0))
        self.indicator_cache = indicator_cache if isinstance(indicator_cache, dict) else {}
        # 增加一个属性用于存储实际开始日期，面向解决多标的数据就绪问题
        self.actual_start_date = None
        # 用于记录单次 next 循环中，卖单预计释放的资金
        self.expected_freed_cash = 0.0
        # 本轮循环已花费的虚拟现金
        self.virtual_spent_cash = 0.0
        # 已提交但尚未在持仓快照中体现的期权现金义务。
        # 回测撮合通常同步完成，但 Backtrader 的通知与策略回调仍可能跨一个
        # bar，必须按订单生命周期保留该短期账本，防止同一 bar 重复卖开。
        self._option_cash_reservations = {}
        self._option_spread_seq = 0
        self.dataclose = self.datas[0].close
        self.strategy = strategy_class(broker=self, params=params)
        self.risk_controls = []
        self.trade_tracker = ClosedTradeTracker(self)
        self.closed_trades = self.trade_tracker.closed_trades
        if risk_control_classes:
            # 如果传入的是单个类（兼容旧代码），转为列表
            if not isinstance(risk_control_classes, list):
                risk_control_classes = [risk_control_classes]

            for rc_cls in risk_control_classes:
                # 所有风控模块共享同一套 params (risk_control_params)
                # 它们会各自提取自己需要的参数
                self.risk_controls.append(rc_cls(broker=self, params=risk_control_params))
        self.strategy.init()

    def getcash(self):
        """代理调用真实 Broker 的 getcash"""
        return self.broker.getcash()

    def get_cash(self):
        """统一实盘与回测的获取现金接口"""
        return self.broker.getcash()

    def get_rebalance_cash(self):
        """返回回测调仓口径的现金，并扣除已知期权现金义务。"""
        return self.get_option_uncommitted_cash()

    def getvalue(self):
        """代理调用真实 Broker 的 getvalue，并启用全局缓存拦截"""
        if hasattr(self, 'current_portfolio_value'):
            return self.current_portfolio_value
        return self.broker.getvalue()

    def get_current_price(self, data):
        """统一实盘与回测的获取最新价格接口"""
        return data.close[0]

    def get_contract_multiplier(self, data):
        """返回 DataFeed 元数据声明的每份合约现金乘数。"""
        return _extract_contract_multiplier(data)

    def get_rebalance_position_value(self, data, signed_size, price, market_value):
        """返回调仓资金盘点使用的持仓价值，按 Broker 担保口径调整。"""
        if float(signed_size) < 0:
            try:
                from common.options.analytics import parse_option_symbol

                option_type = parse_option_symbol(getattr(data, '_name', '')).get('option_type')
                if option_type == 'PUT':
                    # Short Put 的指派现金已由 get_option_uncommitted_cash 扣除。
                    return 0.0
            except Exception:
                pass
        return float(market_value)

    def get_option_uncommitted_cash(self):
        """按已确认期权现金义务计算可用于新增仓位的现金。"""
        from common.options.cash import uncommitted_cash

        short_puts = []
        for data in self.datas:
            position = self.getposition(data)
            if position.size >= 0:
                continue
            dataframe = getattr(getattr(data, 'p', None), 'dataname', None)
            row = None
            if isinstance(dataframe, pd.DataFrame) and not dataframe.empty:
                current_dt = None
                try:
                    current_dt = pd.Timestamp(data.datetime.datetime(0))
                except Exception:
                    pass
                if current_dt is not None:
                    index = pd.to_datetime(dataframe.index, errors='coerce')
                    if getattr(index, 'tz', None) is not None and current_dt.tzinfo is None:
                        index = index.tz_localize(None)
                    elif getattr(index, 'tz', None) is None and current_dt.tzinfo is not None:
                        current_dt = current_dt.tz_localize(None)
                    visible = dataframe.loc[index <= current_dt]
                    row = visible.iloc[-1] if not visible.empty else None
            if row is None:
                try:
                    from common.options.analytics import parse_option_symbol

                    is_option = bool(
                        parse_option_symbol(getattr(data, '_name', '')).get('option_type')
                    )
                except Exception:
                    is_option = False
                if is_option:
                    # 已确认的 Short Put 在缺少当前可见元数据时不能被解释成
                    # 没有现金义务，否则会放大后续买入能力。
                    self._last_order_target_skip_reason = 'option_margin_unavailable'
                    return 0.0
                continue
            option_type = str(row.get('option_type', '')).upper()
            strike = row.get('strike', row.get('strike_price'))
            if option_type not in {'PUT', 'P'} or strike is None:
                try:
                    from common.options.analytics import parse_option_symbol
                    parsed = parse_option_symbol(getattr(data, '_name', ''))
                except Exception:
                    parsed = {}
                option_type = option_type if option_type in {'PUT', 'P'} else parsed.get('option_type', '')
                strike = strike if strike is not None else parsed.get('strike')
            if option_type not in {'PUT', 'P'} or strike is None:
                continue
            # 只有 Short Put 产生现金担保义务；Covered Call 的担保事实
            # 是标的股数，不应被误扣成 Strike×Multiplier 现金。
            if option_type in {'PUT', 'P'}:
                short_puts.append({
                    'strike': strike,
                    'contracts': abs(position.size),
                    'contract_multiplier': self.get_contract_multiplier(data),
                })
        pending_short_puts = []
        for reservation in self._option_cash_reservations.values():
            remaining = float(reservation.get('remaining', 0.0))
            if remaining <= 0:
                continue
            pending_item = {
                'strike': reservation['strike'],
                'remaining': remaining,
                'contract_multiplier': reservation['contract_multiplier'],
            }
            if reservation.get('required') is not None:
                pending_item['reserved_cash'] = reservation['required']
            pending_short_puts.append(pending_item)
        return float(uncommitted_cash(
            self.getcash(),
            short_puts=short_puts,
            pending_short_puts=pending_short_puts,
        ))

    def submit_option_order(self, data, volume, order_effect, price=None, **kwargs):
        """回测中提供显式期权效果入口；生命周期事件仍需单独建模。"""
        from common.options.contracts import (
            InvalidOptionOrderEffect,
            normalize_option_order_effect,
            validate_option_order_effect,
        )
        from common.options.risk import compute_option_margin

        def annotate(order, **extra):
            if order is None:
                return None
            payload = {
                "option_order_effect": effect,
            }
            for key in ("option_spread_id", "option_spread_leg"):
                value = kwargs.get(key)
                if value is not None:
                    payload[key] = value
            payload.update(extra)
            try:
                order.addinfo(**payload)
            except Exception:
                pass
            return order

        try:
            effect = normalize_option_order_effect(order_effect)
            quantity = float(volume)
            if not math.isfinite(quantity) or quantity <= 0:
                raise InvalidOptionOrderEffect('quantity must be positive')
            current_size = float(self.getposition(data).size)
            if effect == 'SELL_TO_OPEN' and current_size > 0:
                raise InvalidOptionOrderEffect(
                    'SELL_TO_OPEN cannot be submitted against a confirmed long position'
                )
            validate_option_order_effect(
                effect,
                current_size,
                quantity,
                allow_sell_to_open=effect == 'SELL_TO_OPEN',
            )
        except (InvalidOptionOrderEffect, TypeError, ValueError, OverflowError):
            self._last_order_target_skip_reason = 'unsupported_option_order_effect'
            return None

        if effect == 'BUY_TO_OPEN':
            try:
                execution_price = float(price if price is not None else data.close[0])
                multiplier = float(self.get_contract_multiplier(data))
                commission_ratio = float(self.broker.getcommissioninfo(data).p.commission)
                required = (
                    quantity
                    * execution_price
                    * multiplier
                    * (1.0 + self.slippage)
                    * (1.0 + commission_ratio)
                )
                if (
                    not math.isfinite(required)
                    or required < 0
                    or not math.isfinite(multiplier)
                    or multiplier <= 0
                    or self.get_option_uncommitted_cash() < required
                ):
                    self._last_order_target_skip_reason = 'option_cash_insufficient'
                    return None
            except (TypeError, ValueError, OverflowError):
                self._last_order_target_skip_reason = 'invalid_option_cash_requirement'
                return None

        if effect == 'SELL_TO_OPEN':
            risk_leg = kwargs.get('risk_leg')
            if risk_leg is None:
                self._last_order_target_skip_reason = 'option_risk_leg_missing'
                return None
            try:
                option_type = str(getattr(risk_leg, 'option_type', '')).upper()
                signed_quantity = float(getattr(risk_leg, 'signed_quantity'))
                strike = float(getattr(risk_leg, 'strike'))
                multiplier = float(getattr(risk_leg, 'contract_multiplier'))
                if not all(math.isfinite(value) for value in (strike, multiplier)):
                    raise ValueError('risk leg strike or multiplier is not finite')
                if strike <= 0 or multiplier <= 0:
                    raise ValueError('risk leg strike or multiplier is not positive')
                _validate_backtest_risk_leg(data, risk_leg)
                if signed_quantity >= 0 or abs(signed_quantity) < quantity:
                    raise ValueError('SELL_TO_OPEN risk leg must cover the requested short quantity')
                if option_type in {'PUT', 'P'}:
                    available = self.get_option_uncommitted_cash()
                    from common.options.cash import assignment_cash
                    required = float(assignment_cash(strike, quantity, multiplier))
                    margin_legs = kwargs.get('_spread_risk_legs')
                    if margin_legs:
                        # 价差开仓按定义风险占用现金；未配对空头仍走 CSP 全额指派。
                        required = float(compute_option_margin(
                            margin_legs,
                            cash=max(0.0, float(self.getcash())),
                            portfolio_margin=True,
                        ).margin_used)
                    if available < required:
                        # 保留旧 CSP skip reason，便于现有 recorder/策略诊断兼容。
                        self._last_order_target_skip_reason = 'csp_assignment_cash_insufficient'
                        return None
                elif option_type in {'CALL', 'C'}:
                    compute_option_margin(
                        [risk_leg],
                        cash=max(0.0, float(self.getcash())),
                        underlying_positions=kwargs.get('underlying_positions') or {},
                    )
                    required = 0.0
                else:
                    raise ValueError('unsupported short option type')
            except (TypeError, ValueError, OverflowError):
                self._last_order_target_skip_reason = 'invalid_option_risk_leg'
                return None
            except Exception:
                self._last_order_target_skip_reason = 'option_collateral_insufficient'
                return None
            order = self.sell(data=data, size=quantity, price=price)
            if order is None:
                return None
            annotate(
                order,
                option_assignment_cash=required,
                option_strike=strike,
                option_contract_multiplier=multiplier,
                option_requested_quantity=quantity,
            )
            if option_type in {'PUT', 'P'}:
                self._option_cash_reservations[str(order.ref)] = {
                    'strike': strike,
                    'contract_multiplier': multiplier,
                    'remaining': quantity,
                    'initial': quantity,
                    'data': data,
                    'required': required,
                }
            return order
        if effect == 'BUY_TO_CLOSE':
            return annotate(self.buy(data=data, size=quantity, price=price))
        if effect == 'BUY_TO_OPEN':
            return annotate(
                self.buy(data=data, size=quantity, price=price),
                option_contract_multiplier=float(self.get_contract_multiplier(data)),
            )
        if effect == 'SELL_TO_CLOSE':
            return annotate(self.sell(data=data, size=quantity, price=price))
        return None

    def submit_option_spread(self, legs, volume=1, **kwargs):
        """回测中同步模拟定义风险组合；不查询 live pending 或网络状态。"""
        if not isinstance(legs, (list, tuple)) or len(legs) != 2:
            self._last_order_target_skip_reason = 'invalid_option_spread'
            return None
        spread_seq = int(self.__dict__.get("_option_spread_seq", 0)) + 1
        self._option_spread_seq = spread_seq
        spread_id = f"spread:{spread_seq}"
        prepared = []
        try:
            quantity = float(volume)
            if not math.isfinite(quantity) or quantity <= 0:
                raise ValueError('invalid spread quantity')
            from common.options.analytics import parse_expiry, parse_option_symbol, underlying_key
            from common.options.risk import OptionRiskLeg, compute_option_margin

            risk_legs = []
            for leg in legs:
                if not isinstance(leg, dict):
                    raise ValueError('spread leg must be a dict')
                data = leg['data']
                effect = leg['effect']
                price = leg.get('price')
                risk_leg = leg.get('risk_leg')
                leg_volume = float(leg.get('volume', quantity))
                if not math.isfinite(leg_volume) or abs(leg_volume - quantity) > 1e-12:
                    raise ValueError('spread leg quantities must match')
                if risk_leg is not None:
                    if not isinstance(risk_leg, OptionRiskLeg):
                        raise ValueError('invalid spread risk leg')
                    risk_legs.append(risk_leg)
                leg_kwargs = {
                    key: value
                    for key, value in leg.items()
                    if key not in {"data", "effect", "price", "risk_leg", "volume"}
                }
                prepared.append((data, effect, price, risk_leg, leg_kwargs))
            if risk_legs:
                compute_option_margin(
                    risk_legs,
                    cash=max(0.0, float(self.getcash())),
                    portfolio_margin=True,
                )
        except Exception:
            self._last_order_target_skip_reason = 'invalid_option_spread'
            return None

        # 先对所有腿做静态预检查，避免第一条腿创建后才发现第二条腿
        # 资金/仓位边界不满足。回测撮合虽同步，但订单创建接口本身仍可能
        # 被测试替身拒绝，因此失败时还会取消本次已创建的订单。
        try:
            from common.options.contracts import normalize_option_order_effect, validate_option_order_effect

            total_buy_cost = 0.0
            opening = []
            for data, effect, price, risk_leg, leg_kwargs in prepared:
                normalized_effect = normalize_option_order_effect(effect)
                current = float(self.getposition(data).size)
                validate_option_order_effect(
                    normalized_effect,
                    current,
                    quantity,
                    allow_sell_to_open=normalized_effect == 'SELL_TO_OPEN',
                )
                if normalized_effect == 'SELL_TO_OPEN':
                    if risk_leg is None or abs(float(risk_leg.signed_quantity) + quantity) > 1e-12:
                        raise ValueError('short spread leg quantity does not match')
                    _validate_backtest_risk_leg(data, risk_leg)
                    opening.append((normalized_effect, risk_leg))
                elif normalized_effect == 'BUY_TO_OPEN' and risk_leg is not None:
                    if abs(float(risk_leg.signed_quantity) - quantity) > 1e-12:
                        raise ValueError('long spread leg quantity does not match')
                    _validate_backtest_risk_leg(data, risk_leg)
                    opening.append((normalized_effect, risk_leg))
                if normalized_effect == 'BUY_TO_OPEN':
                    execution_price = float(price if price is not None else data.close[0])
                    multiplier = float(self.get_contract_multiplier(data))
                    commission_ratio = float(self.broker.getcommissioninfo(data).p.commission)
                    cost = quantity * execution_price * multiplier * (1.0 + self.slippage) * (1.0 + commission_ratio)
                    if not math.isfinite(cost) or cost < 0:
                        raise ValueError('invalid spread buy cost')
                    total_buy_cost += cost
            if opening:
                short = next((leg for effect, leg in opening if effect == 'SELL_TO_OPEN'), None)
                long = next((leg for effect, leg in opening if effect == 'BUY_TO_OPEN'), None)
                if short is None or long is None:
                    raise ValueError('spread must contain one short Put and one long Put')
                if str(short.option_type).upper() not in {'PUT', 'P'} or str(long.option_type).upper() not in {'PUT', 'P'}:
                    raise ValueError('only Put spreads are supported')
                if underlying_key(short.underlying) != underlying_key(long.underlying):
                    raise ValueError('spread legs must share underlying')
                short_expiry = parse_expiry(short.expiry)
                long_expiry = parse_expiry(long.expiry)
                same_expiry = (
                    pd.notna(short_expiry)
                    and pd.notna(long_expiry)
                    and pd.Timestamp(short_expiry).normalize() == pd.Timestamp(long_expiry).normalize()
                )
                if short.strike <= long.strike or not same_expiry:
                    raise ValueError('spread strikes or expiry are invalid')
                if abs(float(short.contract_multiplier) - float(long.contract_multiplier)) > 1e-12:
                    raise ValueError('spread legs must share contract multiplier')
            else:
                # 平仓组合同样必须是同一标的、同一到期日、同一乘数的 Put
                # Spread。不能因为两条腿都是“平仓效果”就放行混合 Call、跨
                # 标的或无法识别的合约，否则会把策略错误送入组合撮合。
                effects = {effect for _data, effect, _price, _risk_leg, _leg_kwargs in prepared}
                if effects != {'BUY_TO_CLOSE', 'SELL_TO_CLOSE'}:
                    raise ValueError('spread must contain one BUY_TO_CLOSE and one SELL_TO_CLOSE leg')
                close_details = []
                for data, _effect, _price, _risk_leg, _leg_kwargs in prepared:
                    parsed = parse_option_symbol(getattr(data, '_name', ''))
                    dataframe = getattr(getattr(data, 'p', None), 'dataname', None)

                    def metadata_value(names):
                        if not isinstance(dataframe, pd.DataFrame) or dataframe.empty:
                            return None
                        for name in names:
                            if name not in dataframe.columns:
                                continue
                            for value in reversed(dataframe[name].tolist()):
                                try:
                                    if value is not None and not pd.isna(value):
                                        return value
                                except (TypeError, ValueError):
                                    if value is not None:
                                        return value
                        return None

                    metadata_type = str(
                        metadata_value(('option_type', 'right', 'cp', 'put_call')) or ''
                    ).strip().upper()
                    option_type = metadata_type or str(parsed.get('option_type') or '').upper()
                    if option_type not in {'PUT', 'P'}:
                        raise ValueError('only Put spreads are supported')
                    strike_raw = metadata_value(('strike', 'strike_price', 'exercise_price'))
                    strike = float(
                        strike_raw if strike_raw is not None else parsed.get('strike', float('nan'))
                    )
                    expiry_raw = metadata_value(('expiry', 'expiration', 'expiration_date'))
                    expiry = parse_expiry(
                        expiry_raw if expiry_raw is not None else parsed.get('expiry')
                    )
                    underlying = str(
                        metadata_value(('underlying', 'underlying_symbol', 'underlying_code'))
                        or parsed.get('underlying')
                        or ''
                    ).upper()
                    if (
                        not math.isfinite(strike) or strike <= 0
                        or pd.isna(expiry) or not underlying
                    ):
                        raise ValueError('closing spread option metadata is unavailable')
                    multiplier = float(self.get_contract_multiplier(data))
                    if not math.isfinite(multiplier) or multiplier <= 0:
                        raise ValueError('closing spread contract multiplier is unavailable')
                    close_details.append({
                        'effect': _effect,
                        'underlying': underlying_key(underlying),
                        'strike': strike,
                        'expiry': pd.Timestamp(expiry).normalize(),
                        'multiplier': multiplier,
                    })
                buy_close = next(item for item in close_details if item['effect'] == 'BUY_TO_CLOSE')
                sell_close = next(item for item in close_details if item['effect'] == 'SELL_TO_CLOSE')
                if buy_close['underlying'] != sell_close['underlying']:
                    raise ValueError('closing spread legs must share underlying')
                if buy_close['expiry'] != sell_close['expiry']:
                    raise ValueError('closing spread legs must share expiry')
                if abs(buy_close['multiplier'] - sell_close['multiplier']) > max(
                    1e-9, abs(buy_close['multiplier']) * 1e-9
                ):
                    raise ValueError('closing spread legs must share contract multiplier')
                # Put credit spread 的短腿执行价较高；平仓方向相反时，
                # BUY_TO_CLOSE 必须对应高执行价短腿。
                if buy_close['strike'] <= sell_close['strike']:
                    raise ValueError('closing Put spread strikes are invalid')
            if total_buy_cost > self.get_option_uncommitted_cash() + 1e-12:
                self._last_order_target_skip_reason = 'option_spread_cash_insufficient'
                return None
        except Exception:
            self._last_order_target_skip_reason = 'invalid_option_spread'
            return None

        orders = []
        initial_positions = {
            id(data): float(self.getposition(data).size)
            for data, _effect, _price, _risk_leg, _leg_kwargs in prepared
        }

        def rollback_created_orders():
            """组合任一腿提交失败时撤销并尽力回滚已产生的成交。"""
            for created in orders:
                try:
                    self.cancel(created)
                except Exception:
                    pass
            # 正常 Backtrader 回测订单在同一调用内尚未撮合；测试替身或
            # 自定义 broker 可能同步成交，此时用反向市价单恢复原始仓位。
            for data, effect, price, _risk_leg, _leg_kwargs in prepared:
                try:
                    current = float(self.getposition(data).size)
                    delta = current - initial_positions.get(id(data), current)
                    if abs(delta) <= 1e-12:
                        continue
                    if delta > 0:
                        self.sell(data=data, size=delta, price=price)
                    else:
                        self.buy(data=data, size=abs(delta), price=price)
                except Exception as exc:
                    log.error(f'[Backtest] option spread rollback failed: {exc}')

        spread_kwargs = dict(kwargs)
        if risk_legs:
            spread_kwargs['_spread_risk_legs'] = risk_legs
        for leg_index, (data, effect, price, risk_leg, leg_kwargs) in enumerate(prepared):
            order_kwargs = dict(spread_kwargs)
            order_kwargs.update(leg_kwargs)
            order_kwargs["option_spread_id"] = spread_id
            order_kwargs["option_spread_leg"] = leg_index
            order = self.submit_option_order(
                data,
                quantity,
                effect,
                price=price,
                risk_leg=risk_leg,
                **order_kwargs,
            )
            if order is None:
                self._last_order_target_skip_reason = 'option_spread_leg_rejected'
                rollback_created_orders()
                for created in orders:
                    try:
                        self._option_cash_reservations.pop(str(created.ref), None)
                    except Exception:
                        pass
                return None
            orders.append(order)
        return orders

    def getcommissioninfo(self, data):
        """代理调用真实 Broker 的 getcommissioninfo"""
        return self.broker.getcommissioninfo(data)

    def log(self, txt, dt=None):
        if self.verbose:
            dt = dt or self.datas[0].datetime.datetime(0)
            log.info(txt, dt=dt)

    def next(self):
        # 每次进入新的 K 线周期，重置预计释放资金为 0
        self.expected_freed_cash = 0.0
        # 每过一个K线，重置虚拟消费账本
        self.virtual_spent_cash = 0.0

        # 仅在回测中，缓存当前 Bar 的账户总价值
        # 避免在调仓循环中重复对所有持仓资产进行市值核算的 O(N) 操作
        self.current_portfolio_value = self.broker.getvalue()
        self.trade_tracker.update_active_lows()

        if self.actual_start_date is None:
            self.actual_start_date = self.datas[0].datetime.datetime(0)

        # 检查策略是否有挂单。
        # 注意：如果策略逻辑是多标的并发的，建议策略内部维护一个订单列表或字典，
        # 而不是依赖单一的 self.strategy.order 锁。
        # 这里保留原逻辑的兼容性，但建议您后续在策略类中改进订单管理。
        if hasattr(self.strategy, 'order') and self.strategy.order:
            return  # 策略有全局挂单锁，跳过逻辑

        # 1. 执行风控检查 (获取被风控接管的标的列表)
        risk_handled_symbols = self._check_risk_controls()

        # 2. 将风控状态注入策略
        # 策略在 next() 中可以通过 checking self.risk_handled_symbols 来决定
        # 是否要跳过对某些标的的操作
        self.strategy.risk_handled_symbols = risk_handled_symbols

        # 3. 始终执行策略逻辑
        # 即使 A 标的触发了止损，B 标的依然可能有信号需要处理
        self.strategy.next()

    def buy(self, *args, **kwargs):
        """
        重写 buy 方法，在下单瞬间记录决策
        """
        order = super().buy(*args, **kwargs)
        if order:
            self._log_decision(order)
        return order

    def sell(self, *args, **kwargs):
        """
        重写 sell 方法，在下单瞬间记录决策
        """
        order = super().sell(*args, **kwargs)
        if order:
            self._log_decision(order)
        return order

    def notify_order(self, order):
        reservation = self._option_cash_reservations.get(str(getattr(order, 'ref', '')))
        if reservation is not None:
            status = getattr(order, 'status', None)
            terminal = status in {
                getattr(order, 'Completed', object()),
                getattr(order, 'Canceled', object()),
                getattr(order, 'Expired', object()),
                getattr(order, 'Margin', object()),
                getattr(order, 'Rejected', object()),
            }
            if terminal:
                self._option_cash_reservations.pop(str(order.ref), None)
            else:
                executed = abs(float(getattr(order.executed, 'size', 0.0) or 0.0))
                requested = float(reservation.get('initial', reservation.get('remaining', 0.0)))
                reservation['remaining'] = max(0.0, requested - executed)
        self.trade_tracker.notify_order(order)

        for rc in self.risk_controls:
            rc.notify_order(OrderProxy(order))

        self.strategy.notify_order(OrderProxy(order))

    def notify_trade(self, trade):
        self.trade_tracker.notify_trade(trade)

        for rc in self.risk_controls:
            rc.notify_trade(TradeProxy(trade))

        self.strategy.notify_trade(TradeProxy(trade))

    def order_target_percent(self, data=None, target=0.0, **kwargs):
        data = data or self.datas[0]
        lot_size = normalize_quantity_step(kwargs.get('lot_size', config.LOT_SIZE))

        # 防守逻辑：如果该标的正在被风控接管，且策略试图买入，则拦截
        if hasattr(self.strategy, 'risk_handled_symbols'):
            if data._name in self.strategy.risk_handled_symbols and target > 0:
                self.log(f"IGNORED BUY order for {data._name} due to Risk Control Lock.")
                return None

        # 1. 获取当前持仓和价格
        pos_size = self.getposition(data).size
        price = data.close[0]

        if price <= 0:
            return None  # 价格异常，不操作

        # 2. 获取账户总价值 (现金 + 持仓市值)
        # self.broker.getvalue() 返回的是当前回测时刻的总资产
        portfolio_value = getattr(self, 'current_portfolio_value', self.broker.getvalue())

        # 3. 计算目标市值和目标股数
        target_value = portfolio_value * target
        unit_value = price * self.get_contract_multiplier(data)
        if unit_value <= 0:
            self._last_order_target_skip_reason = 'invalid_order_unit_value'
            return None
        expected_shares = target_value / unit_value

        # 4. 计算需要变化的股数
        delta_shares = expected_shares - pos_size

        # 5. 执行下单逻辑
        if delta_shares > 0:  # 买入
            # 获取可用现金
            # 可用现金 = 账户当前现金 + 本次循环中卖单预计回笼的资金
            # Short Put 指派义务是不可动用的现金；普通买单也不能穿透该隔离。
            current_cash = min(
                self.broker.getcash(),
                self.get_option_uncommitted_cash(),
            )
            total_purchasing_power = current_cash + self.expected_freed_cash - self.virtual_spent_cash

            # 估算包含手续费/滑点的最大购买量 (假设 commission 是比例，如 0.0003)
            commission_ratio = self.broker.getcommissioninfo(data).p.commission
            execution_cost_multiplier = (1 + self.slippage) * (1 + commission_ratio)
            max_buy_by_cash = total_purchasing_power / (unit_value * execution_cost_multiplier)

            # 取 目标买入量 和 现金最大买入量 的较小值
            shares_to_buy = min(delta_shares, max_buy_by_cash)

            # 向下取整到 lot_size
            if lot_size != 1:
                shares_to_buy = align_quantity_down(shares_to_buy, lot_size)
            else:
                shares_to_buy = int(shares_to_buy)  # 即使是美股也通常是整数股

            if shares_to_buy > 0:
                # 与 order_target_value 保持一致：同 Bar 内先记账，防止多标的连续买入穿透现金。
                estimated_cost = shares_to_buy * unit_value * execution_cost_multiplier
                self.virtual_spent_cash += estimated_cost
                return self.buy(data=data, size=shares_to_buy)

        elif delta_shares < 0:  # 卖出
            shares_to_sell = abs(delta_shares)

            # 如果目标是 0，通常意味着清仓
            if target == 0.0:
                if pos_size > 0:
                    self.expected_freed_cash += pos_size * unit_value
                # 如果是清仓，直接使用 close()，它会处理所有持仓
                # 注意：self.close() 内部逻辑可能不保证 100 整手，但在清仓时通常需要卖出所有零股
                # 如果需要严格整手卖出，可以使用下面的逻辑，但会残留零股
                return self.close(data=data)

            # 向下取整到 lot_size
            if lot_size != 1:
                shares_to_sell = align_quantity_down(shares_to_sell, lot_size)
            else:
                shares_to_sell = int(shares_to_sell)

            if shares_to_sell > 0:
                estimated_value = shares_to_sell * unit_value
                self.expected_freed_cash += estimated_value
                return self.sell(data=data, size=shares_to_sell)

        return None

    def order_target_value(self, data=None, target=0.0, **kwargs):
        """
        重写 order_target_value 以支持：
        1. A股整手 (Lot Size)
        2. 同Bar资金回笼 (Selling frees cash for Buying)
        3. 风控拦截 (Risk Control Lock)
        """
        data = data or self.datas[0]
        lot_size = normalize_quantity_step(kwargs.get('lot_size', config.LOT_SIZE))
        self._last_order_target_skip_reason = None

        # 0. 风控拦截：如果该标的正在被风控接管，且策略试图买入/持有，则拦截
        if hasattr(self.strategy, 'risk_handled_symbols'):
            # 如果目标金额 > 0，视为买入或维持持仓意图，予以拦截
            if data._name in self.strategy.risk_handled_symbols and target > 0:
                self.log(f"IGNORED order_target_value({target}) for {data._name} due to Risk Control Lock.")
                self._last_order_target_skip_reason = 'risk_control_lock'
                return None

        # 1. 获取当前持仓和价格
        pos_size = self.getposition(data).size
        price = data.close[0]

        if price <= 0:
            self._last_order_target_skip_reason = 'invalid_price'
            return None

        # 2. 计算目标股数 (核心区别：直接用 target_value / price)
        # target 参数即为目标市值 (Cash Value)
        unit_value = price * self.get_contract_multiplier(data)
        if unit_value <= 0:
            self._last_order_target_skip_reason = 'invalid_order_unit_value'
            return None
        expected_shares = target / unit_value

        # 3. 计算需要变化的股数
        delta_shares = expected_shares - pos_size

        # 4. 执行下单逻辑 (逻辑复用 order_target_percent)
        if delta_shares > 0:  # 买入
            # 获取可用现金 (含本次循环预计释放的资金)
            # Short Put 指派义务是不可动用的现金；普通买单也不能穿透该隔离。
            current_cash = min(
                self.broker.getcash(),
                self.get_option_uncommitted_cash(),
            )

            # 2. 计算动态购买力
            # 公式: 静态现金 + 卖出回笼 - [新增]本轮已花掉的钱
            total_purchasing_power = current_cash + self.expected_freed_cash - self.virtual_spent_cash

            # 估算最大购买力 (含手续费和回测滑点)。
            # 回测是同步成交模型，不能固定砍 5% 买力；否则 LOT_SIZE=1 时，现金足够买 1 股也可能被压成 0 股。
            commission_ratio = self.broker.getcommissioninfo(data).p.commission
            execution_cost_multiplier = (1 + self.slippage) * (1 + commission_ratio)

            if total_purchasing_power < 0:
                total_purchasing_power = 0

            max_buy_by_cash = total_purchasing_power / (unit_value * execution_cost_multiplier)

            # 取 目标量 和 现金上限 的较小值
            shares_to_buy = min(delta_shares, max_buy_by_cash)
            raw_shares_to_buy = shares_to_buy

            # 向下取整到 lot_size
            if lot_size != 1:
                shares_to_buy = align_quantity_down(shares_to_buy, lot_size)
            else:
                shares_to_buy = int(shares_to_buy)

            if shares_to_buy > 0:
                # 记账：这笔钱已经花出去了！
                # 估算花费 = 股数 * 价格 * (1+手续费)
                estimated_cost = shares_to_buy * unit_value * execution_cost_multiplier
                self.virtual_spent_cash += estimated_cost
                return self.buy(data=data, size=shares_to_buy)

            if delta_shares < lot_size:
                self._last_order_target_skip_reason = 'below_min_lot_delta'
            elif raw_shares_to_buy < lot_size:
                self._last_order_target_skip_reason = 'insufficient_cash_for_min_lot'

        elif delta_shares < 0:  # 卖出
            shares_to_sell = abs(delta_shares)

            # 如果目标价值是 0 或极小，视为清仓
            if target <= 1.0:  # 容忍浮点误差，小于1块钱视同清仓
                if pos_size > 0:
                    self.expected_freed_cash += pos_size * unit_value
                return self.close(data=data)

            # [关键] 向下取整到 lot_size
            if lot_size != 1:
                shares_to_sell = align_quantity_down(shares_to_sell, lot_size)
            else:
                shares_to_sell = align_quantity_down(shares_to_sell, lot_size)

            if shares_to_sell > 0:
                estimated_freed_value = shares_to_sell * unit_value
                self.expected_freed_cash += estimated_freed_value
                return self.sell(data=data, size=shares_to_sell)

            self._last_order_target_skip_reason = 'below_min_lot_delta'
        else:
            self._last_order_target_skip_reason = 'target_already_met'

        return None

    def _check_risk_controls(self) -> list:
        """
        辅助方法：检查所有标的，循环执行所有风控检查。
        """
        triggered_symbols = []

        # 如果没有风控模块，直接返回
        if not self.risk_controls:
            return triggered_symbols

        for data in self.datas:
            # 1. 检查是否有仓位
            if not self.getposition(data).size:
                continue

            # 2. 对持仓标的循环执行风控检查
            final_action = None
            for rc in self.risk_controls:
                action = rc.check(data)

                # 如果任意一个风控模块要求卖出
                if action == 'SELL':
                    final_action = 'SELL'
                    self.log(f"Risk module '{rc.__class__.__name__}' triggered SELL for {data._name}")
                    # 一旦触发平仓，通常不需要再问其他风控模块了，直接 Break
                    break

            # 3. 如果触发平仓
            if final_action == 'SELL':
                # 执行平仓
                order = self.order_target_percent(data=data, target=0.0)

                if hasattr(self.strategy, 'order'):
                    self.strategy.order = order

                triggered_symbols.append(data._name)

        return triggered_symbols

    def _log_decision(self, order):
        """
        辅助方法：立即记录交易决策
        """
        if not self.recorder:
            return

        try:
            action = 'BUY' if order.isbuy() else 'SELL'

            # 1. 获取决策时间 (当前 Bar 的时间)
            current_dt = order.data.datetime.datetime(0)

            # 2. 获取决策价格 (当前 Close)
            decision_price = order.data.close[0]

            # 3. 获取决策数量 (Created 中的 size)
            decision_size = order.created.size

            # 4. 估算手续费 (假定成交)
            comminfo = self.broker.getcommissioninfo(order.data)
            estimated_comm = comminfo.getcommission(decision_price, decision_size)

            # 5. 获取账户快照
            current_cash = self.broker.getcash()
            current_value = self.broker.getvalue()

            self.recorder.log_trade(
                dt=current_dt,
                symbol=order.data._name,
                action=action,
                price=decision_price,
                size=decision_size,
                comm=estimated_comm,
                order_ref=order.ref,
                cash=current_cash,
                value=current_value
            )
        except Exception as e:
            if self.verbose:
                print(f"Error logging decision: {e}")


class SignalLoggingBroker(bt.brokers.BackBroker):
    """
    继承自 Backtrader 原生 Broker，仅用于在回测时拦截下单信号并打印日志。
    """

    def buy(self, owner, data, size, price=None, **kwargs):
        # 训练/优化场景下 owner.verbose=False，直接静音避免刷屏。
        if size > 0 and getattr(owner, 'verbose', True):
            exec_price = price if price else data.close[0]
            # 获取回测当前时间
            current_dt = self.cerebro.datas[0].datetime.datetime(0)

            log.signal('BUY', data._name, size, exec_price, tag="回测信号", dt=current_dt)

        return super().buy(owner, data, size, price, **kwargs)

    def sell(self, owner, data, size, price=None, **kwargs):
        if size > 0 and getattr(owner, 'verbose', True):
            exec_price = price if price else data.close[0]
            current_dt = self.cerebro.datas[0].datetime.datetime(0)

            log.signal('SELL', data._name, size, exec_price, tag="回测信号", dt=current_dt)

        return super().sell(owner, data, size, price, **kwargs)

class Backtester:
    # 回测执行器
    def __init__(self, datas, strategy_class, params=None, start_date=None, end_date=None, cash=100000.0,
                 commission=0.0, slippage=0.001, sizer_class=None, sizer_params=None,
                 risk_control_classes=None, risk_control_params=None,
                 timeframe: str = 'Days', compression: int = 1,
                 recorder = None, enable_plot = True, verbose=True, indicator_cache=None,
                 plot_scope: str = 'full'):
        self.plot_scope = parse_plot_scopes(plot_scope)
        self.cerebro = create_cerebro(self.plot_scope, enable_plot=enable_plot)
        self.cerebro.broker = SignalLoggingBroker()
        self.datas = datas
        self.strategy_class = strategy_class
        self.params = params
        self.start_date = start_date
        self.end_date = end_date
        self.cash = float(cash) if cash is not None else 100000.0
        self.commission = float(commission) if commission is not None else 0.0003
        self.slippage = float(slippage) if slippage is not None else 0.001
        self.sizer_class = sizer_class
        self.sizer_params = sizer_params
        self.risk_control_classes = risk_control_classes
        self.risk_control_params = risk_control_params
        self.timeframe_str = timeframe
        self.compression = compression
        self.recorder = recorder
        self.enable_plot = enable_plot
        self.verbose = verbose
        self.indicator_cache = indicator_cache if isinstance(indicator_cache, dict) else {}
        # 优化器可在同一进程内复用首次运行后生成的对齐行情；签名不匹配时自动重建。
        self._prepared_datas = None
        self.timeframe = self._get_bt_timeframe(timeframe)

        self._init_analyzers()
        if self.enable_plot:
            configure_plot_observers(self.cerebro, self.plot_scope)

    def _get_bt_timeframe(self, timeframe_str: str) -> int:
        """将字符串时间维度映射到backtrader的TimeFrame枚举值"""
        mapping = {
            'Days': bt.TimeFrame.Days,
            'Weeks': bt.TimeFrame.Weeks,
            'Months': bt.TimeFrame.Months,
            'Minutes': bt.TimeFrame.Minutes,
            'Seconds': bt.TimeFrame.Seconds,
        }
        return mapping.get(timeframe_str, bt.TimeFrame.Days)

    def run(self):
        runtime_notifications.clear_deferred_plan()
        self._init_data_feeds()
        self._init_strategy()
        self._init_broker()

        self.log(f"Starting Portfolio Value: {self.cerebro.broker.getvalue():.2f}")

        self.results = self.cerebro.run()
        runtime_notifications.flush_deferred_plan()

        final_val = self.cerebro.broker.getvalue()
        self.log(f"Final Portfolio Value: {final_val:.2f}")

        self._push_backtest_performance_summary()
        self._process_recorder_hooks(final_val)
        self._generate_report()

        return self.results

    def log(self, msg):
        """安静模式下的静音处理"""
        if self.verbose:
            print(msg)

    def _init_data_feeds(self):
        """注册行情。短命期权对齐正股时钟；缺 K 与到期后补 0，避免把旧价当成当日报价。"""
        from common.options.analytics import parse_option_symbol

        start = pd.to_datetime(self.start_date) if self.start_date else None
        end = pd.to_datetime(self.end_date) if self.end_date else None
        underlyings = {}
        options = {}
        parsed_symbols = {}
        skipped_empty = 0

        def _naive_frame(df):
            frame = df
            if not isinstance(frame.index, pd.DatetimeIndex):
                frame = frame.copy(deep=False)
                frame.index = pd.to_datetime(frame.index, errors="coerce")
            if isinstance(frame.index, pd.DatetimeIndex) and frame.index.tz is not None:
                frame = frame.copy(deep=False)
                frame.index = frame.index.tz_convert("UTC").tz_localize(None)
            return frame[~frame.index.isna()] if isinstance(frame.index, pd.DatetimeIndex) else frame

        def _in_window(frame):
            visible = frame
            if start is not None:
                visible = visible[visible.index >= start]
            if end is not None:
                visible = visible[visible.index <= end]
            return visible

        for symbol, df in self.datas.items():
            if df is None or getattr(df, "empty", True):
                skipped_empty += 1
                continue
            frame = _naive_frame(df)
            visible = _in_window(frame)
            if visible.empty:
                skipped_empty += 1
                continue
            parsed = parse_option_symbol(symbol)
            parsed_symbols[symbol] = parsed
            if parsed.get("option_type"):
                options[symbol] = frame
            else:
                underlyings[symbol] = frame
        if skipped_empty:
            self.log(f"  Skipped {skipped_empty} empty data feeds outside the backtest window.")
        clock = None
        for frame in underlyings.values():
            visible = _in_window(frame)
            clock = visible.index if clock is None else clock.union(visible.index)
        if clock is not None:
            clock = clock.sort_values()
        clock_signature = None
        if clock is not None:
            try:
                clock_signature = (len(clock), clock.asi8.tobytes())
            except Exception:
                clock_signature = (len(clock), tuple(clock))
        clock_days = None
        if clock is not None:
            clock_days = pd.DatetimeIndex(pd.to_datetime(clock, errors="coerce"))
            if clock_days.tz is not None:
                clock_days = clock_days.tz_localize(None)
            clock_days = clock_days.normalize()
        alive_masks = {}
        prepared = {}
        for symbol in self.datas:
            if symbol in underlyings:
                prepared[symbol] = underlyings[symbol]
            elif symbol in options:
                frame = options[symbol]
                if clock is not None:
                    cached_signature = getattr(frame, "attrs", {}).get(
                        "_quantada_aligned_clock"
                    )
                    already_aligned = (
                        cached_signature == clock_signature
                        and isinstance(frame.index, pd.DatetimeIndex)
                        and frame.index.equals(clock)
                    )
                    aligned = frame if already_aligned else frame.reindex(clock)
                    expiry = parsed_symbols.get(symbol, {}).get("expiry")
                    expiry_day = None if expiry is None else pd.Timestamp(expiry)
                    if expiry_day is not None and not pd.isna(expiry_day):
                        expiry_day = expiry_day.tz_localize(None) if expiry_day.tzinfo is not None else expiry_day
                        expiry_day = expiry_day.normalize()
                    if expiry_day is not None:
                        mask_key = expiry_day.value
                        alive_mask = alive_masks.get(mask_key)
                        if alive_mask is None:
                            alive_mask = clock_days <= expiry_day
                            alive_masks[mask_key] = alive_mask
                    else:
                        alive_mask = None
                    if not already_aligned:
                        for column in ("open", "high", "low", "close", "bid", "ask", "bid_price", "ask_price", "last"):
                            if column not in aligned.columns:
                                continue
                            series = pd.to_numeric(aligned[column], errors="coerce")
                            if alive_mask is not None:
                                series = series.where(alive_mask, 0.0)
                            series = series.fillna(0.0)
                            aligned[column] = series
                        if "volume" in aligned.columns:
                            aligned["volume"] = pd.to_numeric(aligned["volume"], errors="coerce").fillna(0.0)
                        aligned.attrs["_quantada_aligned_clock"] = clock_signature
                    frame = aligned
                prepared[symbol] = frame
        self._prepared_datas = prepared
        for symbol, df in prepared.items():
            feed = bt.feeds.PandasData(
                dataname=df,
                fromdate=start,
                todate=end,
                name=symbol,
                timeframe=self.timeframe,
                compression=self.compression
            )
            apply_data_feed_plot_scope(feed, self.plot_scope)
            self.cerebro.adddata(feed)
            self.log(f"  Data feed for '{symbol}' added.")

    def _init_strategy(self):
        self.cerebro.addstrategy(
            BacktraderStrategyWrapper,
            strategy_class=self.strategy_class,
            params=self.params,
            risk_control_classes=self.risk_control_classes,
            risk_control_params=self.risk_control_params,
            recorder=self.recorder,
            verbose=self.verbose,  # 将静音标志传递给 wrapper
            slippage=self.slippage,
            indicator_cache=self.indicator_cache,
        )

    def _init_broker(self):
        self.cerebro.broker.setcash(self.cash)
        self.cerebro.broker.setcommission(commission=self.commission)

        # 期权/期货等每份合约对应多个基础单位时，回测柜台也必须按同一名义乘数扣款和估值。
        for data in self.cerebro.datas:
            multiplier = _extract_contract_multiplier(data)
            try:
                from common.options.analytics import parse_option_symbol

                parsed_option = parse_option_symbol(getattr(data, '_name', ''))
            except Exception:
                parsed_option = {}
            dataframe = getattr(getattr(data, 'p', None), 'dataname', None)
            metadata_option = isinstance(dataframe, pd.DataFrame) and any(
                name in dataframe.columns for name in ('option_type', 'right', 'cp', 'put_call')
            )
            if (parsed_option.get('option_type') or metadata_option) and multiplier <= 0:
                raise ValueError(
                    f"Option DataFeed {getattr(data, '_name', '')!r} requires a positive contract multiplier"
                )
            if multiplier == 1.0:
                continue
            self.cerebro.broker.addcommissioninfo(
                _ContractMultiplierCommInfo(
                    commission=self.commission,
                    mult=multiplier,
                    percabs=True,
                ),
                name=data._name,
            )

        # 开启 "Cheat-On-Close" (收盘作弊模式)
        # 作用：让 T 日发出的市价单，以 T 日的 Close 价成交。
        # 目的：模拟实盘在 14:45 (接近收盘) 的买入动作，消除 "次日低开红利" 的回测虚高。
        self.cerebro.broker.set_coc(True)

        # 关闭下单时的资金检查
        # 允许 "先卖后买" 的订单在资金未回笼时先提交进入队列
        # 只要在次日开盘执行顺序正确 (先卖出成交回款，再买入)，交易就能成功
        self.cerebro.broker.set_checksubmit(False)

        # 设置百分比滑点 (0.001 表示 0.1%)
        # 这会让买入价更高，卖出价更低，模拟真实市场的冲击成本
        if self.slippage > 0:
            self.cerebro.broker.set_slippage_perc(perc=self.slippage)

        if self.sizer_class:
            self.cerebro.addsizer(self.sizer_class, **self.sizer_params)

    def _init_analyzers(self):
        # 1. 基础指标 (优化器也需要)
        self.cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=0.0,
                                 timeframe=self.timeframe, compression=self.compression, factor=config.ANNUAL_FACTOR, annualize=True)
        self.cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
        self.cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
        self.cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='tradeanalyzer')
        self.cerebro.addanalyzer(
            bt.analyzers.TimeReturn,
            _name='timereturn_monthly',
            timeframe=bt.TimeFrame.Months,
        )

        # 2. 详细指标 (仅 Verbose 模式挂载，节省内存)
        if self.verbose:
            self.cerebro.addanalyzer(bt.analyzers.PyFolio, _name='pyfolio')

    def _process_recorder_hooks(self, final_val):
        """处理数据库/HTTP记录逻辑"""
        if not (self.recorder and self.recorder.active):
            return

        # 计算概要指标用于记录
        strat = self.results[0]
        sharpe = strat.analyzers.sharpe.get_analysis().get('sharperatio', 0.0) or 0.0
        max_dd = strat.analyzers.drawdown.get_analysis().max.drawdown / 100

        total_ret = (final_val / self.cash) - 1

        # 估算年化
        start_dt = pd.to_datetime(self.start_date) if self.start_date else datetime.datetime.now()
        end_dt = pd.to_datetime(self.end_date) if self.end_date else datetime.datetime.now()
        days = (end_dt - start_dt).days
        ann_ret = ((1 + total_ret) ** (365.0 / days)) - 1 if days > 0 else 0.0
        trade_analysis = strat.analyzers.tradeanalyzer.get_analysis()
        total_trades = trade_analysis.get('total', {}).get('total', 0)

        # 获取获利交易数
        win_trades = trade_analysis.get('won', {}).get('total', 0)

        # 计算胜率 (0.0 ~ 1.0)
        win_rate = (win_trades / total_trades) if total_trades > 0 else 0.0

        self.recorder.finish_execution(
            final_value=final_val, total_return=total_ret,
            sharpe=sharpe, max_drawdown=max_dd, annual_return=ann_ret,
            trade_count=total_trades, win_rate=win_rate  # 传入新增参数
        )

    def _push_backtest_performance_summary(self):
        if not getattr(config, "PRINT_PLAN", False):
            return False
        if hasattr(config, "is_alarms_enabled") and not config.is_alarms_enabled():
            return False

        try:
            metrics = self.get_performance_metrics()
            if not metrics:
                return False
            attribution_report = self.get_trade_micro_attribution_report()
            report = format_backtest_results_report(metrics, attribution_report=attribution_report)
            command = str(runtime_command.get_current_command() or "").strip()
            if command:
                report = f"### Backtest Command\n```bash\n{command}\n```\n{report}"
            return runtime_notifications.push_plan(report)
        except Exception as e:
            if self.verbose:
                print(f"[Backtester] Failed to push backtest performance summary: {e}")
            return False

    def _generate_report(self):
        """生成文字报告和图表"""
        if not self.verbose:
            return

        # 打印详细指标
        self.display_results()

        # 绘图
        if self.enable_plot:
            self.log("Generating plot...")
            try:
                plot_cerebro(self.cerebro, self.plot_scope)
            except Exception as e:
                err = str(e).lower()
                if "tkinter" in err or "tkagg" in err:
                    print("\n[Warning] Plotting Skipped: 'tkinter' missing. Use --no_plot on server.")
                else:
                    print(f"\n[Warning] Plotting Failed: {e}")

    def get_custom_metric(self, metric_name='sharpe'):
        """
        获取特定的回测指标，用于参数优化
        """
        if not hasattr(self, 'results') or not self.results:
            return -999.0

        strat = self.results[0]

        if metric_name == 'sharpe':
            s = strat.analyzers.sharpe.get_analysis().get('sharperatio')
            return s if s is not None else -999.0

        elif metric_name == 'return':
            return (self.cerebro.broker.getvalue() - self.cash) / self.cash

        elif metric_name == 'calmar':
            # Calmar = 年化收益 / 最大回撤
            # 1. 获取年化收益 (近似值)
            init_cash = self.cash
            final_cash = self.cerebro.broker.getvalue()

            # 简易年化计算
            s_dt = pd.to_datetime(self.start_date) if self.start_date else pd.to_datetime('20000101')
            e_dt = pd.to_datetime(self.end_date) if self.end_date else datetime.datetime.now()
            days = (e_dt - s_dt).days or 1

            total_ret = (final_cash / init_cash) - 1
            annual_ret = (1 + total_ret) ** (365.0 / days) - 1

            # 2. 获取最大回撤 (百分比，如 10% -> 0.1)
            dd_stats = strat.analyzers.drawdown.get_analysis()
            max_dd = dd_stats.get('max', {}).get('drawdown', 0) / 100.0

            if max_dd == 0: return annual_ret * 100 # 如果没有回撤，直接返回放大的收益率作为分数
            return annual_ret / abs(max_dd)

        elif metric_name == 'final_value':
            return self.cerebro.broker.getvalue()

        return 0.0

    def _to_naive_datetime(self, dt_obj):
        if dt_obj is None:
            return None
        if getattr(dt_obj, "tzinfo", None):
            return dt_obj.replace(tzinfo=None)
        return dt_obj

    def get_performance_metrics(self):
        """
        提取与终端回测报告一致的核心性能指标，供 Optimizer/外部流程复用。
        """
        if not self.results:
            return None

        strat = self.results[0]
        drawdown_analyzer = strat.analyzers.getbyname('drawdown')
        sharpe_analyzer = strat.analyzers.getbyname('sharpe')
        trade_analyzer = strat.analyzers.getbyname('tradeanalyzer')
        timereturn_monthly_analyzer = strat.analyzers.getbyname('timereturn_monthly')

        # 优先使用策略真实启动时间，避免 warm-up 污染统计区间
        start_date = None
        if hasattr(strat, 'actual_start_date') and strat.actual_start_date is not None:
            start_date = self._to_naive_datetime(strat.actual_start_date)

        start_candidates = []
        end_candidates = []
        for data in getattr(self.cerebro, 'datas', []):
            if len(data) <= 0:
                continue
            try:
                start_candidates.append(self._to_naive_datetime(data.datetime.datetime(-len(data) + 1)))
                end_candidates.append(self._to_naive_datetime(data.datetime.datetime(0)))
            except Exception:
                continue

        if start_date is None:
            if start_candidates:
                start_date = min(start_candidates)
            elif self.start_date:
                start_date = pd.to_datetime(self.start_date).to_pydatetime()

        if end_candidates:
            end_date = max(end_candidates)
        elif self.end_date:
            end_date = pd.to_datetime(self.end_date).to_pydatetime()
        else:
            end_date = datetime.datetime.now()

        if start_date is None or end_date is None:
            return None

        final_value = self.cerebro.broker.getvalue()
        total_return = (final_value / self.cash) - 1
        time_period_years = max((end_date - start_date).days / 365.25, 0.0)
        annual_return = ((1 + total_return) ** (1 / time_period_years) - 1) if time_period_years > 0 else 0.0

        sharpe_ratio = sharpe_analyzer.get_analysis().get('sharperatio', 0.0)
        if sharpe_ratio is None or (isinstance(sharpe_ratio, float) and math.isnan(sharpe_ratio)):
            sharpe_ratio = 0.0

        dd_analysis = drawdown_analyzer.get_analysis()
        try:
            max_drawdown = dd_analysis.max.drawdown / 100.0
        except Exception:
            max_drawdown = dd_analysis.get('max', {}).get('drawdown', 0.0) / 100.0

        calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

        trade_analysis = trade_analyzer.get_analysis()
        total_trades = trade_analysis.get('total', {}).get('total', 0)

        if 'won' in trade_analysis and trade_analysis.won.total > 0:
            win_trades = trade_analysis.won.total
            total_win_pnl = trade_analysis.won.pnl.total
            avg_win_pnl = trade_analysis.won.pnl.average
        else:
            win_trades = 0
            total_win_pnl = 0.0
            avg_win_pnl = 0.0

        if 'lost' in trade_analysis and trade_analysis.lost.total > 0:
            total_loss_pnl = trade_analysis.lost.pnl.total
            avg_loss_pnl = trade_analysis.lost.pnl.average
        else:
            total_loss_pnl = 0.0
            avg_loss_pnl = 0.0

        win_rate = (win_trades / total_trades) * 100 if total_trades > 0 else 0.0
        profit_factor = total_win_pnl / abs(total_loss_pnl) if total_loss_pnl != 0 else float('inf')
        pnl_ratio = avg_win_pnl / abs(avg_loss_pnl) if avg_loss_pnl != 0 else float('inf')

        monthly_returns = timereturn_monthly_analyzer.get_analysis()
        active_monthly_returns = [
            monthly_return for monthly_return in monthly_returns.values()
            if monthly_return is not None and abs(monthly_return) > 1e-12
        ]
        monthly_win_rate = (
            sum(1 for monthly_return in active_monthly_returns if monthly_return > 0) / len(active_monthly_returns)
            if active_monthly_returns else 0.0
        )

        return {
            "start_date": start_date,
            "end_date": end_date,
            "initial_portfolio": self.cash,
            "final_portfolio": final_value,
            "total_return": total_return,
            "annual_return": annual_return,
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown": max_drawdown,
            "calmar_ratio": calmar_ratio,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "monthly_win_rate": monthly_win_rate,
            "profit_factor": profit_factor,
            "pnl_ratio": pnl_ratio,
        }

    def get_closed_trades(self):
        """
        返回回测引擎暴露的已平仓交易列表（如果存在）。
        """
        candidates = [getattr(self, "closed_trades", None)]
        if getattr(self, "results", None):
            strat = self.results[0]
            candidates.append(getattr(strat, "closed_trades", None))
            candidates.append(getattr(getattr(strat, "strategy", None), "closed_trades", None))

        for candidate in candidates:
            if candidate is not None:
                return candidate
        return None

    def get_trade_micro_attribution_report(self):
        closed_trades = self.get_closed_trades()
        if closed_trades is None:
            return None
        return format_trade_micro_attribution_report(closed_trades)

    def display_results(self):
        """
        计算并展示详细的回测性能指标和图表。
        """
        metrics = self.get_performance_metrics()
        if not metrics:
            print("Backtest generated no valid performance metrics.")
            return

        attribution_report = self.get_trade_micro_attribution_report()
        print(format_backtest_results_report(metrics, attribution_report=attribution_report))
