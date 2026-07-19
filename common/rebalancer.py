from abc import ABC, abstractmethod

import config


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
        target_data_ids = {id(data) for data in target_symbols}
        current_data_ids = {id(data) for data in current_positions}

        # 2. 识别清仓与减仓
        for data, current_val in current_positions.items():
            if id(data) not in target_data_ids:
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
            if id(data) not in current_data_ids and target_value > 0:
                plan['increase'].append((data, target_value))

        return plan

    @staticmethod
    def _log_plan(plan, current_positions, target_symbols, target_value, rebalance_threshold) -> str:
        """构建 Markdown 格式的调仓摘要，受 config.LOG 控制打印，并返回该字符串"""

        _n = lambda x: x._name if hasattr(x, '_name') else str(x)
        _fmt_list = lambda items: ", ".join([_n(i) for i in items]) if items else "无"
        _fmt_pair = lambda items: ", ".join([f"{_n(i[0])} → {i[1]:,.0f}" for i in items])
        _curr_pos = ", ".join(
            [f"{_n(k)}: {v:,.0f}" for k, v in current_positions.items()]
        ) if current_positions else "空仓"

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

        md_str = "\n".join(md_lines)

        if getattr(config, 'LOG', True):
            print(f"\n{'=' * 20} 调仓计划生成 {'=' * 20}")
            print(md_str)
            print(f"{'=' * 54}\n")

        return md_str


class BaseSizingMethod(ABC):
    @abstractmethod
    def calculate_weights(self, target_symbols, context_data) -> dict:
        """返回 {symbol: weight_percent}"""
        pass

# 1. 等权
class EqualWeightSizing(BaseSizingMethod):
    def calculate_weights(self, target_symbols, context_data):
        count = len(target_symbols)
        return {s: 1.0 / count for s in target_symbols} if count > 0 else {}

# 2. 波动率倒数加权
class VolatilityWeightedSizing(BaseSizingMethod):
    def calculate_weights(self, target_symbols, context_data):
        inverses = {s: 1.0 / context_data[s]['atr'] for s in target_symbols}
        total_inv = sum(inverses.values())
        return {s: val / total_inv for s, val in inverses.items()}
