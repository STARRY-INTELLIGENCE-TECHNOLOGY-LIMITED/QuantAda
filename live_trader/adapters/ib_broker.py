import asyncio
import datetime
import math
import re
import time

import pandas as pd
from ib_insync import IB, Stock, MarketOrder, Trade, Forex, Contract
try:
    from ib_insync import Crypto
except ImportError:
    Crypto = None

from alarms.manager import AlarmManager
from alarms.live_alarm import LiveAlarmDeduper
from common.log import coerce_dt
from common.ib_symbol_parser import resolve_ib_contract_spec
import config
from data_providers.csv_provider import CsvDataProvider
from data_providers.manager import DataManager
from data_providers.ibkr_provider import IbkrDataProvider
from ..data_bridge.data_warm import SchedulePlanner
from .base_broker import BaseLiveBroker, BaseOrderProxy


class IBOrderProxy(BaseOrderProxy):
    """IBKR 订单代理"""

    def __init__(self, trade: Trade, data=None):
        self.trade = trade
        self.data = data

    @property
    def id(self):
        return str(self.trade.order.orderId)

    @property
    def status(self):
        return self.trade.orderStatus.status

    @property
    def executed(self):
        class ExecutedStats:
            def __init__(self, trade):
                fill = trade.orderStatus
                self.size = fill.filled
                self.price = fill.avgFillPrice
                self.value = self.size * self.price
                # IBKR佣金信息在 commissionReport 对象中
                # 必须检查 commissionReport 是否存在，否则会报 AttributeError
                self.comm = 0.0
                if trade.fills:
                    try:
                        self.comm = sum(
                            (f.commissionReport.commission if f.commissionReport else 0.0)
                            for f in trade.fills
                        )
                    except AttributeError:
                        # 防御性编程：万一结构有变，默认为0不崩亏
                        self.comm = 0.0
                self.dt = IBOrderProxy._extract_execution_dt(trade)

        return ExecutedStats(self.trade)

    @staticmethod
    def _extract_execution_dt(trade):
        for fill in reversed(list(getattr(trade, 'fills', []) or [])):
            dt = coerce_dt(getattr(fill, 'time', None))
            if dt is not None:
                return dt

            execution = getattr(fill, 'execution', None)
            dt = coerce_dt(getattr(execution, 'time', None))
            if dt is not None:
                return dt

        return None

    def is_completed(self) -> bool:
        return self.trade.orderStatus.status == 'Filled'

    def is_canceled(self) -> bool:
        return self.trade.orderStatus.status in ['Cancelled', 'ApiCancelled']

    def is_rejected(self) -> bool:
        return self.trade.orderStatus.status == 'Inactive'  # 或者是 Rejected

    def is_pending(self) -> bool:
        return self.trade.orderStatus.status in ['Submitted', 'PreSubmitted', 'PendingSubmit', 'PendingCancel']

    def is_accepted(self) -> bool:
        # PreSubmitted 意味着已经被 IB 系统接收
        return self.trade.orderStatus.status in ['PreSubmitted', 'Submitted', 'Filled']

    def is_buy(self) -> bool:
        return self.trade.order.action == 'BUY'

    def is_sell(self) -> bool:
        return self.trade.order.action == 'SELL'


class IBDataProvider(IbkrDataProvider):
    """
    继承自 data_providers.ibkr_provider.IbkrDataProvider
    保留在当前模块定义，以便 engine.py 能够通过反射自动发现。
    """

    def get_history(self, symbol: str, start_date: str, end_date: str,
                    timeframe: str = 'Days', compression: int = 1) -> pd.DataFrame:
        """
        适配 engine.py 的接口调用
        直接透传调用父类的 get_data
        """
        return self.get_data(symbol, start_date, end_date, timeframe, compression)


class IBBrokerAdapter(BaseLiveBroker):
    """Interactive Brokers 适配器"""
    _PENDING_STATUSES = {
        'PENDINGSUBMIT',
        'APIPENDING',
        'PENDINGCANCEL',
        'PRESUBMITTED',
        'SUBMITTED',
    }
    _TERMINAL_STATUSES = {
        'FILLED',
        'CANCELLED',
        'APICANCELLED',
        'INACTIVE',
    }

    def __init__(self, context, cash_override=None, commission_override=None, slippage_override=None):
        # 从 context 中获取由 launch 注入的 ib 实例
        self.ib: IB = getattr(context, 'ib_instance', None)
        self._tickers = {}  # 缓存实时行情 snapshot
        self._fx_tickers = {}  # 缓存汇率行情
        # 最后已知有效汇率缓存 (Last Known Good Rate)
        self._last_valid_fx_rates = {}
        # 汇率历史查询失败冷却，防止单次故障导致每次 get_cash 都阻塞。
        self._fx_rate_retry_not_before = {}
        # 跨 client 的 open orders 拉取节流，避免高频路径反复 reqAllOpenOrders。
        self._req_all_open_orders_last_ts = 0.0
        self._req_all_open_orders_interval_s = 2.0
        self._live_alarm = LiveAlarmDeduper()
        self._last_account_snapshot_debug = {}
        # 无实时价兜底与告警去重
        self._price_data_manager = None
        self._delayed_market_data_enabled = False
        super().__init__(context, cash_override, commission_override, slippage_override)

    @property
    def safety_multiplier(self):
        """
        IB 资金预估使用更保守口径，降低 Error 201 概率。
        """
        return max(super().safety_multiplier, 1.05)

    @staticmethod
    def _normalize_account(account_raw) -> str:
        return str(account_raw or '').strip()

    @classmethod
    def _extract_account_from_obj(cls, obj, fields=('account', 'acctNumber')) -> str:
        if obj is None:
            return ''
        for field in fields:
            val = cls._normalize_account(getattr(obj, field, ''))
            if val:
                return val
        return ''

    def _configured_order_account(self) -> str:
        return self._normalize_account(getattr(config, 'IBKR_ORDER_ACCOUNT', ''))

    @staticmethod
    def _is_aggregate_account_marker(account: str) -> bool:
        return str(account or '').strip().upper() in {'ALL', 'ALLACCOUNTS', 'ALL_ACCOUNTS'}

    def _filter_account_scoped_items(self, items, account_getter):
        scoped_account = self._configured_order_account()
        raw_items = list(items or [])
        if not scoped_account:
            return raw_items

        paired = []
        has_account_info = False
        for item in raw_items:
            account = self._normalize_account(account_getter(item))
            if account:
                has_account_info = True
            paired.append((item, account))

        # 若当前数据源不携带账户字段，降级为不过滤，兼容旧结构。
        if not has_account_info:
            return raw_items

        filtered = [item for item, account in paired if account == scoped_account]
        if filtered:
            return filtered

        # accountSummary/accountValues 在部分 IB 会话下仅返回聚合账户标记（如 All），
        # 此时退化为不过滤，避免把有效账户误判为“快照为空”。
        non_empty_accounts = {account for _, account in paired if account}
        if non_empty_accounts and all(self._is_aggregate_account_marker(a) for a in non_empty_accounts):
            return raw_items

        return filtered

    @staticmethod
    def _empty_account_probe_debug(error_msg=None):
        return {
            'managed_accounts_raw': None,
            'managed_accounts_error': error_msg,
            'fallback_source': None,
        }

    @classmethod
    def _ingest_accounts_from_raw(cls, raw, target: set):
        if raw is None:
            return
        if isinstance(raw, str):
            candidates = raw.split(',')
        else:
            try:
                candidates = list(raw)
            except Exception:
                candidates = [raw]
        for c in candidates:
            acct = cls._normalize_account(c)
            if acct:
                target.add(acct)

    def _collect_known_accounts(self, with_debug=False):
        if not hasattr(self, 'ib') or not self.ib:
            debug = self._empty_account_probe_debug('ib instance missing')
            return (set(), debug) if with_debug else set()

        accounts = set()
        debug = self._empty_account_probe_debug(None)

        managed_accounts_getter = getattr(self.ib, 'managedAccounts', None)
        if callable(managed_accounts_getter):
            try:
                raw = managed_accounts_getter()
                debug['managed_accounts_raw'] = raw
                self._ingest_accounts_from_raw(raw, accounts)
            except Exception as e:
                debug['managed_accounts_error'] = repr(e)
        elif managed_accounts_getter is not None:
            debug['managed_accounts_raw'] = managed_accounts_getter
            self._ingest_accounts_from_raw(managed_accounts_getter, accounts)
        else:
            debug['managed_accounts_error'] = 'managedAccounts unavailable'

        # managedAccounts 为空时，回退到 accountSummary/accountValues 提取 account 字段。
        if not accounts:
            for getter_name in ('accountSummary', 'accountValues'):
                getter = getattr(self.ib, getter_name, None)
                if not callable(getter):
                    continue
                try:
                    for item in getter() or []:
                        acct = self._extract_account_from_obj(item)
                        if acct:
                            accounts.add(acct)
                except Exception:
                    continue
                if accounts:
                    debug['fallback_source'] = getter_name
                    break

        return (accounts, debug) if with_debug else accounts

    def _warn_missing_order_account_for_schedule_scope(self, known_accounts):
        alarm_key = self._live_alarm.schedule_key(
            'missing_order_account',
            schedule_rule=getattr(self._context, 'schedule_rule', ''),
            now=getattr(self, '_datetime', None) or getattr(self._context, 'now', None),
        )
        if self._live_alarm.seen(alarm_key):
            return

        accounts = sorted([a for a in (known_accounts or []) if a])
        msg = (
            "[IBBroker] Multiple accounts detected but IBKR_ORDER_ACCOUNT not set. "
            f"managedAccounts={accounts}. Orders are blocked to avoid wrong routing. "
            "Please set IBKR_ORDER_ACCOUNT to the intended account."
        )
        print(msg)
        try:
            AlarmManager().push_text(msg, level='ERROR')
        except Exception as e:
            print(f"[IBBroker Warning] failed to push missing account alarm: {e}")

    def _query_account_rows(self, method_name: str, configured_account: str, attempts_log: list, note: str = ''):
        method = getattr(self.ib, method_name, None)
        if not callable(method):
            attempts_log.append(f"{method_name} unavailable{note}")
            return []
        try:
            if configured_account:
                try:
                    rows = method(configured_account)
                    attempts_log.append(
                        f"{method_name}({configured_account}){note} -> {len(rows or [])}"
                    )
                    return list(rows or [])
                except TypeError:
                    rows = method()
                    attempts_log.append(
                        f"{method_name}() [fallback]{note} -> {len(rows or [])}"
                    )
                    return list(rows or [])
            rows = method()
            attempts_log.append(f"{method_name}(){note} -> {len(rows or [])}")
            return list(rows or [])
        except Exception as e:
            attempts_log.append(f"{method_name} failed{note}: {repr(e)}")
            return []

    def _is_configured_order_account_valid(self, configured_account: str) -> bool:
        account = self._normalize_account(configured_account)
        if not account:
            return True

        known_accounts = self._collect_known_accounts()
        if not known_accounts:
            print(
                f"[IBBroker] Skip order: unable to validate IBKR_ORDER_ACCOUNT='{account}' "
                f"(no managed/visible accounts from IB session)."
            )
            return False
        if account not in known_accounts:
            print(
                f"[IBBroker] Skip order: IBKR_ORDER_ACCOUNT='{account}' not in "
                f"managed/visible accounts={sorted(known_accounts)}."
            )
            return False
        return True

    def _push_zero_cash_account_alarm_if_needed(self, cash_value: float, has_snapshot: bool):
        account = self._configured_order_account()
        if not account:
            return

        snapshot_debug = getattr(self, '_last_account_snapshot_debug', {}) or {}
        if snapshot_debug.get('ib_connected') is False:
            # 启动/重连窗口内 IB 尚未连接时，不推送“账号金额为0”告警，避免误报。
            return

        try:
            cash_num = float(cash_value)
        except Exception:
            cash_num = 0.0

        dedupe_key = f"zero_cash_account:{account}"
        if cash_num > 0 and math.isfinite(cash_num):
            self._live_alarm.forget(dedupe_key)
            return

        known_accounts, accounts_debug = self._collect_known_accounts(with_debug=True)
        # 账号已在当前会话 managedAccounts 中，说明账号ID本身大概率有效；
        # 避免继续推送“可能填写错误”的误导性告警。
        if known_accounts and account in known_accounts:
            return

        if self._live_alarm.seen(dedupe_key):
            return

        reason = (
            "账户快照为空（过滤后无匹配记录）"
            if not has_snapshot else
            "账户可用资金计算结果为 0"
        )
        if known_accounts:
            account_hint = (
                f"当前会话 managedAccounts={sorted(known_accounts)} 未包含该账号，"
                "账号ID很可能填写错误；也可能是当前会话未启用该账号可见性。"
            )
        else:
            account_hint = (
                "当前会话未返回 managedAccounts，暂无法直接校验账号ID；"
                "请核对 IBKR_ORDER_ACCOUNT 与 TWS/Gateway 登录账户。"
            )
        warn_msg = (
            f"[IBBroker Warning] 检测到 IBKR_ORDER_ACCOUNT='{account}' 的账户资金为 0。"
            f"原因: {reason}。"
            f"{account_hint}"
        )
        print(warn_msg)
        self._log_account_probe_debug_once(account, known_accounts, accounts_debug)
        try:
            AlarmManager().push_text(warn_msg, level='ERROR')
        except Exception as e:
            print(f"[IBBroker Warning] failed to push zero-cash alarm: {e}")

    def _log_account_probe_debug_once(self, account: str, known_accounts: set, accounts_debug: dict):
        acct = self._normalize_account(account)
        if not acct:
            return
        if self._live_alarm.seen(f"account_probe_debug:{acct}"):
            return

        snapshot_debug = getattr(self, '_last_account_snapshot_debug', {}) or {}
        managed_raw = accounts_debug.get('managed_accounts_raw')
        managed_error = accounts_debug.get('managed_accounts_error')
        fallback_source = accounts_debug.get('fallback_source')
        msg = (
            f"[IBBroker Debug] Account probe for '{acct}': "
            f"managedAccounts_raw={managed_raw!r}, "
            f"managedAccounts_error={managed_error!r}, "
            f"fallback_source={fallback_source!r}, "
            f"known_accounts={sorted(known_accounts)}, "
            f"snapshot_ib_connected={snapshot_debug.get('ib_connected')}, "
            f"snapshot_in_async={snapshot_debug.get('in_async_task')}, "
            f"snapshot_summary_attempts={snapshot_debug.get('summary_attempts', [])}, "
            f"snapshot_values_attempts={snapshot_debug.get('values_attempts', [])}, "
            f"snapshot_raw_accounts={snapshot_debug.get('raw_accounts', [])}, "
            f"snapshot_raw_rows={snapshot_debug.get('raw_rows', 0)}, "
            f"snapshot_filtered_rows={snapshot_debug.get('filtered_rows', 0)}."
        )
        print(msg)

    def _load_account_snapshot(self):
        """
        拉取账户摘要快照；优先 accountSummary，失败时回退 accountValues。
        在异步回调任务中避免阻塞式等待。
        """
        if not hasattr(self, 'ib') or not self.ib:
            self._last_account_snapshot_debug = {
                'ib_connected': False,
                'in_async_task': False,
                'configured_account': self._configured_order_account(),
                'summary_attempts': ['ib instance missing'],
                'values_attempts': [],
                'raw_accounts': [],
                'raw_rows': 0,
                'filtered_rows': 0,
            }
            return []

        ib_connected = False
        try:
            ib_connected = bool(self.ib.isConnected())
        except Exception:
            ib_connected = False
        if not ib_connected:
            self._last_account_snapshot_debug = {
                'ib_connected': False,
                'in_async_task': self._in_async_task(),
                'configured_account': self._configured_order_account(),
                'summary_attempts': ['ib not connected'],
                'values_attempts': [],
                'raw_accounts': [],
                'raw_rows': 0,
                'filtered_rows': 0,
            }
            return []

        in_loop = self._in_async_task()
        source_data = []
        configured_account = self._configured_order_account()
        debug_info = {
            'ib_connected': True,
            'in_async_task': in_loop,
            'configured_account': configured_account,
            'summary_attempts': [],
            'values_attempts': [],
            'raw_accounts': [],
            'raw_rows': 0,
            'filtered_rows': 0,
        }

        if not in_loop:
            source_data = self._query_account_rows(
                'accountSummary', configured_account, debug_info['summary_attempts']
            )
            if not source_data and hasattr(self.ib, 'sleep'):
                self.ib.sleep(0.5)
                source_data = self._query_account_rows(
                    'accountSummary', configured_account, debug_info['summary_attempts'], note=' [retry]'
                )

        if not source_data:
            source_data = self._query_account_rows(
                'accountValues', configured_account, debug_info['values_attempts']
            )

        raw_source = list(source_data or [])
        debug_info['raw_rows'] = len(raw_source)
        raw_accounts = {
            self._extract_account_from_obj(item)
            for item in raw_source
            if self._extract_account_from_obj(item)
        }
        debug_info['raw_accounts'] = sorted(raw_accounts)
        filtered = self._filter_account_scoped_items(
            raw_source,
            lambda item: self._extract_account_from_obj(item),
        )
        debug_info['filtered_rows'] = len(filtered)
        self._last_account_snapshot_debug = debug_info
        return filtered

    @staticmethod
    def _extract_base_tag_value(source_data, tag):
        for v in source_data:
            if getattr(v, 'tag', None) != tag or getattr(v, 'currency', None) != 'BASE':
                continue
            try:
                return float(v.value)
            except Exception:
                continue
        return None

    def _fetch_order_available_cash(self) -> float:
        """
        下单可用现金口径（保守）：
        1) AvailableFunds BASE（IB 端已换汇口径）
        2) AvailableFunds FX 聚合（本地换汇口径）
        两者取更保守值；若无 AvailableFunds，则回退 TotalCashValue。
        """
        source_data = self._load_account_snapshot()
        if not source_data:
            self._push_zero_cash_account_alarm_if_needed(0.0, has_snapshot=False)
            return 0.0

        has_available_funds = any(
            getattr(v, 'tag', None) == 'AvailableFunds' for v in source_data
        )
        if has_available_funds:
            base_available = self._extract_base_tag_value(source_data, 'AvailableFunds')
            fx_available = self._fetch_smart_value(['AvailableFunds'], source_data=source_data)

            candidates = []
            if base_available is not None and math.isfinite(base_available):
                candidates.append(float(base_available))
            try:
                fx_available = float(fx_available)
                if math.isfinite(fx_available):
                    candidates.append(fx_available)
            except Exception:
                pass

            if candidates:
                result = min(candidates)
                self._push_zero_cash_account_alarm_if_needed(result, has_snapshot=True)
                return result

        result = self._fetch_smart_value(['TotalCashValue'], source_data=source_data)
        self._push_zero_cash_account_alarm_if_needed(result, has_snapshot=True)
        return result

    def _fetch_real_cash(self) -> float:
        """
        [必须实现] 基类要求的底层查钱接口
        用于初始化(_init_cash)和资金同步(sync_balance)
        """
        # 下单可用资金优先以 AvailableFunds 双口径取保守值。
        return self._fetch_order_available_cash()

    def getcash(self):
        """兼容 Backtrader 标准接口: 获取可用资金 (Buying Power)"""
        return self.get_cash()

    def get_rebalance_cash(self):
        """
        调仓计划资金口径（保守）:
        - get_cash: 可立即下单资金（AvailableFunds 语义）
        - TotalCashValue: 账户现金口径（不引入额外杠杆）
        计划层使用两者更保守者，避免“计划金额远大于可成交金额”。
        """
        spendable_cash = self.get_cash()
        total_cash_value = self._fetch_smart_value(['TotalCashValue'])
        try:
            spendable_cash = float(spendable_cash)
        except Exception:
            spendable_cash = 0.0
        try:
            total_cash_value = float(total_cash_value)
        except Exception:
            return spendable_cash

        if not math.isfinite(total_cash_value):
            return spendable_cash
        return min(spendable_cash, total_cash_value)

    def get_cash(self):
        """
        覆盖 BaseLiveBroker 的 get_cash，确保策略层调用的是真实的可用购买力。
        由于盈透的 TotalCashValue 在挂单阶段不会扣减，
        此处必须手动减去在途买单(Pending BUY)的预期消耗，防止产生无限杠杆幻觉。
        """
        # 1. 获取物理账面现金
        # 使用 AvailableFunds BASE/FX 双口径后的保守值，降低换汇边界导致的拒单。
        raw_cash = self._fetch_order_available_cash()

        # 2. 盘点所有在途买单，计算尚未物理扣款的“虚拟冻结资金”
        virtual_frozen_cash = 0.0
        covered_local_order_ids = set()
        try:
            pending_orders = self.get_pending_orders()
            for po in pending_orders:
                if po['direction'] == 'BUY':
                    poid = str(po.get('id', '')).strip()
                    symbol = po['symbol']
                    size = po['size']

                    price = 0.0
                    # 方案 A：优先从框架维护的数据流 (datas) 中精准提取最新价
                    for d in self.datas:
                        # 兼容 'AAPL.SMART' 和 'AAPL' 的命名匹配
                        if symbol == d._name or symbol == d._name.split('.')[0]:
                            price = self.get_current_price(d)
                            break

                    # 方案 B：如果 datas 未命中，从 IB 实时行情快照 _tickers 兜底获取
                    if price == 0.0 and symbol in self._tickers:
                        ticker = self._tickers[symbol]
                        p = ticker.marketPrice()
                        # 规避 NaN 或 0 等无效报价，启用盘前/周末休市兜底
                        if not (p and p > 0):
                            p = ticker.close if (ticker.close and ticker.close > 0) else ticker.last
                        if p and p > 0:
                            price = p

                    # 如果成功获取价格，累加冻结金额（使用统一安全垫）
                    if price > 0:
                        virtual_frozen_cash += size * price * self.safety_multiplier
                        if poid:
                            covered_local_order_ids.add(poid)

        except Exception as e:
            print(f"[IBBroker] 计算买单虚拟冻结资金时发生异常: {e}")

        # 3. 与本地占资做去重合并:
        # - active_buys_total: 基于 _active_buys 逐单估算的本地占资
        # - local_virtual_total: 与 _virtual_spent_cash 取大，兼容异常恢复场景
        # - overlap_cost: 已被 openTrades 成功估算覆盖的本地订单成本
        # 最终占资 = openTrades 冻结 + (本地占资 - 重叠部分)
        active_buys_total = 0.0
        overlap_cost = 0.0
        for oid, buy_info in getattr(self, '_active_buys', {}).items():
            try:
                cost = buy_info['shares'] * buy_info['price'] * self.safety_multiplier
            except Exception:
                continue
            active_buys_total += cost
            if str(oid) in covered_local_order_ids:
                overlap_cost += cost

        local_virtual_total = max(getattr(self, '_virtual_spent_cash', 0.0), active_buys_total)
        local_virtual_extra = max(0.0, local_virtual_total - overlap_cost)

        reserved_cash = virtual_frozen_cash + local_virtual_extra
        real_available_cash = raw_cash - reserved_cash
        return max(0.0, real_available_cash)

    def getvalue(self):
        """
        兼容 Backtrader 标准接口: 获取账户总权益 (NetLiquidation)
        """
        # 明确获取净清算价值
        return self._fetch_smart_value(['NetLiquidation'])

    @staticmethod
    def _safe_order_id(trade) -> str:
        try:
            oid_raw = getattr(getattr(trade, 'order', None), 'orderId', '')
            oid = str(oid_raw or '').strip()
            if not oid:
                return ''
            try:
                # IB 绑定手工单时可能分配负数 orderId（clientId=0 场景），
                # 只应把 0 视为无效。
                if int(float(oid)) == 0:
                    return ''
            except Exception:
                pass
            return oid
        except Exception:
            return ''

    @staticmethod
    def _safe_perm_id(trade) -> str:
        try:
            order_perm = getattr(getattr(trade, 'order', None), 'permId', '')
            status_perm = getattr(getattr(trade, 'orderStatus', None), 'permId', '')
            perm_raw = order_perm if order_perm not in (None, '') else status_perm
            perm = str(perm_raw or '').strip()
            if not perm:
                return ''
            try:
                if int(float(perm)) <= 0:
                    return ''
            except Exception:
                pass
            return perm
        except Exception:
            return ''

    @staticmethod
    def _in_async_task() -> bool:
        try:
            loop = asyncio.get_running_loop()
            return asyncio.current_task(loop=loop) is not None
        except RuntimeError:
            return False
        except Exception:
            return False

    @staticmethod
    def _extract_price_from_ticker(ticker) -> float:
        if not ticker:
            return 0.0
        try:
            price = ticker.marketPrice()
            if price and price > 0 and price == price:
                return float(price)
        except Exception:
            pass
        for field in ('close', 'last'):
            try:
                val = getattr(ticker, field, None)
                if val and val > 0 and val == val:
                    return float(val)
            except Exception:
                continue
        return 0.0

    def _try_enable_delayed_market_data(self, reason: str = '') -> bool:
        if self._delayed_market_data_enabled:
            return True
        if not hasattr(self, 'ib') or not self.ib:
            return False
        switcher = getattr(self.ib, 'reqMarketDataType', None)
        if not callable(switcher):
            return False
        try:
            switcher(3)  # delayed
            self._delayed_market_data_enabled = True
            reason_msg = f" ({reason})" if reason else ""
            print(f"[IB Warning] Realtime quote unavailable, switched to delayed market data{reason_msg}.")
            return True
        except Exception as e:
            print(f"[IB Warning] Failed to switch to delayed market data: {e}")
            return False

    def _resubscribe_symbol_ticker(self, symbol: str):
        if not hasattr(self, 'ib') or not self.ib:
            return None
        try:
            contract = self.parse_contract(symbol)
            self.ib.qualifyContracts(contract)
            ticker = self.ib.reqMktData(contract, '', False, False)
            self._tickers[symbol] = ticker
            return ticker
        except Exception:
            return self._tickers.get(symbol)

    def _try_get_delayed_quote(self, symbol: str):
        was_delayed_mode = bool(self._delayed_market_data_enabled)
        if not self._try_enable_delayed_market_data(reason=f"{symbol} no realtime quote"):
            return 0.0

        ticker = self._tickers.get(symbol)
        if ticker is None or not was_delayed_mode:
            ticker = self._resubscribe_symbol_ticker(symbol) or ticker
        for _ in range(20):
            price = self._extract_price_from_ticker(ticker)
            if price > 0:
                return price
            self._sleep_ib(0.05)
        return self._extract_price_from_ticker(ticker)

    def _safe_pending_id(self, trade) -> str:
        oid = self._safe_order_id(trade)
        if oid:
            return oid
        perm = self._safe_perm_id(trade)
        if perm:
            return f"perm:{perm}"
        return ''

    def _match_pending_id(self, trade, pending_id: str) -> bool:
        target = str(pending_id or '').strip()
        if not target:
            return False

        oid = self._safe_order_id(trade)
        perm = self._safe_perm_id(trade)

        if target.lower().startswith('perm:'):
            return bool(perm and target.split(':', 1)[1] == perm)
        if oid and target == oid:
            return True
        if perm and target == perm:
            return True
        return False

    def _extract_trade_account(self, trade) -> str:
        for obj in (getattr(trade, 'order', None), getattr(trade, 'orderStatus', None), trade):
            acct = self._extract_account_from_obj(obj)
            if acct:
                return acct
        return ''

    def _connected_client_id(self):
        try:
            return int(getattr(getattr(self.ib, 'client', None), 'clientId', -1))
        except Exception:
            return -1

    def _try_bind_manual_order_and_resolve(self, perm_id: str):
        """
        尝试将手工单绑定到当前 API 视角并获取可撤的有效 orderId。
        说明:
        - clientId=0 时，reqAutoOpenOrders/reqOpenOrders 可帮助绑定手工单。
        - 非 0 clientId 下通常无法绑定，调用后返回 None。
        """
        pid = str(perm_id or '').strip()
        if not pid:
            return None

        try:
            if hasattr(self.ib, 'reqOpenOrders'):
                self.ib.reqOpenOrders()
        except Exception as e:
            print(f"[IBBroker] reqOpenOrders bind attempt failed: {e}")

        for t in self._collect_open_trades(force_refresh_all=True):
            if self._safe_perm_id(t) != pid:
                continue
            if self._safe_order_id(t):
                return t
        return None

    def _push_manual_bind_alarm_once(self, pending_id: str, perm_id: str, client_id: int, reason: str):
        key = f"manual_bind:{client_id}:{perm_id or pending_id}"
        if self._live_alarm.seen(key):
            return

        if client_id != 0:
            hint = "Manual TWS order cleanup may require IBKR_CLIENT_ID=0."
        else:
            hint = (
                "clientId is already 0. Please verify TWS API manual-order binding settings "
                "(for example, auto-bind/negative order-id mapping)."
            )

        warn_msg = (
            f"[IBBroker Warning] cancel_pending_order {reason} "
            f"(pending_id={pending_id}, permId={perm_id}, clientId={client_id}). "
            f"{hint}"
        )
        print(warn_msg)
        try:
            AlarmManager().push_text(warn_msg, level='ERROR')
        except Exception as e:
            print(f"[IBBroker Warning] failed to push manual-bind alarm: {e}")

    def _sleep_ib(self, seconds: float):
        wait_s = 0.0
        try:
            wait_s = max(0.0, float(seconds))
        except Exception:
            wait_s = 0.0
        if wait_s <= 0:
            return

        if hasattr(self, 'ib') and self.ib and hasattr(self.ib, 'sleep') and not self._in_async_task():
            try:
                self.ib.sleep(wait_s)
                return
            except Exception:
                pass
        time.sleep(wait_s)

    def _find_trade_by_pending_id(self, pending_id: str):
        for t in self._collect_open_trades(force_refresh_all=True, include_trade_cache=False):
            if self._match_pending_id(t, pending_id):
                return t
        return None

    def _is_trade_still_pending(self, trade) -> bool:
        status = getattr(trade, 'orderStatus', None)
        if not status:
            return False

        raw_status = str(getattr(status, 'status', '') or '').strip().upper()
        try:
            rem = float(getattr(status, 'remaining', 0) or 0)
        except Exception:
            rem = 0.0

        if raw_status in self._TERMINAL_STATUSES:
            return False
        if raw_status:
            if raw_status not in self._PENDING_STATUSES:
                return False
            return rem > 0
        return rem > 0

    def _confirm_cancel_effective(self, pending_id: str, max_checks=3, sleep_seconds=0.15) -> bool:
        try:
            checks = max(1, int(max_checks))
        except Exception:
            checks = 1
        for idx in range(checks):
            trade = self._find_trade_by_pending_id(pending_id)
            if trade is None:
                return True
            if not self._is_trade_still_pending(trade):
                return True
            if idx < checks - 1:
                self._sleep_ib(sleep_seconds)
        return False

    def _collect_open_trades(self, force_refresh_all=False, include_trade_cache=False):
        """
        汇总当前可见的 open trades:
        - openTrades(): 当前客户端视角
        - reqAllOpenOrders(): 跨 client 快照（节流）
        - trades(): 本地缓存兜底（默认关闭，避免引入终态陈旧单）
        """
        if not hasattr(self, 'ib') or not self.ib or not self.ib.isConnected():
            return []

        collected = []
        seen = set()

        def _append(items):
            for t in items or []:
                oid = self._safe_order_id(t)
                key = oid if oid else str(id(t))
                if key in seen:
                    continue
                seen.add(key)
                collected.append(t)

        try:
            _append(self.ib.openTrades())
        except Exception as e:
            print(f"[IBBroker] openTrades 拉取失败: {e}")

        can_refresh_all = hasattr(self.ib, 'reqAllOpenOrders')
        if can_refresh_all:
            # ib_insync 在异步回调任务中直接 reqAllOpenOrders 可能触发
            # "This event loop is already running"，该场景退化为用本地可见缓存。
            if self._in_async_task():
                can_refresh_all = False
        if can_refresh_all:
            now_ts = time.monotonic()
            should_refresh = force_refresh_all or (
                now_ts - self._req_all_open_orders_last_ts >= self._req_all_open_orders_interval_s
            )
            if should_refresh:
                self._req_all_open_orders_last_ts = now_ts
                try:
                    _append(self.ib.reqAllOpenOrders())
                except Exception as e:
                    print(f"[IBBroker] reqAllOpenOrders 拉取失败: {e}")

        if include_trade_cache and hasattr(self.ib, 'trades'):
            try:
                _append(self.ib.trades())
            except Exception as e:
                print(f"[IBBroker] trades 缓存读取失败: {e}")

        return self._filter_account_scoped_items(
            collected,
            lambda trade: self._extract_trade_account(trade),
        )

    def get_pending_orders(self) -> list:
        """盈透：获取在途订单（含跨 client 兜底视角）"""
        if not hasattr(self, 'ib') or not self.ib or not self.ib.isConnected():
            return []

        res = []
        try:
            open_trades = self._collect_open_trades(force_refresh_all=False, include_trade_cache=False)
            for t in open_trades:
                order = getattr(t, 'order', None)
                status = getattr(t, 'orderStatus', None)
                contract = getattr(t, 'contract', None)
                if not order or not status:
                    continue

                try:
                    rem = float(getattr(status, 'remaining', 0) or 0)
                except Exception:
                    rem = 0.0
                raw_status = str(getattr(status, 'status', '') or '').strip()
                norm_status = raw_status.upper()

                # 优先按 IB 状态机判定：
                # - 终态直接排除，避免“已撤单但 remaining 未及时归零”导致误判在途。
                # - 有状态且非 pending 态时保守排除。
                # - 无状态才回退到 remaining>0 规则。
                if norm_status in self._TERMINAL_STATUSES:
                    continue
                if norm_status:
                    if norm_status not in self._PENDING_STATUSES:
                        continue
                    if rem <= 0:
                        continue
                else:
                    if rem <= 0:
                        continue

                symbol = str(getattr(contract, 'symbol', '') or '').strip()
                oid = self._safe_pending_id(t)
                action = str(getattr(order, 'action', '') or '').strip().upper()
                res.append({
                    'id': oid,
                    'symbol': symbol,
                    'direction': 'BUY' if action == 'BUY' else 'SELL',
                    'size': rem
                })
        except Exception as e:
            print(f"[IBBroker] 获取在途订单失败: {e}")
        return res

    def cancel_pending_order(self, order_id: str) -> bool:
        """盈透：按委托ID取消在途单"""
        if not hasattr(self, 'ib') or not self.ib or not self.ib.isConnected():
            return False

        oid = str(order_id or '').strip()
        if not oid:
            return False

        try:
            for t in self._collect_open_trades(force_refresh_all=True, include_trade_cache=False):
                if not self._match_pending_id(t, oid):
                    continue
                remaining = float(getattr(getattr(t, 'orderStatus', None), 'remaining', 0) or 0)
                if remaining <= 0:
                    return False
                raw_status = str(getattr(getattr(t, 'orderStatus', None), 'status', '') or '').strip().upper()
                if raw_status in self._TERMINAL_STATUSES:
                    return False
                if raw_status == 'PENDINGCANCEL':
                    return False
                order = getattr(t, 'order', None)
                if not order:
                    return False
                order_id = self._safe_order_id(t)
                if not order_id:
                    perm_id = self._safe_perm_id(t)
                    rebound = self._try_bind_manual_order_and_resolve(perm_id)
                    if rebound is None:
                        cid = self._connected_client_id()
                        print(
                            f"[IBBroker] cancel_pending_order skipped ({oid}): "
                            f"unresolved api orderId (permId={perm_id}, clientId={cid})."
                        )
                        self._push_manual_bind_alarm_once(
                            oid, perm_id, cid, reason='unresolved api orderId'
                        )
                        return False
                    t = rebound
                    order = getattr(t, 'order', None)
                    if not order or not self._safe_order_id(t):
                        cid = self._connected_client_id()
                        print(
                            f"[IBBroker] cancel_pending_order skipped ({oid}): "
                            f"bind retry still has invalid orderId (clientId={cid})."
                        )
                        self._push_manual_bind_alarm_once(
                            oid, perm_id, cid, reason='bind retry still has invalid orderId'
                        )
                        return False
                self.ib.cancelOrder(order)
                if self._confirm_cancel_effective(oid, max_checks=3, sleep_seconds=0.15):
                    return True

                cid = self._connected_client_id()
                perm_id = self._safe_perm_id(t)
                print(
                    f"[IBBroker] cancel_pending_order failed ({oid}): "
                    f"cancel not confirmed (permId={perm_id}, clientId={cid})."
                )
                self._push_manual_bind_alarm_once(
                    oid, perm_id, cid, reason='cancel not confirmed by IB'
                )
                return False
            return False
        except Exception as e:
            print(f"[IBBroker] cancel_pending_order failed ({oid}): {e}")
            return False

    @staticmethod
    def is_live_mode(context) -> bool:
        # IB Adapter 只要被调用基本都是为了实盘 (paper or live)
        # 回测建议使用 Backtrader 原生或 CSV
        return True

    @staticmethod
    def extract_run_config(context) -> dict:
        return {}

    @staticmethod
    def parse_contract(symbol: str) -> Contract:
        """
        合约解析器
        支持格式:
        1. "QQQ.ISLAND" -> 美股指定主交易所 (PrimaryExchange)
        2. "SHSE.600000" -> A股 (保持兼容)
        3. "00700" -> 港股 (保持兼容)
        4. "AAPL" -> 默认 SMART/USD
        """
        spec = resolve_ib_contract_spec(symbol)

        if spec['kind'] == 'forex':
            return Forex(spec['pair'])

        if spec['kind'] == 'crypto':
            if Crypto is not None:
                return Crypto(spec['symbol'], spec['exchange'], spec['currency'])
            return Contract(
                symbol=spec['symbol'],
                secType='CRYPTO',
                exchange=spec['exchange'],
                currency=spec['currency'],
            )

        if spec['primary_exchange']:
            return Stock(
                spec['symbol'],
                spec['exchange'],
                spec['currency'],
                primaryExchange=spec['primary_exchange']
            )

        return Stock(spec['symbol'], spec['exchange'], spec['currency'])

    # 1. 查钱 (重构为通用方法，支持指定 Tag)
    def _fetch_smart_value(self, target_tags=None, source_data=None) -> float:
        """
        获取账户特定价值（如现金或净值），支持多币种自动加总并统一转换为 USD。
        修复了因单一币种（如USD）为负债时，忽略其他币种正资产的问题。
        """
        if not hasattr(self, 'ib') or not self.ib: return 0.0

        in_loop = self._in_async_task()

        tags_priority = target_tags if target_tags else ['NetLiquidation', 'TotalCashValue', 'AvailableFunds']

        # 尝试获取账户数据源
        if source_data is None:
            source_data = self._load_account_snapshot()
        if not source_data:
            return 0.0

        for tag in tags_priority:
            base_value = None
            for v in source_data:
                if v.tag == tag and v.currency == 'BASE':
                    try:
                        base_value = float(v.value)
                        break
                    except Exception:
                        continue

            # 提取该 tag 下所有的币种记录 (排除 BASE，由我们自己精准换算 USD)
            items = [v for v in source_data if v.tag == tag and v.currency and v.currency != 'BASE']
            if not items:
                if base_value is not None:
                    return base_value
                continue

            total_usd = 0.0
            found_valid = False
            missing_fx = False

            for item in items:
                try:
                    val = float(item.value)
                    # 忽略为0的货币项 (除非是查净值)
                    if val == 0 and tag != 'NetLiquidation':
                        continue

                    if item.currency == 'USD':
                        total_usd += val
                        found_valid = True
                    else:
                        # --- 汇率转换逻辑 ---
                        pair_symbol = f"USD{item.currency}"
                        inverse_pair = False
                        if item.currency in ['EUR', 'GBP', 'AUD', 'NZD']:
                            pair_symbol = f"{item.currency}USD"
                            inverse_pair = True

                        exchange_rate = self._load_fx_rate(pair_symbol, in_loop=in_loop)

                        if exchange_rate > 0:
                            if inverse_pair:
                                total_usd += val * exchange_rate
                            else:
                                total_usd += val / exchange_rate
                            found_valid = True
                        else:
                            if val != 0:
                                missing_fx = True
                                print(f"[IB Warning] 无法获取 {item.currency} 汇率, 金额 {val} 未计入。")
                except Exception:
                    continue

            # 若多币种中存在无法换汇项，且券商提供 BASE 汇总，优先回退 BASE，避免低估可用资金。
            if missing_fx and base_value is not None:
                print(f"[IB Warning] {tag} 存在汇率缺口，回退使用 BASE 汇总口径。")
                return base_value

            # 只要在这个 tag 下成功计算了哪怕一个有效条目（即便加总是负数），都直接返回
            if found_valid:
                return total_usd

            if base_value is not None:
                return base_value

        return 0.0

    def _extract_rate_from_ticker(self, ticker):
        """辅助方法：从 ticker 中提取有效汇率，含 Close/Last 兜底"""
        rate = ticker.marketPrice()
        if not (rate and rate > 0 and rate == rate):
            if ticker.close and ticker.close > 0:
                return ticker.close
            elif ticker.last and ticker.last > 0:
                return ticker.last
            # 尝试 midPoint (Forex 有时用这个)
            elif ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                return (ticker.bid + ticker.ask) / 2
        return rate

    def _get_or_request_fx_ticker(self, pair_symbol: str, in_loop: bool = None):
        if in_loop is None:
            in_loop = self._in_async_task()
        ticker = self._fx_tickers.get(pair_symbol)
        if ticker:
            return ticker

        contract = Forex(pair_symbol)
        if not in_loop:
            self.ib.qualifyContracts(contract)
        ticker = self.ib.reqMktData(contract, '', False, False)
        self._fx_tickers[pair_symbol] = ticker
        if not in_loop:
            start_wait = datetime.datetime.now()
            while (datetime.datetime.now() - start_wait).total_seconds() < 1.0:
                self.ib.sleep(0.1)
                if self._extract_rate_from_ticker(ticker) > 0:
                    break
        return ticker

    def _load_fx_rate(self, pair_symbol: str, in_loop: bool = None) -> float:
        if not hasattr(self, 'ib') or not self.ib:
            return 0.0
        if in_loop is None:
            in_loop = self._in_async_task()

        exchange_rate = 0.0
        try:
            ticker = self._get_or_request_fx_ticker(pair_symbol, in_loop=in_loop)
            exchange_rate = self._extract_rate_from_ticker(ticker)
        except Exception:
            exchange_rate = 0.0

        if not (exchange_rate > 0):
            if pair_symbol in self._last_valid_fx_rates:
                exchange_rate = self._last_valid_fx_rates[pair_symbol]
            elif not in_loop:
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                retry_not_before = self._fx_rate_retry_not_before.get(pair_symbol)
                if not retry_not_before or now_utc >= retry_not_before:
                    try:
                        bars = self.ib.reqHistoricalData(
                            Forex(pair_symbol), endDateTime='', durationStr='2 D',
                            barSizeSetting='1 day', whatToShow='MIDPOINT', useRTH=False,
                            timeout=3.0
                        )
                        if bars:
                            exchange_rate = bars[-1].close
                            self._fx_rate_retry_not_before.pop(pair_symbol, None)
                        else:
                            self._fx_rate_retry_not_before[pair_symbol] = (
                                now_utc + datetime.timedelta(minutes=5)
                            )
                    except Exception:
                        self._fx_rate_retry_not_before[pair_symbol] = (
                            now_utc + datetime.timedelta(minutes=5)
                        )

        if exchange_rate > 0:
            self._last_valid_fx_rates[pair_symbol] = exchange_rate
            return float(exchange_rate)
        return 0.0

    def prewarm_additional_connections(self, now=None):
        warmed = []
        if self._load_fx_rate('USDHKD', in_loop=False) > 0:
            warmed.append('USDHKD')
        return warmed

    # 2. 查持仓
    def get_position(self, data):
        class Pos:
            size = 0
            price = 0.0

        if not self.ib: return Pos()

        symbol = str(getattr(data, '_name', '')).strip()
        if not symbol:
            return Pos()

        # 遍历 ib.positions()；兼容 QQQ.ISLAND / QQQ.SMART 与 QQQ 的双向匹配
        positions = self._filter_account_scoped_items(
            self.ib.positions(),
            lambda p: self._extract_account_from_obj(p),
        )

        expected_symbols = {symbol.upper(), symbol.split('.')[0].upper()}
        expected_sec_type = None
        try:
            target_contract = self.parse_contract(symbol)
            target_symbol = str(getattr(target_contract, 'symbol', '')).strip().upper()
            if target_symbol:
                expected_symbols.add(target_symbol)
                expected_symbols.add(target_symbol.split('.')[0])
            expected_sec_type = str(getattr(target_contract, 'secType', '')).strip().upper() or None
            valid_sec_types = {'STK', 'CASH', 'CRYPTO', 'FUT', 'OPT', 'FOP', 'CFD', 'BOND', 'CMDTY', 'IND', 'WAR'}
            if expected_sec_type not in valid_sec_types:
                expected_sec_type = None
        except Exception:
            pass

        for p in positions:
            pos_contract = getattr(p, 'contract', None)
            if not pos_contract:
                continue

            pos_sec_type = str(getattr(pos_contract, 'secType', '')).strip().upper()
            if expected_sec_type and pos_sec_type and pos_sec_type != expected_sec_type:
                continue

            raw_symbol = str(getattr(pos_contract, 'symbol', '')).strip().upper()
            local_symbol = str(getattr(pos_contract, 'localSymbol', '')).strip().upper()
            position_symbols = set()
            for s in (raw_symbol, local_symbol):
                if s:
                    position_symbols.add(s)
                    position_symbols.add(s.split('.')[0])

            if position_symbols & expected_symbols:
                o = Pos()
                o.size = p.position
                o.price = p.avgCost
                return o
        return Pos()

    # 3. 查价
    def get_current_price(self, data):
        """
        获取标的当前价格。
        增强版：支持周末/休市期间使用 Close/Last 价格兜底，防止无法计算下单数量。
        """
        if not hasattr(self, 'ib') or not self.ib or not self.ib.isConnected():
            return 0.0

        symbol = data._name
        ticker = self._tickers.get(symbol)

        # 1. 如果缓存里没有 ticker (防御性逻辑，防止动态添加的标的没订阅)
        if not ticker:
            # print(f"[IB Debug] Ticker not found for {symbol}, requesting subscription...")
            contract = self.parse_contract(symbol)
            self.ib.qualifyContracts(contract)
            # snapshot=False 建立流式订阅
            ticker = self.ib.reqMktData(contract, '', False, False)
            self._tickers[symbol] = ticker

            import time
            start_time = time.time()
            while time.time() - start_time < 1.0:
                self.ib.sleep(0.01)  # 允许较短的协作式让出
                if ticker.marketPrice() == ticker.marketPrice() and ticker.marketPrice() > 0:
                    break

        # 2. 获取价格 (优先 marketPrice)
        price = ticker.marketPrice()

        # 如果 marketPrice 无效 (NaN/0/-1)，先尝试切换 delayed 行情并重取。
        if not (price and 0 < price == price):
            delayed_price = self._try_get_delayed_quote(symbol)
            ticker = self._tickers.get(symbol, ticker)
            if delayed_price > 0:
                print(f"[IB Warning] {symbol} realtime quote invalid. Using DELAYED price: {delayed_price}")
                price = delayed_price

        # 如果仍无效，尝试使用 close 或 last
        # 这种情况常见于周末、盘前盘后或停牌
        if not (price and 0 < price == price):
            # 优先用昨日收盘价 (Close)
            if ticker.close and ticker.close > 0:
                print(
                    f"[IB Debug] {symbol} marketPrice invalid ({price}). Using CLOSE price for execution: {ticker.close}")
                price = ticker.close
            # 其次用最后成交价 (Last)
            elif ticker.last and ticker.last > 0:
                print(f"[IB Debug] {symbol} marketPrice invalid. Using LAST price: {ticker.last}")
                price = ticker.last
            else:
                # 无实时价时，从非 CSV 数据源按优先级兜底获取
                fallback_price, tried_sources = self._fallback_price_from_sources(symbol)
                if fallback_price > 0:
                    print(
                        f"[IB Warning] No valid price (Market/Close/Last) for {symbol}. "
                        f"Using provider price: {fallback_price}"
                    )
                    price = fallback_price
                else:
                    self._alarm_no_price(symbol, tried_sources)
                    price = 0.0

        return price

    def _resolve_runtime_config(self) -> dict:
        try:
            ctx = getattr(self, '_context', None)
            trader = getattr(ctx, 'strategy_instance', None)
            if trader and hasattr(trader, 'config'):
                return trader.config or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _extract_last_price(df: pd.DataFrame) -> float:
        if df is None or df.empty:
            return 0.0
        for col in ('close', 'Close', 'adjClose', 'Adj Close', 'last', 'Last'):
            if col in df.columns:
                try:
                    val = df[col].iloc[-1]
                    if val and val == val and val > 0:
                        return float(val)
                except Exception:
                    continue
        return 0.0

    def _collect_price_providers(self, data_source: str = None):
        if self._price_data_manager is None:
            self._price_data_manager = DataManager()

        providers = list(self._price_data_manager.providers or [])
        allowed = None
        if data_source:
            raw_sources = str(data_source).strip().lower()
            allowed = {s for s in re.split(r"[,\s]+", raw_sources) if s}

        selected = []
        for provider in providers:
            if isinstance(provider, CsvDataProvider):
                continue
            provider_name = provider.__class__.__name__.replace('DataProvider', '').lower()
            if allowed and provider_name not in allowed:
                continue
            selected.append((provider_name, provider))

        if not allowed:
            # 未指定 data_source 时，优先使用 IBKR 数据源
            selected.sort(key=lambda item: 0 if item[0] in {'ibkr', 'ib'} else 1)

        return selected

    def _build_price_window(self, now_ts: pd.Timestamp, timeframe: str, compression: int):
        if timeframe == 'Minutes':
            start_ts = now_ts - pd.Timedelta(days=2)
            return (
                start_ts.strftime('%Y-%m-%d %H:%M:%S'),
                now_ts.strftime('%Y-%m-%d %H:%M:%S'),
            )
        start_ts = now_ts - pd.Timedelta(days=7)
        return (start_ts.strftime('%Y-%m-%d'), now_ts.strftime('%Y-%m-%d'))

    def _fallback_price_from_sources(self, symbol: str):
        cfg = self._resolve_runtime_config()
        timeframe = cfg.get('timeframe', 'Days')
        try:
            compression = int(cfg.get('compression', 1) or 1)
        except Exception:
            compression = 1
        data_source = cfg.get('data_source')

        now_ts = pd.Timestamp(getattr(self._context, 'now', None) or datetime.datetime.now())
        start_date, end_date = self._build_price_window(now_ts, timeframe, compression)

        providers = self._collect_price_providers(data_source=data_source)
        tried = []

        for provider_name, provider in providers:
            tried.append(provider_name)
            try:
                df = provider.get_data(
                    symbol,
                    start_date=start_date,
                    end_date=end_date,
                    timeframe=timeframe,
                    compression=compression,
                )
                price = self._extract_last_price(df)
                if price > 0:
                    return price, tried
            except Exception:
                continue

        return 0.0, tried

    def _alarm_no_price(self, symbol: str, tried_sources: list):
        now = getattr(self._context, 'now', None) or datetime.datetime.now()
        try:
            now = pd.Timestamp(now)
        except Exception:
            now = pd.Timestamp(datetime.datetime.now())
        if pd.isna(now):
            now = pd.Timestamp(datetime.datetime.now())
        day_key = now.strftime('%Y-%m-%d')
        alarm_key = f"no_price:{symbol}:{day_key}"
        if self._live_alarm.seen(alarm_key):
            return

        tried_str = ",".join(tried_sources) if tried_sources else "N/A"
        msg = (
            f"[IBBroker] No valid price for {symbol}. "
            f"Tried providers: {tried_str}. Orders will be blocked."
        )
        print(f"[IB Warning] {msg}")
        try:
            AlarmManager().push_text(msg, level='ERROR')
        except Exception:
            pass

    @staticmethod
    def _augment_live_data_source(data_source: str) -> str:
        source_names = [s for s in re.split(r"[,\s]+", str(data_source or '').strip().lower()) if s]
        if not source_names:
            return data_source
        if any(s in {'ib', 'ibkr'} for s in source_names):
            return ",".join(source_names)
        return ",".join(source_names + ['ibkr'])

    # 4. 发单
    def _submit_order(self, data, volume, side, price):
        if not self.ib: return None

        contract = self.parse_contract(data._name)
        action = 'BUY' if side == 'BUY' else 'SELL'

        try:
            raw_volume = abs(float(volume))
        except Exception:
            raw_volume = 0.0

        # 默认禁用 API 小数股卖单，避免在不支持分数股的 IB 账户触发 10243 拒单。
        # 如账户已确认支持，可显式在 config 中开启 IBKR_ALLOW_FRACTIONAL_SELL=True。
        allow_fractional_sell = bool(getattr(config, 'IBKR_ALLOW_FRACTIONAL_SELL', False))
        should_use_fractional_sell = (
            side == 'SELL'
            and allow_fractional_sell
            and raw_volume > 0
            and abs(raw_volume - round(raw_volume)) > 1e-9
        )
        if should_use_fractional_sell:
            volume_final = round(raw_volume, 6)
        else:
            # 买单维持整数向下取整，防止超额占资
            volume_final = int(raw_volume)

        # 防止零股交易
        if volume_final <= 0:
            print(f"[IB Warning] Order size < 1 (raw: {volume}), skipped.")
            return None

        configured_account = self._configured_order_account()
        known_accounts = None
        if configured_account:
            if not self._is_configured_order_account_valid(configured_account):
                return None
        else:
            known_accounts = self._collect_known_accounts()
            if known_accounts and all(self._is_aggregate_account_marker(a) for a in known_accounts):
                known_accounts = set()
            if len(known_accounts) > 1:
                self._warn_missing_order_account_for_schedule_scope(known_accounts)
                return None

        # 使用市价单 (MarketOrder) 或 限价单 (LimitOrder)
        # 此处简单起见使用市价单
        order = MarketOrder(action, volume_final)
        if configured_account:
            # 透传 IB 订单 account 字段；留空时由 IB 默认使用主账户路由。
            order.account = configured_account

        trade = self.ib.placeOrder(contract, order)
        return IBOrderProxy(trade, data=data)

    @staticmethod
    def _should_trigger_daily_schedule(now: datetime.datetime, target_h: int, target_m: int, target_s: int,
                                       last_schedule_run_date: str):
        """
        每日调度触发判定:
        - 仅在目标时间后的短容忍窗口内触发（不做补跑）
        - 同一自然日只允许运行一次
        """
        target_dt = now.replace(hour=target_h, minute=target_m, second=target_s, microsecond=0)
        delta = (now - target_dt).total_seconds()
        current_date_str = now.strftime('%Y-%m-%d')

        if last_schedule_run_date == current_date_str:
            return False, delta, current_date_str
        tolerance_window = 5.0
        if delta < 0 or delta > tolerance_window:
            return False, delta, current_date_str
        return True, delta, current_date_str

    # 5. 将券商的原始订单对象（raw_order）转换为框架标准的 BaseOrderProxy
    def convert_order_proxy(self, raw_trade_or_order) -> 'BaseOrderProxy':
        """
        注意：IB 的回调有时候传回 Trade 对象，有时候是 Order 对象，需要这里做判断处理
        """
        # 假设 raw_trade_or_order 是 ib_insync 的 Trade 对象
        # 如果 Engine 里的回调传的是 order，这里需要适配一下

        trade = raw_trade_or_order
        # 如果传入的只是 Order 对象（没有 Trade 包装），可能需要特殊处理或者在 IB 回调入口处统一封装

        # 查找 Data
        target_symbol = ""
        if hasattr(trade, 'contract'):
            target_symbol = trade.contract.symbol
        elif hasattr(trade, 'symbol'):  # 万一是 Contract
            target_symbol = trade.symbol

        matched_data = None
        # 简单的符号匹配逻辑 (可能需要根据 IBBrokerAdapter.parse_contract 的逆逻辑来匹配)
        for d in self.datas:
            # 提取策略层命名中的基础代码 (例如将 'AAPL.SMART' 提取为 'AAPL')
            base_name = d._name.split('.')[0].upper()

            # 使用精确等于 (==) 而非包含 (in)
            if base_name == target_symbol.upper():
                matched_data = d
                break

        return IBOrderProxy(trade, data=matched_data)

    # 5. IB 特有的启动协议
    @classmethod
    def launch(cls, conn_cfg: dict, strategy_path: str, params: dict, **kwargs):
        """
        IBKR 全天候启动入口
        """
        import config
        import time
        import asyncio
        import pytz
        from ib_insync import IB

        host = config.IBKR_HOST
        port = config.IBKR_PORT
        client_id = config.IBKR_CLIENT_ID

        # 默认为空，表示使用服务器本地时间
        timezone_str = conn_cfg.get('timezone')
        target_tz = pytz.timezone(timezone_str) if timezone_str else None

        # 1. 获取调度配置 (格式示例: "1d:14:50:00")
        schedule_rule = conn_cfg.get('schedule')
        if not schedule_rule:
            # 尝试从 kwargs 获取 (兼容命令行传参)
            schedule_rule = kwargs.get('schedule')

        symbols = kwargs.get('symbols', [])
        selection_name = kwargs.get('selection')
        risk_name = kwargs.get('risk')
        risk_params = kwargs.get('risk_params')

        print(f"\n>>> 🛡️ Launching IBKR Phoenix Mode (Host: {host}:{port}) <<<")
        runtime_marker = getattr(config, 'RUNTIME_MARKER', 'ib-live-2026-02-27-riskfix-v1')
        print(f">>> 🧬 Runtime Marker: {runtime_marker}")
        if schedule_rule:
            tz_info = timezone_str if timezone_str else "Server Local Time"
            print(f">>> ⏰ Schedule Active: {schedule_rule} (Zone: {tz_info})")
        else:
            print(f">>> ⚠️ No Schedule Found: Strategy will NOT run automatically. (Heartbeat Only)")

        parsed_schedule = None
        if schedule_rule:
            try:
                parsed_schedule = SchedulePlanner.parse_schedule_rule(schedule_rule)
                if parsed_schedule is None:
                    print(
                        f">>> ⚠️ Unsupported schedule format for IB adapter: {schedule_rule}. "
                        "Expected: 1d|Nm|Nh:HH:MM[:SS]"
                    )
            except Exception as e:
                print(f">>> ⚠️ Invalid schedule config: {schedule_rule}. Error: {e}")
                parsed_schedule = None

        try:
            prewarm_lead_seconds = SchedulePlanner.parse_schedule_prewarm_lead(
                getattr(config, 'LIVE_SCHEDULE_PREWARM_LEAD', 0)
            )
        except Exception as e:
            print(f">>> ⚠️ Invalid LIVE_SCHEDULE_PREWARM_LEAD: {e}. Prewarm disabled.")
            prewarm_lead_seconds = 0.0
        if parsed_schedule and prewarm_lead_seconds > 0:
            interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
            if interval_seconds <= 0 or prewarm_lead_seconds >= interval_seconds:
                print(
                    f">>> ⚠️ LIVE_SCHEDULE_PREWARM_LEAD={prewarm_lead_seconds:.0f}s is not smaller than "
                    f"schedule interval {interval_seconds:.0f}s. Prewarm disabled."
                )
                prewarm_lead_seconds = 0.0
        if parsed_schedule and prewarm_lead_seconds > 0:
            print(f">>> 🔥 Prewarm enabled: trigger {prewarm_lead_seconds:.0f}s before schedule")
        # 1. 创建全局唯一的 IB 实例
        ib = IB()

        # 2. 预初始化 Engine Context
        class Context:
            now = None
            ib_instance = ib
            strategy_instance = None

        ctx = Context()
        ctx.schedule_rule = schedule_rule
        init_now = datetime.datetime.now(target_tz) if target_tz else datetime.datetime.now()
        ctx.now = pd.Timestamp(init_now)

        if parsed_schedule:
            try:
                tz_info = timezone_str if timezone_str else "Server Local Time"
                SchedulePlanner.print_schedule_preview(
                    now=init_now,
                    parsed_schedule=parsed_schedule,
                    prewarm_lead_seconds=prewarm_lead_seconds,
                    tz_info=tz_info,
                    count=3,
                    prefix=">>>",
                )
            except Exception as e:
                print(f">>> ⚠️ Failed to compute next schedule time: {e}")

        # 初始化 Engine (只做一次)
        from live_trader.engine import LiveTrader, on_order_status_callback
        engine_config = config.__dict__.copy()
        engine_config['strategy_name'] = strategy_path
        engine_config['params'] = params
        engine_config['platform'] = 'ib'
        engine_config['symbols'] = symbols
        if kwargs.get('timeframe') is not None:
            engine_config['timeframe'] = kwargs.get('timeframe')
        if kwargs.get('compression') is not None:
            engine_config['compression'] = kwargs.get('compression')
        if kwargs.get('data_source'):
            raw_data_source = kwargs.get('data_source')
            source_names = [s for s in re.split(r"[,\s]+", str(raw_data_source or '').strip().lower()) if s]
            had_ib_source = any(s in {'ib', 'ibkr'} for s in source_names)
            data_source = cls._augment_live_data_source(raw_data_source)
            if data_source != raw_data_source and not had_ib_source:
                print(
                    f"[IBBroker] Live data source fallback enabled: "
                    f"{raw_data_source} -> {data_source}"
                )
            engine_config['data_source'] = data_source
        if selection_name:
            engine_config['selection_name'] = selection_name
        if risk_name:
            engine_config['risk'] = risk_name
        if risk_params is not None:
            engine_config['risk_params'] = risk_params

        trader = LiveTrader(engine_config)
        # 注入 IB 实例到 data_provider (如果有)
        if hasattr(trader.data_provider, 'ib'):
            trader.data_provider.ib = ib

        trader.init(ctx)
        ctx.strategy_instance = trader

        # 确定标的列表
        target_symbols = []
        if hasattr(trader.broker, 'datas'):
            target_symbols = [d._name for d in trader.broker.datas]
        else:
            target_symbols = symbols

        # 注册回调
        async def on_trade_update(trade):
            # 在这里拦截卖单，安全地异步等待 1 秒
            # 这会让出控制权给 Event Loop，使其有时间处理 IB 推送过来的 AccountValue 更新
            if trade.order.action == 'SELL' and trade.orderStatus.status == 'Filled':
                await asyncio.sleep(1.0)

            on_order_status_callback(ctx, trade)

        ib.orderStatusEvent += on_trade_update

        # --- 调度器状态变量 ---
        last_schedule_run_key = None
        last_prewarm_run_key = None
        is_first_connect = True

        # --- 3. 进入“不死鸟”主循环 ---
        while True:
            try:
                # --- A. 连接阶段 ---
                if not ib.isConnected():
                    print(f"[System] Connecting to IB Gateway ({host}:{port}) with clientId={client_id}...")
                    try:
                        ib.connect(host, port, clientId=client_id)
                        print("[System] ✅ Connected successfully.")
                        # 连接恢复后先复位行情模式标记，后续若实时不可用可再次自动降级 delayed。
                        try:
                            if hasattr(trader, 'broker') and trader.broker:
                                trader.broker._delayed_market_data_enabled = False
                        except Exception:
                            pass
                        try:
                            if client_id == 0 and hasattr(ib, 'reqAutoOpenOrders'):
                                ib.reqAutoOpenOrders(True)
                                if hasattr(ib, 'reqOpenOrders'):
                                    ib.reqOpenOrders()
                                print("[System] 🔗 Manual order binding enabled (clientId=0).")
                            elif client_id != 0:
                                print(
                                    "[System] ℹ️ clientId != 0: manual TWS orders may keep orderId=0 "
                                    "and cannot be canceled individually."
                                )
                        except Exception as bind_err:
                            print(f"[System Warning] manual-order bind setup failed: {bind_err}")
                    except Exception as e:
                        # 🔴 关键修复：使用 repr(e) 捕获空字面量异常
                        err_msg = repr(e)
                        print(f"[System] ⏳ Connection failed: {err_msg}")

                        # 幽灵占用与超时自愈逻辑
                        if "already in use" in err_msg or "326" in err_msg:
                            print(f"[System] 🔄 发现幽灵占用，坚持使用 client_id={client_id} 每 5 秒尝试抢占 Session...")
                            time.sleep(5)
                            continue

                        # 其他真网络错误保持较长的冷却
                        print("[System] ⏳ Retrying in 10s...")
                        time.sleep(10)
                        continue

                # --- B. 状态恢复 (Re-Subscribe) ---
                if is_first_connect or not ib.tickers():  # 如果没有 tickers 说明订阅丢了
                    print(f"[System] 📡 (Re)Subscribing market data for {len(target_symbols)} symbols...")
                    active_tickers = {}
                    for sym in target_symbols:
                        try:
                            contract = cls.parse_contract(sym)
                            ib.qualifyContracts(contract)
                            # snapshot=False 建立流式订阅
                            ticker = ib.reqMktData(contract, '', False, False)
                            active_tickers[sym] = ticker
                        except Exception as e:
                            print(f"[Warning] Failed to subscribe {sym}: {e}")

                    # 更新 Broker 的引用
                    trader.broker._tickers = active_tickers

                    if not is_first_connect:
                        print("[System] 🔄 Re-connection logic triggered (Data Stream Restored).")

                is_first_connect = False

                # --- C. 运行阶段 (Event Loop) ---
                print("[System] Entering Event Loop...")

                while ib.isConnected():
                    # 1. 驱动 IB 事件
                    # 如果断线，ib.sleep 会抛出 OSError 或 ConnectionResetError
                    ib.sleep(1)

                    # 基于时区的时间计算
                    if target_tz:
                        # 如果配置了时区，获取带时区的当前时间
                        now = datetime.datetime.now(target_tz)
                    else:
                        # 否则使用本地时间
                        now = datetime.datetime.now()

                    # 2. 执行策略
                    ctx.now = pd.Timestamp(now)

                    if parsed_schedule and prewarm_lead_seconds > 0:
                        try:
                            should_prewarm, seconds_to_schedule, schedule_slot_key = (
                                SchedulePlanner.should_trigger_schedule_prewarm_for_rule(
                                    now=now,
                                    parsed_schedule=parsed_schedule,
                                    lead_seconds=prewarm_lead_seconds,
                                    last_prewarm_run_key=last_prewarm_run_key,
                                    last_schedule_run_key=last_schedule_run_key,
                                )
                            )
                            if should_prewarm:
                                timeframe = trader.config.get('timeframe', 'Days')
                                compression = trader.config.get('compression', 1)
                                print(
                                    f"\n>>> 🔥 Prewarm Triggered: {schedule_rule} "
                                    f"(T-{seconds_to_schedule:.2f}s) <<<"
                                )
                                summary = trader.broker.run_schedule_prewarm(
                                    schedule_rule=schedule_rule,
                                    data_provider=trader.data_provider,
                                    symbols=target_symbols,
                                    timeframe=timeframe,
                                    compression=compression,
                                    now=ctx.now,
                                )
                                last_prewarm_run_key = schedule_slot_key
                                print(
                                    ">>> Prewarm Finished. "
                                    f"source={summary.get('source')}, "
                                    f"symbol={summary.get('symbol')}, "
                                    f"extras={summary.get('extras')}, "
                                    f"errors={summary.get('errors')}\n"
                                )
                        except Exception as e:
                            print(f"[Prewarm Error] Check failed: {e}")

                    # (B) 调度检查逻辑
                    if parsed_schedule:
                        try:
                            should_run, delta, schedule_slot_key = SchedulePlanner.should_trigger_schedule(
                                now=now,
                                parsed_schedule=parsed_schedule,
                                last_schedule_run_key=last_schedule_run_key,
                            )
                            if should_run:
                                print(
                                    f"\n>>> ⏰ Schedule Triggered: {schedule_rule} (Delta: {delta:.2f}s) <<<")

                                # === 触发策略运行 ===
                                trader.run(ctx)

                                # === 更新状态锁 ===
                                last_schedule_run_key = schedule_slot_key
                                next_run = SchedulePlanner.resolve_next_schedule_slot(
                                    now + datetime.timedelta(seconds=1),
                                    parsed_schedule,
                                )
                                print(f">>> Run Finished. Next run: {next_run.strftime('%Y-%m-%d %H:%M:%S')}\n")

                        except Exception as e:
                            print(f"[Schedule Error] Check failed: {e}")

            # --- D. 异常处理 ---
            except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError, TimeoutError, ConnectionError,
                    asyncio.TimeoutError) as e:
                # 捕获这些明确的网络层异常
                print(f"\n[⚠️ Disconnect] Network Error: {e}")
                print("[System] Entering Recovery Mode. Waiting for TWS/Gateway...")

                try:
                    ib.disconnect()
                except:
                    pass

                time.sleep(10)  # 稍微长一点的冷却
                continue

            except Exception as e:
                # 捕获其他未知的崩溃 (如数据解析错误)
                print(f"[CRITICAL] Unexpected crash in Main Loop: {e}")
                import traceback
                traceback.print_exc()

                # 防止死循环刷屏
                time.sleep(5)
                # 尝试重启
                try:
                    ib.disconnect()
                except:
                    pass
                continue

            except KeyboardInterrupt:
                print("\n[Stop] User interrupted. Exiting.")
                ib.disconnect()
                break

