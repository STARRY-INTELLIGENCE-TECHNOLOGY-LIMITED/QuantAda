from risk_controls.base_risk_control import BaseRiskControl


class SampleMaxDrawdownKillSwitch(BaseRiskControl):
    """
    全局最大回撤拔网线模块 (Portfolio Kill Switch)
    """

    params = {
        "max_dd_tolerance": 0.15,  # 容忍的最大回撤比例 (例如 15%)
    }

    def __init__(self, broker, params=None):
        super().__init__(broker, params)
        self.peak_value = None  # 记录账户历史最高净值
        self.plug_pulled = False  # 网线是否已被拔掉
        self._invalid_value_warned = False

    def check(self, data) -> str:
        # 1. 致命伤锁定：如果网线已经拔了，永远返回 SELL，拒绝任何持仓
        if self.plug_pulled:
            return "SELL"

        # 2. 如果用户显式传入 --cash，优先用它作为初始峰值
        if self.peak_value is None:
            cash_override = self._resolve_cash_override()
            if cash_override is not None:
                self.peak_value = cash_override

        # 3. 获取当前账户总资产 (上帝视角)
        current_value = self._safe_getvalue()
        if current_value is None:
            return None

        # 4. 更新历史最高净值
        if self.peak_value is None or current_value > self.peak_value:
            self.peak_value = current_value

        if not self.peak_value or self.peak_value <= 0:
            return None

        # 5. 计算当前回撤
        current_dd = (self.peak_value - current_value) / self.peak_value

        # 6. 触发拔网线逻辑
        if current_dd >= self.p.max_dd_tolerance:
            self.plug_pulled = True
            self._log_trigger(current_value, current_dd)
            return "SELL"

        return None

    def _resolve_cash_override(self):
        override = getattr(self.broker, "_cash_override", None)
        if override is None:
            return None
        try:
            override = float(override)
        except (TypeError, ValueError):
            self._log_once(f"[RiskControl] cash override invalid: {override!r}")
            return None
        if override <= 0:
            self._log_once(f"[RiskControl] cash override non-positive: {override!r}")
            return None
        return override

    def _safe_getvalue(self):
        try:
            value = self.broker.getvalue()
        except Exception as exc:  # pragma: no cover - 防御性保护
            self._log_once(f"[RiskControl] getvalue failed: {exc}")
            return None

        if value is None:
            self._log_once("[RiskControl] getvalue returned None; skip drawdown check.")
            return None

        try:
            value = float(value)
        except (TypeError, ValueError):
            self._log_once(f"[RiskControl] getvalue returned non-numeric value: {value!r}")
            return None

        if value <= 0:
            self._log_once(f"[RiskControl] getvalue returned non-positive value: {value!r}")
            return None

        return value

    def _log_once(self, message: str):
        if self._invalid_value_warned:
            return
        self._invalid_value_warned = True
        broker = getattr(self, "broker", None)
        if broker and hasattr(broker, "log"):
            broker.log(message)

    def _log_trigger(self, current_value: float, current_dd: float):
        broker = getattr(self, "broker", None)
        if broker and hasattr(broker, "log"):
            broker.log(
                f"[RiskControl] Max drawdown exceeded. "
                f"Peak={self.peak_value:.2f}, Current={current_value:.2f}, "
                f"DD={current_dd:.2%}, Tolerance={self.p.max_dd_tolerance:.2%}. "
                f"Kill switch armed."
            )
