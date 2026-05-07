import config
import time
from alarms.manager import AlarmManager


class PortfolioRebalancer:
    """
    纯粹的持仓平衡计算器。
    它不持有 Broker，不发单，只负责数学计算。
    """

    @staticmethod
    def calculate_plan(current_positions: dict,
                       target_symbols: list,
                       total_capital: float,
                       select_top_k: int,
                       rebalance_threshold: float = 0.05) -> dict:
        """
        生成调仓计划
        :param current_positions: 当前持仓字典 {data_object: market_value}
        :param target_symbols: 目标持仓列表 [data_object]
        :param total_capital: 分配给这些标的的总资金 (已扣除 SGOV 等保留资金)
        :param select_top_k: 目标份数
        :param rebalance_threshold: 调仓阈值 (默认 0.05 即 5%)。只有当持仓偏离目标超过此比例时才触发平衡操作。
        :return: 交易计划字典 {'sell_clear': [], 'reduce': [], 'increase': [], 'target_val_per_stock': float}"""

        # 1. 计算单股目标市值
        if select_top_k <= 0:
            target_value = 0
        else:
            target_value = total_capital / select_top_k

        plan = {
            'sell_clear': [],  # 需要清仓的
            'reduce': [],  # 需要减仓的 (data, target_value)
            'increase': [],  # 需要加仓的 (data, target_value)
            'target_per_stock': target_value
        }

        # 2. 识别清仓与减仓
        for data, current_val in current_positions.items():
            if data not in target_symbols:
                plan['sell_clear'].append(data)
            else:
                # 目标价值为 0 的防御
                if target_value <= 0:
                    plan['sell_clear'].append(data)
                    continue

                diff = target_value - current_val
                # 只有当持仓偏离度超过阈值，才纳入调整计划
                if abs(diff / target_value) > rebalance_threshold:
                    if diff < 0:
                        plan['reduce'].append((data, target_value))
                    else:
                        plan['increase'].append((data, target_value))

        # 3. 识别新开仓
        for data in target_symbols:
            if data not in current_positions:
                if target_value > 0:
                    plan['increase'].append((data, target_value))

        if config.PRINT_PLAN:
            # 终端打印
            plan_md_str = PortfolioRebalancer._log_plan(plan, current_positions, target_symbols, target_value, rebalance_threshold)
            # 推送消息
            AlarmManager().push_plan(plan_md_str)

        return plan

    @staticmethod
    def _log_plan(plan, current_positions, target_symbols, target_value, rebalance_threshold) -> str:
        """构建 Markdown 格式的调仓摘要，受 config.LOG 控制打印，并返回该字符串"""

        # 辅助工具：提取名称并格式化数值，转为易读的字符串拼接
        _n = lambda x: x._name if hasattr(x, '_name') else str(x)
        _fmt_list = lambda items: ", ".join([_n(i) for i in items]) if items else "无"
        _fmt_pair = lambda items: ", ".join([f"{_n(i[0])} → {i[1]:,.0f}" for i in items])
        _curr_pos = ", ".join(
            [f"{_n(k)}: {v:,.0f}" for k, v in current_positions.items()]) if current_positions else "空仓"

        # 拼接 Markdown 字符串数组
        md_lines = [
            "#### 🔄 调仓计划生成",
            f"- **目标市值/股**: `{target_value:,.2f}`",
            f"- **偏离阈值**: `{rebalance_threshold:.1%}`",
            f"- **当前持仓**: `{_curr_pos}`",
            f"- **目标标的**: `{_fmt_list(target_symbols)}`",
            "",
            "##### 📝 执行清单"
        ]

        has_action = False
        if plan['sell_clear']:
            md_lines.append(f"- 🔴 **[清仓]**: {_fmt_list(plan['sell_clear'])}")
            has_action = True

        if plan['reduce']:
            md_lines.append(f"- 🟡 **[减仓]**: {_fmt_pair(plan['reduce'])}")
            has_action = True

        if plan['increase']:
            md_lines.append(f"- 🟢 **[加仓]**: {_fmt_pair(plan['increase'])}")
            has_action = True

        if not has_action:
            md_lines.append("- ✨ **无需调整** (未触及偏离阈值)")

        # 将数组合并为一个完整的 Markdown 字符串
        md_str = "\n".join(md_lines)

        # 受 config.LOG 控制是否在终端打印
        if getattr(config, 'LOG', True):
            print(f"\n{'=' * 20} 调仓计划生成 {'=' * 20}")
            print(md_str)
            print(f"{'=' * 54}\n")

        return md_str

class OrderExecutor:
    """
    专门的执行器，负责把计划变成订单。
    """
    _SELL_SETTLE_WARN_SECONDS = 300.0
    _SELL_SETTLE_POLL_SECONDS = 1.0

    def __init__(self, broker, debug=False):
        self.broker = broker
        self.debug = debug

    def execute_plan(self, plan):
        """执行调仓计划：利用 order_target_value 及其内部智能逻辑"""

        sell_submitted = False
        sell_submit_failed = False
        submitted_sell_ids = set()
        has_untracked_sell = False

        # 第一步：处理所有卖出动作 (清仓 + 减仓)
        # 必须先执行卖出，再等待卖单终态，最大化后续买单资金利用率。
        for data in plan['sell_clear']:
            self._log(f"执行清仓: {data._name}")
            sell_order = self.broker.order_target_value(data=data, target=0.0)
            if sell_order:
                sell_submitted = True
                oid = str(getattr(sell_order, 'id', '') or '').strip()
                if oid:
                    submitted_sell_ids.add(oid)
                else:
                    has_untracked_sell = True
            else:
                sell_submit_failed = True
                self._warn_order_not_submitted('SELL', data, 0.0, phase='clear')

        for data, target in plan['reduce']:
            self._log(f"执行减仓: {data._name} -> {target:.2f}")
            sell_order = self.broker.order_target_value(data=data, target=target)
            if sell_order:
                sell_submitted = True
                oid = str(getattr(sell_order, 'id', '') or '').strip()
                if oid:
                    submitted_sell_ids.add(oid)
                else:
                    has_untracked_sell = True
            else:
                sell_submit_failed = True
                self._warn_order_not_submitted('SELL', data, target, phase='reduce')

        # 第二步：若本轮有卖单，则持续等待直到卖单进入终态。
        # 等待过久只告警，不据此改变交易决策。
        if sell_submitted:
            self._log("等待卖单终态...")
            self._wait_sells_settled(submitted_sell_ids, has_untracked_sell)

        if sell_submit_failed:
            msg = (
                "[Executor Warning] One or more SELL orders were not submitted. "
                "Planned BUY orders are skipped for this rebalance run."
            )
            print(msg)
            try:
                AlarmManager().push_text(msg, level='ERROR')
            except Exception:
                pass
            return

        # 第三步：处理所有买入动作 (补仓/开仓)
        for data, target in plan['increase']:
            self._log(f"执行补仓/开仓: {data._name} -> {target:.2f}")
            buy_order = self.broker.order_target_value(data=data, target=target)
            if not buy_order:
                self._warn_order_not_submitted('BUY', data, target, phase='increase')

    def _log(self, txt):
        if self.debug:
            print(f"[Executor] {txt}")

    @staticmethod
    def _warn_order_not_submitted(side, data, target, phase):
        symbol = getattr(data, '_name', str(data))
        try:
            target_text = f"{float(target):.2f}"
        except Exception:
            target_text = str(target)
        msg = (
            f"[Executor Warning] {side} order not submitted: "
            f"phase={phase}, symbol={symbol}, target={target_text}."
        )
        print(msg)
        try:
            AlarmManager().push_text(msg, level='ERROR')
        except Exception:
            pass

    def _wait_sells_settled(self, submitted_sell_ids=None, has_untracked_sell=False):
        tracked_ids = {str(x).strip() for x in (submitted_sell_ids or set()) if str(x).strip()}

        warn_after = max(0.0, float(self._SELL_SETTLE_WARN_SECONDS))
        poll = max(0.1, float(self._SELL_SETTLE_POLL_SECONDS))
        start_ts = time.time()
        warn_sent = False

        while True:
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
            if hasattr(self.broker, 'get_pending_orders'):
                try:
                    pending_orders = self.broker.get_pending_orders() or []
                except Exception as e:
                    print(f"[Executor] 获取在途订单失败，继续基于本地 pending_sells 等待: {e}")
                    pending_orders = []

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

            combined_pending_sell_ids = local_pending_ids | remote_pending_sell_ids

            # 严格模式：
            # 1) 优先等待本轮提交的卖单 ID 全部离开 pending
            # 2) 若存在无 ID 卖单，退化为任一卖单仍 pending 就继续等待
            if tracked_ids:
                unresolved = {oid for oid in tracked_ids if oid in combined_pending_sell_ids}
                settled = not unresolved
                if settled and has_untracked_sell and (remote_has_pending_sell or bool(local_pending_ids)):
                    settled = False
            else:
                settled = not (remote_has_pending_sell or bool(local_pending_ids))

            if settled:
                if hasattr(self.broker, 'sync_balance'):
                    try:
                        self.broker.sync_balance()
                    except Exception as e:
                        print(f"[Executor] 卖单终态后资金同步失败(继续执行): {e}")
                return True

            if not warn_sent and warn_after > 0 and (time.time() - start_ts) >= warn_after:
                warn_msg = (
                    f"[Executor] 卖单在 {int(warn_after)} 秒内未全部终态，"
                    f"继续等待，不据此跳过买入。"
                )
                print(warn_msg)
                try:
                    AlarmManager().push_text(warn_msg, level='WARNING')
                except Exception:
                    pass
                warn_sent = True

            time.sleep(poll)


from abc import ABC, abstractmethod

class BaseSizingMethod(ABC):
    @abstractmethod
    def calculate_weights(self, target_symbols, context_data) -> dict:
        """返回 {symbol: weight_percent}"""
        pass

# 1. 等权
class EqualWeightSizing(BaseSizingMethod):
    def calculate_weights(self, target_symbols, context_data):
        count = len(target_symbols)
        return {s: 1.0/count for s in target_symbols} if count > 0 else {}

# 2. 波动率倒数加权
class VolatilityWeightedSizing(BaseSizingMethod):
    def calculate_weights(self, target_symbols, context_data):
        # context_data 需包含 ATR 或 std 数据
        inverses = {s: 1.0/context_data[s]['atr'] for s in target_symbols}
        total_inv = sum(inverses.values())
        return {s: val/total_inv for s, val in inverses.items()}
