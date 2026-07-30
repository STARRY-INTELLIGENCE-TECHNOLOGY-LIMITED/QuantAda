import time

import config
from common import runtime_notifications
from common.live_execution_budget import (
    get_live_run_deadline,
    live_run_budget_expired,
    live_run_finalization_reserve,
    live_run_seconds_remaining,
    sleep_with_live_run_budget,
)
from common.order_quantity import (
    positive_quantity,
    quantity_at_most,
    subtract_quantities,
)


class OrderExecutor:
    """
    Executes a rebalance plan against a live broker.
    """
    _SELL_SETTLE_WARN_SECONDS = 300.0
    _SELL_SETTLE_HARD_SECONDS = 600.0
    _SELL_SETTLE_POLL_SECONDS = 1.0
    _POST_SELL_CASH_WAIT_SECONDS = 10.0
    _POST_SELL_CASH_POLL_SECONDS = 0.5
    _POST_SELL_CASH_TOLERANCE = 0.99
    _BENIGN_BACKTEST_SKIP_REASONS = {
        'below_min_lot_delta',
        'insufficient_cash_for_min_lot',
        'target_already_met',
    }

    def __init__(self, broker, debug=False):
        self.broker = broker
        self.debug = debug

    def execute_plan(self, plan):
        """执行调仓计划：利用 order_target_value 及其内部智能逻辑"""

        should_wait_post_sell_cash = getattr(self.broker, 'is_live', False) is True
        has_sell_plan = bool(plan['sell_clear'] or plan['reduce'])
        pre_sell_cash = self._get_available_cash() if should_wait_post_sell_cash and has_sell_plan else 0.0
        expected_sell_cash_release = 0.0
        sell_submitted = False
        sell_submit_failed = False
        submitted_sell_ids = set()
        sell_reconcile_targets = {}
        has_untracked_sell = False

        if should_wait_post_sell_cash and live_run_budget_expired(self.broker):
            self._emit_warning(
                "[Executor Warning] Live run execution budget exhausted before rebalance execution.",
                level='ERROR',
            )
            return

        # 第一步：处理所有卖出动作 (清仓 + 减仓)
        # 必须先执行卖出，再等待卖单终态，最大化后续买单资金利用率。
        for data in plan['sell_clear']:
            if should_wait_post_sell_cash and live_run_budget_expired(self.broker):
                sell_submit_failed = True
                self._emit_warning(
                    "[Executor Warning] Live run execution budget exhausted while submitting SELL orders; "
                    "accepted partial SELL orders remain active and later plan items are skipped.",
                    level='ERROR',
                )
                break
            self._log(f"执行清仓: {data._name}")
            before_size = self._read_position_size(data)
            sell_order = self.broker.order_target_value(data=data, target=0.0)
            if sell_order:
                sell_submitted = True
                if bool(getattr(sell_order, 'batch_submit_failed', False)):
                    sell_submit_failed = True
                order_ids = self._infer_order_ids(sell_order)
                reconcile_target = self._build_sell_reconcile_target(
                    data=data,
                    before_size=before_size,
                    sell_order=sell_order,
                    target_value=0.0,
                )
                if order_ids:
                    submitted_sell_ids.update(order_ids)
                    if reconcile_target is not None:
                        for oid in order_ids:
                            sell_reconcile_targets[oid] = reconcile_target
                else:
                    has_untracked_sell = True
                if should_wait_post_sell_cash:
                    expected_sell_cash_release += self._estimate_sell_cash_release(
                        data=data,
                        before_size=before_size,
                        sell_order=sell_order,
                        target_value=0.0,
                    )
            else:
                if not self._is_benign_backtest_skip():
                    sell_submit_failed = True
                self._warn_order_not_submitted('SELL', data, 0.0, phase='clear')

        for data, target in plan['reduce']:
            if should_wait_post_sell_cash and live_run_budget_expired(self.broker):
                sell_submit_failed = True
                self._emit_warning(
                    "[Executor Warning] Live run execution budget exhausted while submitting SELL orders; "
                    "accepted partial SELL orders remain active and later plan items are skipped.",
                    level='ERROR',
                )
                break
            self._log(f"执行减仓: {data._name} -> {target:.2f}")
            before_size = self._read_position_size(data)
            sell_order = self.broker.order_target_value(data=data, target=target)
            if sell_order:
                sell_submitted = True
                if bool(getattr(sell_order, 'batch_submit_failed', False)):
                    sell_submit_failed = True
                order_ids = self._infer_order_ids(sell_order)
                reconcile_target = self._build_sell_reconcile_target(
                    data=data,
                    before_size=before_size,
                    sell_order=sell_order,
                    target_value=target,
                )
                if order_ids:
                    submitted_sell_ids.update(order_ids)
                    if reconcile_target is not None:
                        for oid in order_ids:
                            sell_reconcile_targets[oid] = reconcile_target
                else:
                    has_untracked_sell = True
                if should_wait_post_sell_cash:
                    expected_sell_cash_release += self._estimate_sell_cash_release(
                        data=data,
                        before_size=before_size,
                        sell_order=sell_order,
                        target_value=target,
                    )
            else:
                if not self._is_benign_backtest_skip():
                    sell_submit_failed = True
                self._warn_order_not_submitted('SELL', data, target, phase='reduce')

        if sell_submit_failed:
            msg = (
                "[Executor Warning] One or more SELL orders were only partially submitted or not submitted. "
                "Accepted SELL orders will be reconciled, then BUY orders will use current broker cash; "
                "without an accepted SELL, BUY orders still attempt a cash-limited partial execution."
            )
            self._emit_warning(msg, level='ERROR')
            if should_wait_post_sell_cash and sell_submitted:
                self._wait_sells_settled(
                    submitted_sell_ids,
                    has_untracked_sell,
                    sell_reconcile_targets,
                    increase_plan=plan['increase'],
                    min_final_buy_cash=pre_sell_cash + expected_sell_cash_release,
                )
            else:
                self._execute_final_buys(
                    plan['increase'],
                    check_pending=should_wait_post_sell_cash,
                )
            return

        if not sell_submitted:
            for data, target in plan['increase']:
                if should_wait_post_sell_cash and live_run_budget_expired(self.broker):
                    self._emit_warning(
                        "[Executor Warning] Live run execution budget exhausted while submitting BUY orders; "
                        "remaining plan items are skipped.",
                        level='ERROR',
                    )
                    break
                self._log(f"执行补仓/开仓: {data._name} -> {target:.2f}")
                buy_order = self.broker.order_target_value(data=data, target=target)
                if not buy_order:
                    self._warn_order_not_submitted('BUY', data, target, phase='increase')
            return

        is_live_broker = getattr(self.broker, 'is_live', True) is True
        if not is_live_broker:
            self._execute_final_buys(plan['increase'], check_pending=False)
            return

        # 第二步：等待卖单回报的同时，按已确认现金滚动释放买单。
        self._log("等待卖单终态并滚动买入...")
        sells_settled = self._wait_sells_settled(
            submitted_sell_ids,
            has_untracked_sell,
            sell_reconcile_targets,
            plan['increase'],
            min_final_buy_cash=pre_sell_cash + expected_sell_cash_release,
        )
        if not sells_settled:
            return

    def _log(self, txt):
        if self.debug:
            print(f"[Executor] {txt}")

    def _should_emit_order_warning(self):
        return True

    def _emit_warning(self, msg, level='ERROR'):
        if not self._should_emit_order_warning():
            return

        print(msg)
        runtime_notifications.push_text(msg, level=level)

    def _is_benign_backtest_skip(self):
        if getattr(self.broker, 'is_live', True) is True:
            return False
        reason = getattr(self.broker, '_last_order_target_skip_reason', None)
        return reason in self._BENIGN_BACKTEST_SKIP_REASONS

    def _warn_order_not_submitted(self, side, data, target, phase):
        if self._is_benign_backtest_skip():
            self._log(
                f"{side} order skipped as backtest no-op: "
                f"phase={phase}, symbol={getattr(data, '_name', str(data))}, "
                f"reason={getattr(self.broker, '_last_order_target_skip_reason', None)}"
            )
            return

        symbol = getattr(data, '_name', str(data))
        try:
            target_text = f"{float(target):.2f}"
        except Exception:
            target_text = str(target)
        reason = getattr(self.broker, '_last_order_target_skip_reason', None)
        reason_text = f", reason={reason}" if reason else ""
        msg = (
            f"[Executor Warning] {side} order not submitted: "
            f"phase={phase}, symbol={symbol}, target={target_text}{reason_text}."
        )
        self._emit_warning(msg, level='ERROR')

    @staticmethod
    def _symbol_key(value):
        if hasattr(value, '_name'):
            value = getattr(value, '_name', '')
        return str(value or '').strip().upper()

    def _symbol_matches_data(self, symbol, data):
        symbol_key = self._symbol_key(symbol)
        data_key = self._symbol_key(data)
        if not symbol_key or not data_key:
            return False
        return symbol_key == data_key or symbol_key == data_key.split('.')[0]

    def _has_pending_buy(self, data):
        data_key = self._symbol_key(data)
        if not data_key:
            return False

        if hasattr(self.broker, '_active_buys'):
            try:
                active_buys = getattr(self.broker, '_active_buys', {}) or {}
                for buy_info in active_buys.values():
                    if isinstance(buy_info, dict):
                        info_data = buy_info.get('data')
                        if info_data is not None and self._symbol_key(info_data) == data_key:
                            return True
                        if self._symbol_matches_data(buy_info.get('symbol'), data):
                            return True
                    elif self._symbol_matches_data(getattr(buy_info, 'symbol', None), data):
                        return True
            except Exception:
                pass

        return self._has_remote_pending_buy(data)

    def _has_remote_pending_buy(self, data):
        if hasattr(self.broker, 'get_pending_orders'):
            try:
                for po in self.broker.get_pending_orders() or []:
                    if not isinstance(po, dict):
                        continue
                    if str(po.get('direction', '')).strip().upper() != 'BUY':
                        continue
                    if self._symbol_matches_data(po.get('symbol'), data):
                        return True
            except Exception:
                pass

        return False

    def _get_available_cash(self):
        getter = getattr(self.broker, 'get_rebalance_cash', None)
        if not callable(getter):
            getter = getattr(self.broker, 'get_cash', None)
        if not callable(getter):
            return 0.0

        try:
            return max(0.0, float(getter() or 0.0))
        except Exception:
            return 0.0

    def _wait_for_post_sell_cash(self, min_cash):
        try:
            min_cash = float(min_cash)
        except Exception:
            return False

        tolerance = max(0.0, min(1.0, float(self._POST_SELL_CASH_TOLERANCE)))
        target_cash = max(0.0, min_cash * tolerance)
        if target_cash <= 0:
            return True

        wait_seconds = max(0.0, float(self._POST_SELL_CASH_WAIT_SECONDS))
        poll = max(0.05, float(self._POST_SELL_CASH_POLL_SECONDS))
        run_deadline = get_live_run_deadline(self.broker)
        reserve = live_run_finalization_reserve(self.broker) if run_deadline is not None else 0.0
        if run_deadline is not None:
            # 卖单已由持仓确认后，现金到账是执行剩余计划的最后关键条件。
            # 使用本轮剩余预算等待，而不是沿用旧的 10 秒局部上限。
            wait_seconds = live_run_seconds_remaining(
                self.broker,
                reserve,
                deadline=run_deadline,
            )
        start_ts = time.time()
        last_cash = self._get_available_cash()

        while last_cash < target_cash:
            if run_deadline is not None and live_run_budget_expired(
                self.broker,
                reserve_seconds=reserve,
                deadline=run_deadline,
            ):
                msg = (
                    "[Executor] 卖单已确认清空，但现金快照在最终买入预留窗口前仍未更新；"
                    "立即按当前可用现金提交最终买单，可能发生保守截断。"
                    f" available_cash={last_cash:.2f}, expected_cash>={target_cash:.2f}"
                )
                self._emit_warning(msg, level='WARNING')
                return False

            elapsed = time.time() - start_ts
            if elapsed >= wait_seconds:
                msg = (
                    "[Executor] 卖单已确认清空，但现金快照仍低于本轮卖出释放预期；"
                    "继续按当前可用现金提交最终买单，可能发生保守截断。"
                    f" available_cash={last_cash:.2f}, expected_cash>={target_cash:.2f}"
                )
                self._emit_warning(msg, level='WARNING')
                return False

            sleep_seconds = min(poll, max(0.0, wait_seconds - elapsed))
            if run_deadline is None:
                time.sleep(sleep_seconds)
            elif sleep_with_live_run_budget(
                self.broker,
                sleep_seconds,
                reserve_seconds=reserve,
            ) <= 0:
                msg = (
                    "[Executor] 卖单已确认清空，但现金快照在最终买入预留窗口前仍未更新；"
                    "立即按当前可用现金提交最终买单，可能发生保守截断。"
                    f" available_cash={last_cash:.2f}, expected_cash>={target_cash:.2f}"
                )
                self._emit_warning(msg, level='WARNING')
                return False
            if hasattr(self.broker, 'sync_balance'):
                if live_run_budget_expired(self.broker):
                    continue
                try:
                    self.broker.sync_balance()
                except Exception:
                    pass
            last_cash = self._get_available_cash()

        return True

    def _read_position_value(self, data):
        try:
            pos = self.broker.get_position(data)
            size = max(0.0, float(getattr(pos, 'size', 0) or 0))
            if size <= 0:
                return 0.0

            price = 0.0
            try:
                price = float(self.broker.get_current_price(data) or 0.0)
            except Exception:
                price = 0.0

            if price <= 0:
                try:
                    price = float(getattr(pos, 'price', 0) or 0.0)
                except Exception:
                    price = 0.0

            if price <= 0:
                return None
            return size * price
        except Exception:
            return None

    def _rolling_buy_pass(self, increase_plan, released_target_by_symbol):
        if live_run_budget_expired(self.broker):
            return 0

        candidates = []
        for data, target in increase_plan or []:
            if live_run_budget_expired(self.broker):
                return 0
            symbol_key = self._symbol_key(data)
            if not symbol_key:
                continue

            if self._has_pending_buy(data):
                continue

            current_value = self._read_position_value(data)
            if current_value is None:
                continue

            try:
                target_value = float(target)
            except Exception:
                continue

            released_target = float(released_target_by_symbol.get(symbol_key, 0.0) or 0.0)
            if released_target > 0 and current_value < released_target * 0.995:
                continue

            base_value = max(float(current_value), released_target)
            deficit = target_value - base_value
            if deficit <= 0:
                continue

            candidates.append({
                'data': data,
                'current_value': float(current_value),
                'base_value': float(base_value),
                'target_value': target_value,
                'deficit': deficit,
                'symbol_key': symbol_key,
            })

        if not candidates:
            return 0

        available_cash = self._get_available_cash()
        if available_cash <= 0:
            return 0

        cost_multiplier = float(getattr(self.broker, 'safety_multiplier', 1.0) or 1.0)
        usable_budget = available_cash / max(1.0, cost_multiplier)
        if usable_budget <= 0:
            return 0

        total_deficit = sum(item['deficit'] for item in candidates)
        if total_deficit <= 0:
            return 0

        submitted = 0
        for item in candidates:
            if live_run_budget_expired(self.broker):
                break
            alloc = usable_budget * item['deficit'] / total_deficit
            alloc = min(alloc, item['deficit'])
            if alloc <= 0:
                continue

            target_value = item['base_value'] + alloc
            self._log(f"滚动买入: {item['data']._name} -> {target_value:.2f} (释放预算: {alloc:.2f})")
            buy_order = self.broker.order_target_value(data=item['data'], target=target_value)
            if buy_order:
                submitted += 1
                released_target_by_symbol[item['symbol_key']] = float(target_value)

        return submitted

    def _execute_final_buys(self, increase_plan, check_pending=True, released_target_by_symbol=None):
        released_target_by_symbol = released_target_by_symbol or {}
        submitted = 0
        for data, target in increase_plan or []:
            if live_run_budget_expired(self.broker):
                self._emit_warning(
                    "[Executor Warning] Live run execution budget exhausted during final BUY submission; "
                    "accepted partial BUY orders remain active and remaining plan items are skipped.",
                    level='ERROR',
                )
                break
            if check_pending and self._has_pending_buy(data):
                allow_top_up = False
                symbol_key = self._symbol_key(data)
                if symbol_key in released_target_by_symbol:
                    try:
                        released_target = float(released_target_by_symbol.get(symbol_key, 0.0) or 0.0)
                        target_value = float(target)
                        allow_top_up = (
                            released_target < target_value * 0.995
                            and self._has_remote_pending_buy(data)
                        )
                    except Exception:
                        allow_top_up = False
                if not allow_top_up:
                    continue
            if live_run_budget_expired(self.broker):
                self._emit_warning(
                    "[Executor Warning] Live run execution budget exhausted before final BUY submission; "
                    "remaining plan items are skipped.",
                    level='ERROR',
                )
                break
            self._log(f"执行补仓/开仓: {data._name} -> {target:.2f}")
            buy_order = self.broker.order_target_value(data=data, target=target)
            if buy_order:
                submitted += 1
            else:
                self._warn_order_not_submitted('BUY', data, target, phase='increase')
        return submitted

    def _read_position_size(self, data):
        try:
            pos = self.broker.get_position(data)
            return positive_quantity(getattr(pos, 'size', 0) or 0)
        except Exception:
            return None

    @staticmethod
    def _infer_order_ids(order):
        raw_ids = getattr(order, 'batch_order_ids', None)
        if raw_ids is None:
            raw_ids = [getattr(order, 'id', None)]
        elif isinstance(raw_ids, (str, bytes)):
            raw_ids = [raw_ids]

        order_ids = set()
        try:
            candidates = list(raw_ids)
        except TypeError:
            candidates = [raw_ids]
        for raw_id in candidates:
            oid = str(raw_id or '').strip()
            if oid:
                order_ids.add(oid)
        return order_ids

    @staticmethod
    def _infer_order_size(order):
        def _read_path(obj, path):
            cur = obj
            for attr in path:
                if not hasattr(cur, attr):
                    return None
                cur = getattr(cur, attr)
            return cur

        candidate_paths = [
            ('batch_submitted_size',),
            ('submitted_size',),
            ('requested_size',),
            ('platform_order', 'volume'),
            ('raw_order', 'volume'),
            ('trade', 'order', 'totalQuantity'),
        ]

        for path in candidate_paths:
            raw = _read_path(order, path)
            val = positive_quantity(raw)
            if val > 0:
                return val
        return None

    def _build_sell_reconcile_target(self, data, before_size, sell_order, target_value):
        submitted_size = self._infer_order_size(sell_order)
        target_size = None

        if before_size is not None and submitted_size is not None:
            target_size = subtract_quantities(before_size, submitted_size)
        else:
            try:
                if float(target_value) <= 0.0:
                    target_size = 0
            except Exception:
                target_size = None

        if target_size is None:
            return None
        return {
            'data': data,
            'target_size': target_size,
        }

    def _estimate_sell_cash_release(self, data, before_size, sell_order, target_value):
        try:
            price = float(self.broker.get_current_price(data) or 0.0)
        except Exception:
            price = 0.0
        if price <= 0:
            return 0.0

        submitted_size = self._infer_order_size(sell_order)
        if submitted_size is not None:
            return max(0.0, float(submitted_size) * price)

        try:
            before_value = max(0.0, float(before_size) * price)
            target_value = max(0.0, float(target_value))
            return max(0.0, before_value - target_value)
        except Exception:
            return 0.0

    def _wait_sells_settled(self, submitted_sell_ids=None, has_untracked_sell=False,
                             sell_reconcile_targets=None, increase_plan=None,
                             min_final_buy_cash=0.0):
        tracked_ids = {str(x).strip() for x in (submitted_sell_ids or set()) if str(x).strip()}
        reconcile_targets = sell_reconcile_targets or {}
        increase_plan = list(increase_plan or [])
        released_target_by_symbol = {}

        warn_after = max(0.0, float(self._SELL_SETTLE_WARN_SECONDS))
        hard_after = max(float(self._SELL_SETTLE_HARD_SECONDS), warn_after * 2.0)
        poll = max(0.1, float(self._SELL_SETTLE_POLL_SECONDS))
        run_deadline = get_live_run_deadline(self.broker)
        remaining_run_seconds = live_run_seconds_remaining(
            self.broker,
            deadline=run_deadline,
        )
        finalization_reserve = 0.0
        if run_deadline is not None and increase_plan:
            finalization_reserve = min(
                live_run_finalization_reserve(self.broker),
                remaining_run_seconds * 0.5,
            )
        hard_after = min(
            hard_after,
            max(0.0, remaining_run_seconds - finalization_reserve),
        )
        warn_after = min(warn_after, hard_after)
        start_ts = time.time()
        warn_sent = False
        pending_fetch_failures = 0
        synced_balance_once = False
        remote_sell_clear_polls = 0
        rolling_buy_attempted = False
        reconcile_wait_logged = False
        last_unreconciled_targets = []

        while True:
            if live_run_budget_expired(self.broker):
                msg = (
                    "[Executor] Live run execution budget exhausted while waiting for SELL settlement. "
                    "Accepted partial orders remain active; planned final BUY orders are skipped."
                )
                self._emit_warning(msg, level='ERROR')
                return False
            if run_deadline is not None and live_run_budget_expired(
                self.broker,
                reserve_seconds=finalization_reserve,
                deadline=run_deadline,
            ):
                msg = (
                    "[Executor] SELL settlement window ended to preserve finalization time. "
                    "Accepted partial orders remain active; planned final BUY orders are skipped."
                )
                self._emit_warning(msg, level='ERROR')
                return False

            local_pending_ids = set()
            if hasattr(self.broker, '_pending_sells'):
                try:
                    local_pending_ids = {
                        str(x).strip() for x in (getattr(self.broker, '_pending_sells', set()) or set())
                        if str(x).strip()
                    }
                except Exception:
                    local_pending_ids = set()

            pending_orders = []
            pending_orders_loaded = False
            pending_orders_trusted = False
            if hasattr(self.broker, 'get_pending_orders'):
                try:
                    pending_orders = self.broker.get_pending_orders() or []
                    pending_orders_loaded = True
                except Exception as e:
                    pending_fetch_failures += 1
                    print(f"[Executor] 获取在途订单失败，继续基于本地 pending_sells 等待: {e}")
                    pending_orders = []

            if live_run_budget_expired(self.broker):
                msg = (
                    "[Executor] Live run execution budget exhausted during SELL reconciliation. "
                    "Accepted partial orders remain active; planned final BUY orders are skipped."
                )
                self._emit_warning(msg, level='ERROR')
                return False

            pending_fetch_failed = bool(getattr(self.broker, '_last_pending_orders_fetch_failed', False))
            if pending_orders_loaded and not pending_fetch_failed:
                pending_orders_trusted = True
                pending_fetch_failures = 0
            elif pending_orders_loaded and pending_fetch_failed:
                pending_fetch_failures += 1
                err = getattr(self.broker, '_last_pending_orders_fetch_error', None)
                print(f"[Executor] 在途订单查询结果不可信，继续基于本地 pending_sells 等待: {err}")

            remote_pending_sell_ids = set()
            remote_has_pending_sell = False
            for po in pending_orders:
                if not isinstance(po, dict):
                    continue
                direction = str(po.get('direction', '')).strip().upper()
                if direction != 'SELL':
                    continue
                remote_has_pending_sell = True
                poid = str(po.get('id', '') or '').strip()
                if poid:
                    remote_pending_sell_ids.add(poid)

            if pending_orders_trusted and not remote_has_pending_sell:
                remote_sell_clear_polls += 1
            else:
                remote_sell_clear_polls = 0

            combined_pending_sell_ids = local_pending_ids | remote_pending_sell_ids

            if tracked_ids:
                unresolved = {oid for oid in tracked_ids if oid in combined_pending_sell_ids}
                if unresolved and pending_orders_trusted and reconcile_targets:
                    reconciled_ids = set()
                    for oid in unresolved:
                        target_info = reconcile_targets.get(oid)
                        if not target_info:
                            continue
                        data = target_info.get('data')
                        target_size = target_info.get('target_size')
                        current_size = self._read_position_size(data)
                        if current_size is None or target_size is None:
                            continue

                        if quantity_at_most(
                            current_size,
                            target_size,
                            getattr(config, 'LOT_SIZE', 1),
                        ):
                            reconciled_ids.add(oid)

                    if reconciled_ids:
                        if hasattr(self.broker, '_pending_sells'):
                            try:
                                pending_sells = getattr(self.broker, '_pending_sells', set())
                                for oid in reconciled_ids:
                                    pending_sells.discard(oid)
                            except Exception:
                                pass
                        local_pending_ids -= reconciled_ids
                        combined_pending_sell_ids -= reconciled_ids
                        unresolved -= reconciled_ids
                        ids_text = ", ".join(sorted(reconciled_ids))
                        print(f"[Executor] 卖单终态回调缺失，已通过实时持仓/在途单确认卖出完成: {ids_text}")
                        if (
                            hasattr(self.broker, 'sync_balance')
                            and not live_run_budget_expired(self.broker)
                        ):
                            try:
                                self.broker.sync_balance()
                                synced_balance_once = True
                            except Exception as e:
                                print(f"[Executor] 卖单终态后资金同步失败(继续执行): {e}")

                stale_local_ids = {oid for oid in tracked_ids if oid in local_pending_ids}
                if (
                    stale_local_ids
                    and pending_orders_trusted
                    and not remote_has_pending_sell
                    and remote_sell_clear_polls >= 2
                ):
                    if hasattr(self.broker, '_pending_sells'):
                        try:
                            pending_sells = getattr(self.broker, '_pending_sells', set())
                            for oid in stale_local_ids:
                                pending_sells.discard(oid)
                        except Exception:
                            pass
                    local_pending_ids -= stale_local_ids
                    combined_pending_sell_ids -= stale_local_ids
                    ids_text = ", ".join(sorted(stale_local_ids))
                    print(f"[Executor] 卖单终态回调延迟，已通过柜台在途单连续为空确认本地 pending 过期: {ids_text}")
                    if (
                        hasattr(self.broker, 'sync_balance')
                        and not live_run_budget_expired(self.broker)
                    ):
                        try:
                            self.broker.sync_balance()
                            synced_balance_once = True
                        except Exception as e:
                            print(f"[Executor] 卖单终态后资金同步失败(继续执行): {e}")

            sell_state_clear = not (remote_has_pending_sell or bool(local_pending_ids))
            if (tracked_ids or has_untracked_sell) and not pending_orders_trusted:
                sell_state_clear = False
            if sell_state_clear:
                if (
                    hasattr(self.broker, 'sync_balance')
                    and not synced_balance_once
                    and not live_run_budget_expired(self.broker)
                ):
                    try:
                        self.broker.sync_balance()
                        synced_balance_once = True
                    except Exception as e:
                        print(f"[Executor] 卖单终态后资金同步失败(继续执行): {e}")

                unreconciled_targets = []
                seen_targets = set()
                for target_info in reconcile_targets.values():
                    if not target_info:
                        continue
                    data = target_info.get('data')
                    target_size = target_info.get('target_size')
                    target_key = (id(data), target_size)
                    if target_key in seen_targets:
                        continue
                    seen_targets.add(target_key)
                    current_size = self._read_position_size(data)
                    if (
                        current_size is None
                        or target_size is None
                        or not quantity_at_most(
                            current_size,
                            target_size,
                            getattr(config, 'LOT_SIZE', 1),
                        )
                    ):
                        symbol = getattr(data, '_name', 'UNKNOWN')
                        unreconciled_targets.append(
                            f"{symbol}: current={current_size}, target<={target_size}"
                        )

                last_unreconciled_targets = unreconciled_targets
                if unreconciled_targets:
                    sell_state_clear = False
                    if not reconcile_wait_logged:
                        print(
                            "[Executor] SELL pending state cleared but broker positions have not "
                            "yet reached this run's sell targets; continuing bounded reconciliation: "
                            + "; ".join(unreconciled_targets)
                        )
                        reconcile_wait_logged = True
                else:
                    if increase_plan:
                        cash_wait_target = float(min_final_buy_cash or 0.0)
                        planned_cash_target = 0.0
                        for _, target in increase_plan:
                            try:
                                planned_cash_target += max(0.0, float(target))
                            except Exception:
                                continue
                        if planned_cash_target > 0:
                            cash_wait_target = min(cash_wait_target, planned_cash_target)
                        if released_target_by_symbol:
                            cash_wait_target = max(
                                0.0,
                                cash_wait_target - sum(float(v or 0.0) for v in released_target_by_symbol.values())
                            )
                        self._wait_for_post_sell_cash(cash_wait_target)
                        self._execute_final_buys(
                            increase_plan,
                            released_target_by_symbol=released_target_by_symbol,
                        )
                    return True

            elapsed = time.time() - start_ts

            if not warn_sent and warn_after > 0 and elapsed >= warn_after:
                warn_msg = (
                    f"[Executor] 卖单在 {int(warn_after)} 秒内未全部终态，"
                    f"继续等待并按已确认现金滚动买入。"
                )
                print(warn_msg)
                runtime_notifications.push_text(warn_msg, level='WARNING')
                warn_sent = True

            can_roll_buy = (warn_after <= 0 or elapsed >= warn_after) and remote_has_pending_sell
            if increase_plan and can_roll_buy and not rolling_buy_attempted:
                if self._rolling_buy_pass(increase_plan, released_target_by_symbol) > 0:
                    rolling_buy_attempted = True

            if elapsed >= hard_after:
                if not sell_state_clear:
                    if last_unreconciled_targets:
                        msg = (
                            "[Executor] SELL reconciliation did not reach broker position targets "
                            "within the hard wait. Planned final BUY orders are skipped: "
                            + "; ".join(last_unreconciled_targets)
                        )
                    else:
                        msg = (
                            "[Executor] SELL orders still pending after hard wait. "
                            f"Continuing with conservative rolling buys only: "
                            f"local_pending={sorted(local_pending_ids)}, "
                            f"remote_pending={sorted(remote_pending_sell_ids)}, "
                            f"fetch_failures={pending_fetch_failures}"
                        )
                    print(msg)
                    runtime_notifications.push_text(msg, level='ERROR')
                if (
                    hasattr(self.broker, 'sync_balance')
                    and not live_run_budget_expired(self.broker)
                ):
                    try:
                        self.broker.sync_balance()
                    except Exception:
                        pass
                return sell_state_clear

            sleep_seconds = min(poll, max(0.0, hard_after - elapsed))
            if run_deadline is None:
                time.sleep(sleep_seconds)
            elif sleep_with_live_run_budget(
                self.broker,
                sleep_seconds,
                reserve_seconds=finalization_reserve,
            ) <= 0:
                msg = (
                    "[Executor] SELL settlement window ended to preserve finalization time. "
                    "Accepted partial orders remain active; planned final BUY orders are skipped."
                )
                self._emit_warning(msg, level='ERROR')
                return False
