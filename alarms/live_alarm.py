import datetime

import pandas as pd


class LiveAlarmDeduper:
    """
    实盘告警和日志去重辅助类。

    这里只负责去重键和按调度范围构造键，消息格式化与发送由调用方负责。
    """

    def __init__(self, max_keys=5000):
        self.max_keys = max(1, int(max_keys or 5000))
        self._keys = set()

    def seen(self, key) -> bool:
        key = str(key or '').strip()
        if not key:
            return False
        if key in self._keys:
            return True
        self._keys.add(key)
        if len(self._keys) > self.max_keys:
            self._keys.clear()
            self._keys.add(key)
        return False

    def forget(self, key):
        self._keys.discard(str(key or '').strip())

    @staticmethod
    def schedule_key(event_name, schedule_rule=None, now=None):
        event = str(event_name or 'event').strip() or 'event'
        schedule_rule = str(schedule_rule or '').strip()
        now_value = now if now is not None else datetime.datetime.now()
        try:
            now_ts = pd.Timestamp(now_value)
        except Exception:
            now_ts = pd.Timestamp(datetime.datetime.now())
        if pd.isna(now_ts):
            now_ts = pd.Timestamp(datetime.datetime.now())

        if schedule_rule:
            from live_trader.data_bridge.data_warm import SchedulePlanner

            try:
                parsed_schedule = SchedulePlanner.parse_schedule_rule(schedule_rule)
            except Exception:
                parsed_schedule = None
            if parsed_schedule is not None:
                try:
                    slot_dt = SchedulePlanner.resolve_current_schedule_slot(now_ts, parsed_schedule)
                    if slot_dt is None:
                        slot_dt = SchedulePlanner.resolve_next_schedule_slot(now_ts, parsed_schedule)
                    if slot_dt is not None:
                        slot_key = SchedulePlanner.format_schedule_slot_key(slot_dt)
                        return f"{event}:{schedule_rule}:{slot_key}"
                except Exception:
                    pass

        return f"{event}:{schedule_rule or 'no_schedule'}:{now_ts.strftime('%Y-%m-%d')}"
