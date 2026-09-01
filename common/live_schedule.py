"""实盘 schedule 的通用轮询与槽位派发能力。

该模块不依赖具体券商连接，只负责 schedule 槽位计算、prewarm 去重、
单工作线程派发和长连接保活。事件驱动的券商可以调用 ``poll_once``，
没有原生 schedule 回调的券商可以调用 ``run_forever``。
"""

from __future__ import annotations

import datetime
import inspect
import threading
from typing import Callable, Optional

import pandas as pd

from common.live_runtime import runtime_print
from live_trader.data_bridge.data_warm import SchedulePlanner


class LiveScheduleRunner:
    """跨券商复用的实盘 schedule 运行器。"""

    def __init__(
        self,
        schedule_rule: str = None,
        parsed_schedule: dict = None,
        on_slot: Callable = None,
        on_prewarm: Callable = None,
        on_slot_error: Callable = None,
        on_prewarm_error: Callable = None,
        slot_filter: Callable = None,
        prewarm_lead_seconds: float = 0.0,
        clock: Callable = None,
        runtime_log: Callable = None,
        poll_interval_seconds: float = 1.0,
        idle_interval_seconds: float = 5.0,
        min_sleep_seconds: float = 0.05,
    ):
        self.schedule_rule = str(schedule_rule or '').strip()
        self.parsed_schedule = parsed_schedule
        if self.parsed_schedule is None and self.schedule_rule:
            self.parsed_schedule = SchedulePlanner.parse_schedule_rule(self.schedule_rule)
            if self.parsed_schedule is None:
                raise ValueError(
                    f'Unsupported schedule format: {self.schedule_rule}; '
                    'expected 1d|Nm|Nh:HH:MM[:SS].'
                )
        if self.parsed_schedule is not None and not isinstance(self.parsed_schedule, dict):
            raise ValueError('parsed_schedule must be a schedule dictionary or None')
        if self.parsed_schedule is not None and not self.schedule_rule:
            self.schedule_rule = str(self.parsed_schedule.get('raw', '') or '')

        self.on_slot = on_slot
        self.on_prewarm = on_prewarm
        self.on_slot_error = on_slot_error
        self.on_prewarm_error = on_prewarm_error
        self.slot_filter = slot_filter
        self.prewarm_lead_seconds = max(0.0, float(prewarm_lead_seconds or 0.0))
        self.clock = clock or datetime.datetime.now
        self.runtime_log = runtime_log or runtime_print
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds or 1.0))
        self.idle_interval_seconds = max(0.05, float(idle_interval_seconds or 5.0))
        self.min_sleep_seconds = max(0.01, float(min_sleep_seconds or 0.05))

        self.last_schedule_run_key = None
        self.last_prewarm_run_key = None
        self._scheduled_thread = None
        self._scheduled_lock = threading.Lock()
        self._stop_event = threading.Event()

        interval_seconds = 0.0
        if isinstance(self.parsed_schedule, dict):
            interval_seconds = float(self.parsed_schedule.get('interval_seconds') or 0.0)
        if interval_seconds > 0 and self.prewarm_lead_seconds >= interval_seconds:
            self.prewarm_lead_seconds = 0.0

    @property
    def scheduled_thread(self):
        """返回当前槽位工作线程；仅用于诊断和测试。"""
        with self._scheduled_lock:
            return self._scheduled_thread

    def stop(self):
        """请求 ``run_forever`` 在当前等待结束后返回。"""
        self._stop_event.set()

    def _log(self, message):
        try:
            self.runtime_log(message)
        except Exception:
            pass

    @staticmethod
    def _coerce_now(value):
        if value is None:
            return pd.Timestamp(datetime.datetime.now())
        return pd.Timestamp(value)

    def _handle_prewarm_error(self, error, slot_key):
        if callable(self.on_prewarm_error):
            try:
                self.on_prewarm_error(error, slot_key)
                return
            except Exception as callback_error:
                self._log(
                    f'[LiveSchedule] prewarm error callback failed for {slot_key}: {callback_error}'
                )
        self._log(f'[LiveSchedule] prewarm failed for slot {slot_key}: {error}')

    def _handle_slot_error(self, error, slot_key):
        if callable(self.on_slot_error):
            try:
                self.on_slot_error(error, slot_key)
                return
            except Exception as callback_error:
                self._log(
                    f'[LiveSchedule] slot error callback failed for {slot_key}: {callback_error}'
                )
        self._log(f'[LiveSchedule] slot {slot_key} failed: {error}')

    def _allow_slot(self, now, slot_key, phase):
        """调用可选的券商时段过滤器；旧式两参数回调仍可兼容。"""
        if not callable(self.slot_filter):
            return True
        try:
            # 先用签名绑定判断参数个数，避免回调内部抛出 TypeError 时被再次执行。
            # inspect.signature 对常规函数、绑定方法和大多数测试替身都可用；
            # 无法取得签名时只尝试新式三参数调用，宁可安全拒绝也不重复副作用。
            try:
                signature = inspect.signature(self.slot_filter)
            except (TypeError, ValueError):
                signature = None
            if signature is not None:
                try:
                    signature.bind(now, slot_key, phase)
                except TypeError:
                    signature.bind(now, slot_key)
                    return bool(self.slot_filter(now, slot_key))
            return bool(self.slot_filter(now, slot_key, phase))
        except Exception as error:
            self._log(f'[LiveSchedule] slot filter failed for {slot_key}: {error}')
            return False

    def _run_slot_worker(self, now_snapshot, slot_key):
        succeeded = False
        try:
            if callable(self.on_slot):
                self.on_slot(now_snapshot, slot_key)
            succeeded = True
        except Exception as error:
            self._handle_slot_error(error, slot_key)
        finally:
            with self._scheduled_lock:
                if not succeeded and self.last_schedule_run_key == slot_key:
                    # 异步 worker 的回调失败后释放 key，允许后续轮询在同一 slot 重试。
                    self.last_schedule_run_key = None
                if self._scheduled_thread is threading.current_thread():
                    self._scheduled_thread = None

    def _dispatch_slot(self, now_snapshot, slot_key):
        with self._scheduled_lock:
            previous = self._scheduled_thread
            if previous is not None and previous.is_alive():
                self._log(
                    '[LiveSchedule] previous slot is still running; '
                    f'skipping overlapping slot {slot_key or "unknown"}.'
                )
                return False

            worker = threading.Thread(
                target=self._run_slot_worker,
                args=(now_snapshot, slot_key),
                name=f'quantada-live-run-{slot_key or "unknown"}',
                daemon=True,
            )
            self._scheduled_thread = worker
        worker.start()
        return True

    def poll_once(self, now=None) -> dict:
        """处理一次 prewarm/slot 检查，供 SDK 事件回调线程调用。"""
        current_now = self._coerce_now(now if now is not None else self.clock())
        result = {
            'now': current_now,
            'prewarm_triggered': False,
            'slot_triggered': False,
            'slot_key': None,
            'prewarm_slot_key': None,
            'overlap': False,
        }
        if not isinstance(self.parsed_schedule, dict):
            return result

        if self.prewarm_lead_seconds > 0 and callable(self.on_prewarm):
            should_prewarm, _, prewarm_key = (
                SchedulePlanner.should_trigger_schedule_prewarm_for_rule(
                    now=current_now,
                    parsed_schedule=self.parsed_schedule,
                    lead_seconds=self.prewarm_lead_seconds,
                    last_prewarm_run_key=self.last_prewarm_run_key,
                    last_schedule_run_key=self.last_schedule_run_key,
                )
            )
            if should_prewarm and prewarm_key and self._allow_slot(
                current_now, prewarm_key, 'prewarm'
            ):
                result['prewarm_triggered'] = True
                result['prewarm_slot_key'] = prewarm_key
                try:
                    prewarm_result = self.on_prewarm(current_now, prewarm_key)
                    if isinstance(prewarm_result, dict) and prewarm_result.get('errors'):
                        raise RuntimeError(
                            'prewarm returned errors: '
                            + '; '.join(str(item) for item in prewarm_result['errors'])
                        )
                    self.last_prewarm_run_key = prewarm_key
                except Exception as error:
                    self._handle_prewarm_error(error, prewarm_key)

        should_run, delta, slot_key = SchedulePlanner.should_trigger_schedule(
            now=current_now,
            parsed_schedule=self.parsed_schedule,
            last_schedule_run_key=self.last_schedule_run_key,
            tolerance_window=5.0,
        )
        result['slot_key'] = slot_key
        result['delta'] = delta
        if should_run and slot_key and self._allow_slot(current_now, slot_key, 'slot'):
            self.last_schedule_run_key = slot_key
            result['slot_triggered'] = True
            result['overlap'] = not self._dispatch_slot(current_now, slot_key)
        return result

    def _next_wait_seconds(self, current_now):
        if not isinstance(self.parsed_schedule, dict):
            return self.idle_interval_seconds
        next_slot = SchedulePlanner.resolve_next_schedule_slot(
            current_now + datetime.timedelta(seconds=1),
            self.parsed_schedule,
        )
        if next_slot is None:
            return self.poll_interval_seconds
        seconds_to_slot = (pd.Timestamp(next_slot) - current_now).total_seconds()
        return min(
            self.poll_interval_seconds,
            max(self.min_sleep_seconds, seconds_to_slot),
        )

    def run_forever(self, stop_event: Optional[threading.Event] = None):
        """持续保活并执行 schedule；调用方可用 ``stop`` 或传入 stop_event。"""
        event = stop_event or self._stop_event
        while not event.is_set():
            current_now = self._coerce_now(self.clock())
            self.poll_once(current_now)
            wait_seconds = self._next_wait_seconds(current_now)
            event.wait(wait_seconds)
