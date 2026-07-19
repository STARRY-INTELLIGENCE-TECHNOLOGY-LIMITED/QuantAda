import atexit
import datetime
import math
import pandas as pd
import signal
import socket
import sys
import threading
import time
from typing import Dict, Tuple
from zoneinfo import ZoneInfo

import config
from .base_alarm import BaseAlarm
from .dingtalk_alarm import DingTalkAlarm
from .wecom_alarm import WeComAlarm


class AlarmManager:
    _instance = None
    _lock = threading.Lock()
    _WINDOW_BYPASS_ALARM_TAGS = frozenset({
        BaseAlarm.TAG_LIFECYCLE,
        BaseAlarm.TAG_PLAN,
    })
    _LIFECYCLE_STATUSES = frozenset({"STARTED", "DEAD", "STOPPED"})
    _EXCEPTION_AGGREGATION_WINDOW_SECONDS = 60
    _COOLDOWN_BASE_DELAY_SECONDS = 30
    _COOLDOWN_MAX_DELAY_SECONDS = 10 * 60
    _COOLDOWN_RESET_WINDOW_SECONDS = 15 * 60

    def __new__(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(AlarmManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized: return

        self.alarms = []
        alarms_enabled = (
            config.is_alarms_enabled()
            if hasattr(config, "is_alarms_enabled")
            else bool(getattr(config, "ALARMS_ENABLED", False))
        )
        if alarms_enabled:
            # 加载钉钉
            if config.DINGTALK_WEBHOOK:
                self.alarms.append(DingTalkAlarm())
            # 加载企业微信
            if config.WECOM_WEBHOOK:
                self.alarms.append(WeComAlarm())

        # 运行时身份上下文
        self.host_name = socket.gethostname()
        self.context_tag = ""  # 例如: [IB:7497]
        self.context_detail = ""  # 例如: 策略参数详情
        self._schedule_alarm_rule = None
        self._parsed_schedule_alarm_rule = None
        self._schedule_alarm_timezone = ""
        self._schedule_alarm_window_before_seconds = 0.0
        self._schedule_alarm_window_after_seconds = 0.0
        self._terminal_lifecycle_status = None
        self._schedule_api_unavailable_alarm_keys = set()

        # 异常报警聚合: 默认开启，固定 60 秒窗口内合并相同(context, error)
        self._exception_aggregation_window_seconds = self._EXCEPTION_AGGREGATION_WINDOW_SECONDS
        self._exception_aggregation_lock = threading.Lock()
        self._exception_aggregation_buffer: Dict[Tuple[str, str], int] = {}
        self._exception_flush_timer = None

        # 二级冷却(分钟聚合后): 对重复报警进行对数回退推送，降低告警疲劳
        self._cooldown_base_delay_seconds = self._COOLDOWN_BASE_DELAY_SECONDS
        self._cooldown_max_delay_seconds = self._COOLDOWN_MAX_DELAY_SECONDS
        self._cooldown_reset_window_seconds = self._COOLDOWN_RESET_WINDOW_SECONDS

        self._exception_cooldown_lock = threading.Lock()
        self._exception_cooldown_state: Dict[Tuple[str, str], Dict[str, float]] = {}
        self._exception_cooldown_timer = None
        self._exception_cooldown_day = time.strftime("%Y-%m-%d", time.localtime())

        self._register_dead_letter_handlers()
        self._initialized = True
        print(f"[AlarmManager] Initialized with {len(self.alarms)} channels.")

    # 供 Launcher 调用，注入身份信息
    @staticmethod
    def _parse_schedule_alarm_window(raw_value) -> tuple[float, float]:
        from live_trader.data_bridge.data_warm import SchedulePlanner

        if raw_value in (None, "", "0:0"):
            return 0.0, 0.0

        raw = str(raw_value).strip().lower()
        if not raw:
            return 0.0, 0.0
        if ":" not in raw:
            raise ValueError(
                f"Invalid schedule alarm window format: {raw_value}. "
                "Expected like '30m:15m' or '1h:30s'."
            )

        before_raw, after_raw = raw.split(":", 1)
        before_seconds = SchedulePlanner.parse_schedule_prewarm_lead(before_raw.strip() or 0)
        after_seconds = SchedulePlanner.parse_schedule_prewarm_lead(after_raw.strip() or 0)
        return before_seconds, after_seconds

    def _current_schedule_alarm_time(self):
        tz_name = str(getattr(self, "_schedule_alarm_timezone", "") or "").strip()
        if tz_name:
            try:
                return datetime.datetime.now(ZoneInfo(tz_name))
            except Exception as e:
                print(f"[AlarmManager] Invalid schedule timezone '{tz_name}': {e}. Falling back to local time.")
        return datetime.datetime.now()

    @staticmethod
    def _resolve_last_schedule_slot_for_day(anchor_dt, parsed_schedule):
        slot_ts = pd.Timestamp(anchor_dt)
        if parsed_schedule.get('kind') == 'daily':
            return slot_ts

        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        if interval_seconds <= 0:
            return None

        day_end = slot_ts.normalize() + pd.Timedelta(days=1)
        remaining_seconds = max(0.0, (day_end - slot_ts).total_seconds())
        last_slot_index = int(max(0.0, remaining_seconds - 1e-9) // interval_seconds)
        return slot_ts + pd.Timedelta(seconds=last_slot_index * interval_seconds)

    @classmethod
    def _resolve_previous_schedule_slot(cls, now, parsed_schedule):
        from live_trader.data_bridge.data_warm import SchedulePlanner

        now_ts = pd.Timestamp(now)
        anchor_dt = SchedulePlanner.schedule_anchor_for_day(now_ts, parsed_schedule)

        if parsed_schedule.get('kind') == 'daily':
            if now_ts < anchor_dt:
                return anchor_dt - pd.Timedelta(days=1)
            return anchor_dt

        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        if interval_seconds <= 0:
            return None
        if now_ts < anchor_dt:
            prev_anchor_dt = SchedulePlanner.schedule_anchor_for_day(now_ts - pd.Timedelta(days=1), parsed_schedule)
            return cls._resolve_last_schedule_slot_for_day(prev_anchor_dt, parsed_schedule)

        elapsed_seconds = max(0.0, (now_ts - anchor_dt).total_seconds())
        slot_index = int(elapsed_seconds // interval_seconds)
        return anchor_dt + pd.Timedelta(seconds=slot_index * interval_seconds)

    @staticmethod
    def _normalize_alarm_tag(alarm_tag=None) -> str:
        raw = str(alarm_tag or BaseAlarm.TAG_GENERAL).strip().lower()
        return raw or BaseAlarm.TAG_GENERAL

    @classmethod
    def _resolve_status_alarm_tag(cls, status, alarm_tag=None) -> str:
        if alarm_tag is not None:
            return cls._normalize_alarm_tag(alarm_tag)

        status_text = str(status or "").strip().upper()
        if status_text in cls._LIFECYCLE_STATUSES:
            return BaseAlarm.TAG_LIFECYCLE
        return BaseAlarm.TAG_GENERAL

    def _should_dispatch_now(self, now=None, alarm_tag=None) -> bool:
        normalized_tag = self._normalize_alarm_tag(alarm_tag)
        if normalized_tag in self._WINDOW_BYPASS_ALARM_TAGS:
            return True
        if (self._schedule_alarm_window_before_seconds <= 0
                and self._schedule_alarm_window_after_seconds <= 0):
            return True
        if not self._schedule_alarm_rule or not self._parsed_schedule_alarm_rule:
            return True

        from live_trader.data_bridge.data_warm import SchedulePlanner

        now_ts = pd.Timestamp(now or self._current_schedule_alarm_time())
        parsed_schedule = self._parsed_schedule_alarm_rule
        candidate_slots = [
            self._resolve_previous_schedule_slot(now_ts, parsed_schedule),
            SchedulePlanner.resolve_next_schedule_slot(now_ts, parsed_schedule),
        ]

        before_delta = pd.Timedelta(seconds=float(self._schedule_alarm_window_before_seconds))
        after_delta = pd.Timedelta(seconds=float(self._schedule_alarm_window_after_seconds))

        for slot_dt in candidate_slots:
            if slot_dt is None:
                continue
            slot_ts = pd.Timestamp(slot_dt)
            if (slot_ts - before_delta) <= now_ts <= (slot_ts + after_delta):
                return True
        return False

    def set_runtime_context(self, broker, conn_id, strategy, params, market_scope="",
                            schedule_rule=None, schedule_timezone=None, alarm_window=None):
        """
        设置运行时上下文，用于区分多实例
        """
        self.context_tag = f"[{broker.upper()}:{conn_id}]"
        self._schedule_alarm_rule = str(schedule_rule or '').strip() or None
        self._schedule_alarm_timezone = str(schedule_timezone or '').strip()
        self._parsed_schedule_alarm_rule = None
        self._schedule_alarm_window_before_seconds = 0.0
        self._schedule_alarm_window_after_seconds = 0.0
        self._schedule_api_unavailable_alarm_keys.clear()

        try:
            before_seconds, after_seconds = self._parse_schedule_alarm_window(
                getattr(config, 'LIVE_SCHEDULE_ALARM_WINDOW', '0:0')
                if alarm_window is None else alarm_window
            )
            self._schedule_alarm_window_before_seconds = before_seconds
            self._schedule_alarm_window_after_seconds = after_seconds
        except Exception as e:
            print(f"[AlarmManager] Invalid schedule alarm window: {e}. Alarm window disabled.")

        if self._schedule_alarm_rule:
            try:
                from live_trader.data_bridge.data_warm import SchedulePlanner
                self._parsed_schedule_alarm_rule = SchedulePlanner.parse_schedule_rule(self._schedule_alarm_rule)
            except Exception as e:
                print(f"[AlarmManager] Invalid schedule rule for alarm window: {self._schedule_alarm_rule}. Error: {e}")
                self._parsed_schedule_alarm_rule = None

        # 格式化参数详情，便于在报警中查看
        param_str = ", ".join([f"{k}={v}" for k, v in params.items()]) if params else "None"
        market_str = market_scope if market_scope else "N/A"
        detail_lines = [
            f"Machine: {self.host_name}\n"
            f"Strategy: {strategy}\n"
            f"Market: {market_str}\n"
            f"Params: {param_str}"
        ]
        if self._schedule_alarm_rule:
            detail_lines.append(f"Schedule: {self._schedule_alarm_rule}")
        if (self._schedule_alarm_window_before_seconds > 0
                or self._schedule_alarm_window_after_seconds > 0):
            detail_lines.append(
                "AlarmWindow: "
                f"-{self._schedule_alarm_window_before_seconds:.0f}s/+{self._schedule_alarm_window_after_seconds:.0f}s"
            )
        self.context_detail = "\n".join(detail_lines)

    def _register_dead_letter_handlers(self):
        """注册死信监听 (Ctrl+C, Kill, 正常退出)"""

        # 1. 正常退出钩子
        atexit.register(self._on_exit)

        # 2. 信号监听 (Ctrl+C, Kill)
        # 注意：在 Windows 下某些信号可能受限，但在 Linux/Docker 中很有效
        try:
            signal.signal(signal.SIGINT, self._signal_handler)  # Ctrl+C
            signal.signal(signal.SIGTERM, self._signal_handler)  # Kill
        except ValueError:
            # 说明可能不在主线程运行，跳过信号注册
            pass

    def _on_exit(self):
        """程序正常结束的回调"""
        # 只有在非异常退出时才发 Finish，如果是 crash，会被 Exception 捕获
        # 这里可以发一个简单的结束报告
        self._flush_exception_aggregation()
        self._flush_exception_cooldown_pending(force=True)
        if self._terminal_lifecycle_status in {"DEAD", "STOPPED"}:
            return
        self._terminal_lifecycle_status = "STOPPED"
        self.push_status("STOPPED", "Program exited normally.")
        pass

    def _signal_handler(self, sig, frame):
        """捕获中断信号"""
        sig_name = "SIGINT (Ctrl+C)" if sig == signal.SIGINT else "SIGTERM"
        print(f"\n[AlarmManager] Caught signal {sig_name}. Sending Dead Letter...")
        self._terminal_lifecycle_status = "DEAD"
        self.push_status("DEAD", f"Process killed by {sig_name}")
        sys.exit(0)

    # --- 统一推送接口 ---

    def push_text(self, content, level='INFO', alarm_tag=None):
        if not self.alarms:
            return
        if not self._should_dispatch_now(alarm_tag=alarm_tag):
            return

        if self.context_tag:
            content = f"""### {self.context_tag}
{content}         
"""

        self._dispatch_text(content, level)

    def push_plan(self, content, level='INFO'):
        self.push_text(content, level=level, alarm_tag=BaseAlarm.TAG_PLAN)

    def push_schedule_api_unavailable(self, broker_name, reason, now=None, prewarm_lead_seconds=None,
                                      schedule_tolerance_seconds=60.0):
        if not self.alarms:
            return []
        if not self._schedule_alarm_rule or not self._parsed_schedule_alarm_rule:
            return []

        from live_trader.data_bridge.data_warm import SchedulePlanner

        now_ts = pd.Timestamp(now or self._current_schedule_alarm_time())
        try:
            lead_seconds = (
                SchedulePlanner.parse_schedule_prewarm_lead(
                    getattr(config, 'LIVE_SCHEDULE_PREWARM_LEAD', 0)
                )
                if prewarm_lead_seconds is None
                else SchedulePlanner.parse_schedule_prewarm_lead(prewarm_lead_seconds)
            )
        except Exception:
            lead_seconds = 0.0

        sent = []

        def _push(event_name, slot_key, impact):
            if not slot_key:
                return
            alarm_key = f"schedule_api_unavailable:{broker_name}:{event_name}:{self._schedule_alarm_rule}:{slot_key}"
            if alarm_key in self._schedule_api_unavailable_alarm_keys:
                return
            self._schedule_api_unavailable_alarm_keys.add(alarm_key)
            if len(self._schedule_api_unavailable_alarm_keys) > 5000:
                self._schedule_api_unavailable_alarm_keys.clear()
                self._schedule_api_unavailable_alarm_keys.add(alarm_key)

            msg = (
                f"[{broker_name} Error] Broker API unavailable during schedule {event_name}. "
                f"schedule={self._schedule_alarm_rule}, slot={slot_key}, "
                f"now={now_ts.strftime('%Y-%m-%d %H:%M:%S')}, reason={reason}. "
                f"{impact}; process will keep reconnecting."
            )
            print(msg)
            content = msg
            if self.context_tag:
                content = f"""### {self.context_tag}
{msg}         
"""
            self._dispatch_text(content, 'ERROR')
            sent.append({"event": event_name, "slot_key": slot_key, "message": msg})

        parsed_schedule = self._parsed_schedule_alarm_rule
        if lead_seconds > 0:
            try:
                should_prewarm, _, prewarm_slot_key = SchedulePlanner.should_trigger_schedule_prewarm_for_rule(
                    now=now_ts,
                    parsed_schedule=parsed_schedule,
                    lead_seconds=lead_seconds,
                    last_prewarm_run_key=None,
                    last_schedule_run_key=None,
                )
                if should_prewarm:
                    _push("prewarm", prewarm_slot_key, "schedule prewarm may be missed")
            except Exception as exc:
                print(f"[AlarmManager] failed to evaluate schedule prewarm API alarm: {exc}")

        try:
            tolerance = max(0.0, float(schedule_tolerance_seconds or 0.0))
        except Exception:
            tolerance = 60.0
        if parsed_schedule.get('kind') == 'interval':
            try:
                tolerance = max(tolerance, float(parsed_schedule.get('interval_seconds') or 0.0))
            except Exception:
                pass
        else:
            tolerance = max(tolerance, 60.0)

        try:
            should_run, _, run_slot_key = SchedulePlanner.should_trigger_schedule(
                now=now_ts,
                parsed_schedule=parsed_schedule,
                last_schedule_run_key=None,
                tolerance_window=tolerance,
            )
            if should_run:
                _push("run", run_slot_key, "strategy run may be missed")
        except Exception as exc:
            print(f"[AlarmManager] failed to evaluate schedule run API alarm: {exc}")

        return sent

    def push_exception(self, context, error, alarm_tag=None):
        if not self.alarms:
            return
        if not self._should_dispatch_now(alarm_tag=alarm_tag):
            return

        full_context = f"{self.context_tag} {context} @ {self.host_name}"
        error_text = str(error).strip()
        self._buffer_exception_alarm(full_context, error_text)

    def push_trade(self, order_info, alarm_tag=BaseAlarm.TAG_TRADE):
        if not self.alarms:
            return
        if not self._should_dispatch_now(alarm_tag=alarm_tag):
            return
        for alarm in self.alarms:
            threading.Thread(target=alarm.push_trade, args=(order_info,)).start()

    def push_start(self, strategy_name):
        detail = self.context_detail if self.context_detail else f"Strategy: {strategy_name}"
        self.push_status("STARTED", detail, alarm_tag=BaseAlarm.TAG_LIFECYCLE)

    def push_status(self, status, detail="", alarm_tag=None):
        if not self.alarms:
            return
        resolved_alarm_tag = self._resolve_status_alarm_tag(status, alarm_tag=alarm_tag)
        if not self._should_dispatch_now(alarm_tag=resolved_alarm_tag):
            return
        status_text = str(status or "").strip().upper()
        if resolved_alarm_tag == BaseAlarm.TAG_LIFECYCLE and status_text in {"DEAD", "STOPPED"}:
            self._terminal_lifecycle_status = status_text
        full_status = f"{status} {self.context_tag}" if self.context_tag else status

        # 如果 detail 里没有包含机器信息，自动补全 (防止重复叠加)
        full_detail = detail
        if self.host_name not in detail:
            if self.context_detail:
                full_detail = f"{detail}\n---\n{self.context_detail}"
            else:
                full_detail = f"{detail}\nHost: {self.host_name}"

        for alarm in self.alarms:
            threading.Thread(target=alarm.push_status, args=(full_status, full_detail)).start()

    def _dispatch_text(self, content, level):
        for alarm in self.alarms:
            threading.Thread(target=alarm.push_text, args=(content, level)).start()

    @staticmethod
    def _merge_exception_content(error_text, count):
        if count > 1:
            return f"{error_text}\n重复次数: {count}"
        return error_text

    def _compute_log_cooldown_delay(self, k):
        delay = self._cooldown_base_delay_seconds * (1 + math.log1p(max(0, k)))
        return min(self._cooldown_max_delay_seconds, delay)

    @staticmethod
    def _build_new_cooldown_state(now):
        return {
            "k": 0,
            "pending_count": 0,
            "next_allowed_at": now,
            "last_seen_at": now,
        }

    def _maybe_reset_exception_cooldown_day_locked(self):
        current_day = time.strftime("%Y-%m-%d", time.localtime())
        if current_day == self._exception_cooldown_day:
            return
        self._exception_cooldown_day = current_day
        self._exception_cooldown_state.clear()
        if self._exception_cooldown_timer and self._exception_cooldown_timer.is_alive():
            self._exception_cooldown_timer.cancel()
        self._exception_cooldown_timer = None

    def _prune_exception_cooldown_state_locked(self, now):
        stale_keys = []
        for key, state in self._exception_cooldown_state.items():
            if state["pending_count"] > 0:
                continue
            if now - state["last_seen_at"] >= self._cooldown_reset_window_seconds:
                stale_keys.append(key)
        for key in stale_keys:
            self._exception_cooldown_state.pop(key, None)

    def _reschedule_exception_cooldown_timer_locked(self, now):
        if self._exception_cooldown_timer and self._exception_cooldown_timer.is_alive():
            self._exception_cooldown_timer.cancel()
        self._exception_cooldown_timer = None

        next_fire_at = None
        for state in self._exception_cooldown_state.values():
            if state["pending_count"] <= 0:
                continue
            next_allowed_at = state["next_allowed_at"]
            if next_fire_at is None or next_allowed_at < next_fire_at:
                next_fire_at = next_allowed_at

        if next_fire_at is None:
            return

        delay = max(0.01, next_fire_at - now)
        self._exception_cooldown_timer = threading.Timer(delay, self._flush_exception_cooldown_pending)
        self._exception_cooldown_timer.daemon = True
        self._exception_cooldown_timer.start()

    def _flush_exception_cooldown_pending(self, force=False):
        dispatch_items = []
        now = time.time()
        with self._exception_cooldown_lock:
            if self._exception_cooldown_timer and self._exception_cooldown_timer.is_alive():
                self._exception_cooldown_timer.cancel()
            self._exception_cooldown_timer = None
            self._maybe_reset_exception_cooldown_day_locked()

            for (context, error_text), state in self._exception_cooldown_state.items():
                if state["pending_count"] <= 0:
                    continue
                if not force and now < state["next_allowed_at"]:
                    continue

                dispatch_count = int(state["pending_count"])
                state["pending_count"] = 0
                state["last_seen_at"] = now
                dispatch_items.append((context, error_text, dispatch_count))

                if force:
                    state["k"] = 0
                    state["next_allowed_at"] = now
                else:
                    state["k"] += 1
                    state["next_allowed_at"] = now + self._compute_log_cooldown_delay(state["k"])

            if force:
                self._exception_cooldown_state.clear()
            else:
                self._prune_exception_cooldown_state_locked(now)
            self._reschedule_exception_cooldown_timer_locked(now)

        for context, error_text, count in dispatch_items:
            self._dispatch_exception(context, self._merge_exception_content(error_text, count))

    def _process_exception_cooldown_batch(self, buffered_items):
        if not buffered_items:
            return

        dispatch_items = []
        now = time.time()
        with self._exception_cooldown_lock:
            self._maybe_reset_exception_cooldown_day_locked()
            for (context, error_text), count in buffered_items:
                key = (context, error_text)
                state = self._exception_cooldown_state.get(key)
                if not state or now - state["last_seen_at"] >= self._cooldown_reset_window_seconds:
                    state = self._build_new_cooldown_state(now)
                    self._exception_cooldown_state[key] = state

                state["last_seen_at"] = now
                state["pending_count"] += count

                if now >= state["next_allowed_at"]:
                    dispatch_count = int(state["pending_count"])
                    state["pending_count"] = 0
                    dispatch_items.append((context, error_text, dispatch_count))
                    state["k"] += 1
                    state["next_allowed_at"] = now + self._compute_log_cooldown_delay(state["k"])

            self._prune_exception_cooldown_state_locked(now)
            self._reschedule_exception_cooldown_timer_locked(now)

        for context, error_text, count in dispatch_items:
            self._dispatch_exception(context, self._merge_exception_content(error_text, count))

    def _dispatch_exception(self, context, error):
        for alarm in self.alarms:
            # 异常推送保持同步发送，提升进程异常场景下的送达概率
            try:
                alarm.push_exception(context, error)
            except:
                pass

    def _buffer_exception_alarm(self, context, error_text):
        key = (context, error_text)
        with self._exception_aggregation_lock:
            self._exception_aggregation_buffer[key] = self._exception_aggregation_buffer.get(key, 0) + 1

            if self._exception_flush_timer and self._exception_flush_timer.is_alive():
                return

            self._exception_flush_timer = threading.Timer(
                self._exception_aggregation_window_seconds,
                self._flush_exception_aggregation
            )
            self._exception_flush_timer.daemon = True
            self._exception_flush_timer.start()

    def _flush_exception_aggregation(self):
        with self._exception_aggregation_lock:
            if self._exception_flush_timer and self._exception_flush_timer.is_alive():
                self._exception_flush_timer.cancel()
            buffered_items = list(self._exception_aggregation_buffer.items())
            self._exception_aggregation_buffer.clear()
            self._exception_flush_timer = None

        self._process_exception_cooldown_batch(buffered_items)
