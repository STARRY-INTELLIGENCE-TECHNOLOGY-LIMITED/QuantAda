"""实盘期权组合风险监控；只设置开仓闸门，不直接提交订单。"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskWatchdogResult:
    """一次风险快照检查结果。"""

    healthy: bool
    blocked: bool
    reasons: tuple[str, ...]


def _finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


class OptionRiskWatchdog:
    """按固定间隔读取 Broker 事实快照并设置开仓 kill-switch。

    Broker 必须提供 ``get_option_risk_snapshot``。快照失败、字段缺失或非有限
    值均视为不可信并阻断新开仓；平仓和撤单路径不读取此闸门。
    """

    def __init__(self, broker, *, interval_seconds=60.0, max_gamma=None,
                 max_margin_utilization=0.90, max_spread_pct=0.50):
        self.broker = broker
        interval = _finite(interval_seconds)
        margin_limit = _finite(max_margin_utilization)
        spread_limit = _finite(max_spread_pct)
        if interval is None or interval <= 0:
            raise ValueError("interval_seconds must be positive and finite")
        if margin_limit is None or margin_limit < 0:
            raise ValueError("max_margin_utilization must be non-negative and finite")
        if spread_limit is None or spread_limit < 0:
            raise ValueError("max_spread_pct must be non-negative and finite")
        gamma_limit = None if max_gamma in (None, "") else _finite(max_gamma)
        if max_gamma not in (None, "") and (gamma_limit is None or gamma_limit < 0):
            raise ValueError("max_gamma must be non-negative and finite")
        self.interval_seconds = max(0.1, interval)
        self.max_gamma = gamma_limit
        self.max_margin_utilization = margin_limit
        self.max_spread_pct = spread_limit
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self.last_result = None
        self._last_alarm_key = None

    @property
    def running(self):
        thread = self._thread
        return bool(thread and thread.is_alive())

    def _log(self, message, level="WARNING"):
        logger = getattr(self.broker, "_runtime_log", None)
        if callable(logger):
            logger(message)
        try:
            from common import runtime_notifications

            runtime_notifications.push_text(message, level=level)
        except Exception:
            pass

    def _set_block(self, blocked, reason):
        setter = getattr(self.broker, "set_option_entry_kill_switch", None)
        if callable(setter):
            try:
                setter(bool(blocked), reason=reason, source="watchdog")
                return
            except TypeError:
                # 旧适配器可能没有 source 参数，但若已暴露来源字典，仍
                # 必须在这里按来源更新，不能让兼容调用清掉其它闸门。
                blocks = getattr(self.broker, "_option_entry_blocks", None)
                if isinstance(blocks, dict):
                    if blocked:
                        blocks["watchdog"] = str(reason or "watchdog")
                    else:
                        blocks.pop("watchdog", None)
                    self.broker._option_entry_kill_switch = bool(blocks)
                    self.broker._option_entry_kill_reason = "; ".join(
                        f"{name}: {detail}" for name, detail in blocks.items()
                    ) or None
                    return
                # 兼容只实现旧签名的最小测试/第三方 Broker。
                try:
                    setter(bool(blocked), reason=reason)
                except TypeError:
                    setter(bool(blocked))
        else:
            blocks = getattr(self.broker, "_option_entry_blocks", None)
            if not isinstance(blocks, dict):
                blocks = {}
                self.broker._option_entry_blocks = blocks
            if blocked:
                blocks["watchdog"] = str(reason or "watchdog")
            else:
                blocks.pop("watchdog", None)
            self.broker._option_entry_kill_switch = bool(blocks)
            self.broker._option_entry_kill_reason = str(reason or "") or None

    def evaluate(self, snapshot=None) -> RiskWatchdogResult:
        """同步检查一次快照，供测试和引擎启动时使用。"""
        reasons = []
        if snapshot is None:
            getter = getattr(self.broker, "get_option_risk_snapshot", None)
            if not callable(getter):
                reasons.append("risk_snapshot_unsupported")
            else:
                try:
                    snapshot = getter()
                except Exception as exc:
                    snapshot = None
                    reasons.append(f"risk_snapshot_error:{exc}")
        if not isinstance(snapshot, dict):
            if not reasons:
                reasons.append("risk_snapshot_untrusted")
        else:
            if snapshot.get("supported", True) is not True:
                reasons.append("risk_snapshot_unsupported")
            trusted = snapshot.get("trusted", True)
            if trusted is not True:
                reasons.append(str(snapshot.get("error") or "risk_snapshot_untrusted"))

            gamma = snapshot.get("portfolio_gamma", snapshot.get("gamma"))
            if self.max_gamma is not None:
                gamma_value = _finite(gamma)
                if gamma_value is None:
                    reasons.append("gamma_unavailable")
                elif abs(gamma_value) > abs(self.max_gamma):
                    reasons.append(f"gamma_limit:{gamma_value}")

            utilization = snapshot.get("margin_utilization", snapshot.get("buying_power_utilization"))
            if utilization is None:
                margin_used = _finite(snapshot.get("margin_used"))
                cash = _finite(snapshot.get("cash"))
                if margin_used is not None and cash is not None and cash > 0:
                    utilization = margin_used / cash
            utilization_value = _finite(utilization)
            if utilization_value is None:
                reasons.append("margin_utilization_unavailable")
            elif utilization_value < 0:
                reasons.append("margin_utilization_invalid")
            elif utilization_value > self.max_margin_utilization:
                reasons.append(f"margin_utilization_limit:{utilization_value}")

            spread = snapshot.get("max_spread_pct", snapshot.get("spread_pct"))
            if spread is None and snapshot.get("spreads"):
                values = [_finite(value) for value in snapshot.get("spreads") or ()]
                values = [value for value in values if value is not None]
                if not values:
                    reasons.append("spread_unavailable")
                else:
                    spread = max(values)
            if spread is None and snapshot.get("has_options"):
                reasons.append("spread_unavailable")
            if spread is not None:
                spread_value = _finite(spread)
                if spread_value is None:
                    reasons.append("spread_unavailable")
                elif spread_value < 0:
                    reasons.append("spread_invalid")
                elif spread_value > self.max_spread_pct:
                    reasons.append(f"spread_limit:{spread_value}")

        blocked = bool(reasons)
        result = RiskWatchdogResult(
            healthy=not blocked,
            blocked=blocked,
            reasons=tuple(reasons),
        )
        with self._lock:
            self.last_result = result
        # 健康快照只清除 Watchdog 自己的来源闸门，不能清除清算层或人工
        # 设置的其它闸门；短暂故障恢复后允许实盘自愈继续运行。
        if blocked:
            self._set_block(True, "; ".join(reasons))
        else:
            self._set_block(False, "")
        if blocked:
            alarm_key = tuple(result.reasons)
            if alarm_key != self._last_alarm_key:
                self._log(f"[Option Risk Watchdog] new entries blocked: {result.reasons}", level="ERROR")
            self._last_alarm_key = alarm_key
        else:
            self._last_alarm_key = None
        return result

    def reset(self):
        """立即清除 Watchdog 来源闸门；不会修改券商仓位或订单。"""
        self._set_block(False, "")
        self._last_alarm_key = None

    def _run(self):
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.evaluate()
            except Exception as exc:
                self._set_block(True, f"watchdog_error:{exc}")
                self._log(f"[Option Risk Watchdog] check failed: {exc}", level="ERROR")

    def start(self):
        if self.running:
            return self
        self._stop_event.clear()
        self.evaluate()
        self._thread = threading.Thread(
            target=self._run,
            name="quantada-option-risk-watchdog",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(max(0.0, float(timeout)))
        self._thread = None


__all__ = ["RiskWatchdogResult", "OptionRiskWatchdog"]
