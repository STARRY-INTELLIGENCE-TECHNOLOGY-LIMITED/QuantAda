"""通用 schedule 解析与槽位计算，供实盘、告警和训练启动等待复用。"""

from __future__ import annotations

import datetime
import re

import pandas as pd


class SchedulePlanner:
    """
    通用 schedule 计算器，供实盘槽位和训练启动等待复用。

    设计边界:
    - 这里只放纯 schedule 解析、slot 推导、preview 构造
    - 不依赖 broker 实例状态，不持有运行期告警/连接状态
    """

    @staticmethod
    def parse_daily_schedule(schedule_rule: str):
        """
        解析每日调度规则，支持:
        - 1d:HH:MM
        - 1d:HH:MM:SS
        返回 (hour, minute, second, time_str)，无效则返回 None。
        """
        if not schedule_rule or not isinstance(schedule_rule, str):
            return None
        if not schedule_rule.startswith('1d:'):
            return None

        _, target_time_str = schedule_rule.split(':', 1)
        parts = target_time_str.split(':')
        if len(parts) not in (2, 3):
            raise ValueError(f"Invalid schedule time format: {target_time_str}")

        target_h = int(parts[0])
        target_m = int(parts[1])
        target_s = int(parts[2]) if len(parts) > 2 else 0

        if not (0 <= target_h <= 23 and 0 <= target_m <= 59 and 0 <= target_s <= 59):
            raise ValueError(f"Invalid schedule time value: {target_time_str}")

        return target_h, target_m, target_s, target_time_str

    @staticmethod
    def parse_schedule_rule(schedule_rule: str):
        """
        解析通用实盘调度规则，支持:
        - HH:MM[:SS]（单次触发）
        - Nd[:HH:MM[:SS]]
        - Nw[:HH:MM[:SS]]（以周一为周锚点）
        - Nm[:HH:MM[:SS]]
        - Nh[:HH:MM[:SS]]

        语义:
        - HH:MM: 当前进程生命周期内的一次性触发
        - 1d: 每日固定时刻触发
        - Nd/Nw: 按日历间隔触发，N 为正整数
        - Nm/Nh: 以 time 为每日 anchor，在当天内按固定频率重复触发
        - 省略时刻时默认 00:00:00，即从当天零点起按间隔或日线触发
        """
        if not schedule_rule or not isinstance(schedule_rule, str):
            return None

        raw = str(schedule_rule).strip().lower()
        once_match = re.fullmatch(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', raw)
        if once_match:
            target_h = int(once_match.group(1))
            target_m = int(once_match.group(2))
            target_s = int(once_match.group(3) or 0)
            if not (0 <= target_h <= 23 and 0 <= target_m <= 59 and 0 <= target_s <= 59):
                raise ValueError(f"Invalid schedule time value: {schedule_rule}")
            return {
                'raw': schedule_rule,
                'freq_n': 1,
                'freq_unit': 'once',
                'target_h': target_h,
                'target_m': target_m,
                'target_s': target_s,
                'time_str': f"{target_h:02d}:{target_m:02d}:{target_s:02d}",
                'interval_seconds': 0.0,
                'kind': 'once',
            }
        if re.match(r'^\d+s(?:$|:)', raw):
            raise ValueError(
                f"Second-level schedule '{schedule_rule}' is not supported. "
                "Use a long-lived broker connection with event-driven bars/ticks and "
                "timeframe='Seconds' instead. Supported schedule frequencies: 1d, Nm, Nh, Nd, Nw."
            )

        matched = re.fullmatch(r'(\d+)([dmhw])(?::(\d{1,2}):(\d{2})(?::(\d{2}))?)?', raw)
        if not matched:
            return None

        freq_n = int(matched.group(1))
        freq_unit = matched.group(2)
        target_h = int(matched.group(3) or 0)
        target_m = int(matched.group(4) or 0)
        target_s = int(matched.group(5) or 0)

        if freq_n <= 0:
            raise ValueError(f"Invalid schedule frequency: {schedule_rule}")
        if not (0 <= target_h <= 23 and 0 <= target_m <= 59 and 0 <= target_s <= 59):
            raise ValueError(f"Invalid schedule time value: {schedule_rule}")
        unit_seconds = {'m': 60, 'h': 3600, 'w': 7 * 86400}
        interval_seconds = freq_n * 86400 if freq_unit == 'd' else freq_n * unit_seconds[freq_unit]
        if freq_unit == 'd' and freq_n == 1:
            schedule_kind = 'daily'
        elif freq_unit == 'w' and freq_n == 1:
            schedule_kind = 'weekly'
        elif freq_unit in {'d', 'w'}:
            schedule_kind = 'calendar_interval'
        else:
            schedule_kind = 'interval'
        return {
            'raw': schedule_rule,
            'freq_n': freq_n,
            'freq_unit': freq_unit,
            'target_h': target_h,
            'target_m': target_m,
            'target_s': target_s,
            'time_str': f"{target_h:02d}:{target_m:02d}:{target_s:02d}",
            'interval_seconds': float(interval_seconds),
            'kind': schedule_kind,
        }

    @classmethod
    def wait_until_schedule(cls, schedule_rule: str, now=None, sleep_func=None, log_func=None):
        """等待到下一次调度槽位；训练启动和实盘调度共用同一套解析语义。"""
        import time

        parsed_schedule = cls.parse_schedule_rule(schedule_rule)
        if parsed_schedule is None:
            raise ValueError(
                f"Unsupported schedule format: {schedule_rule}; "
                "expected HH:MM[:SS] or Nd|Nw|Nm|Nh[:HH:MM[:SS]]."
            )

        now_ts = pd.Timestamp(now if now is not None else datetime.datetime.now())
        next_slot = cls.resolve_next_schedule_slot(now_ts, parsed_schedule)
        if next_slot is None:
            return now_ts

        wait_seconds = max(0.0, (pd.Timestamp(next_slot) - now_ts).total_seconds())
        if wait_seconds <= 0:
            return pd.Timestamp(next_slot)

        if callable(log_func):
            log_func(
                f"[Schedule] waiting {wait_seconds:.1f}s until "
                f"{pd.Timestamp(next_slot).strftime('%Y-%m-%d %H:%M:%S')} "
                f"for {schedule_rule}"
            )
        (sleep_func or time.sleep)(wait_seconds)
        return pd.Timestamp(next_slot)

    @staticmethod
    def assert_repeating_live_schedule(parsed_schedule, schedule_rule=None):
        """实盘只接受重复调度；HH:MM 一次性规则留给训练启动等待。"""
        if not parsed_schedule:
            return parsed_schedule
        if parsed_schedule.get('kind') == 'once':
            raw = schedule_rule or parsed_schedule.get('raw') or 'HH:MM'
            raise ValueError(
                f'One-time schedule {raw} is not supported for live trading; '
                'use Nd|Nw|Nm|Nh[:HH:MM[:SS]].'
            )
        return parsed_schedule

    @staticmethod
    def parse_schedule_prewarm_lead(raw_value) -> float:
        if raw_value in (None, '', 0, 0.0, '0', '0s', '0m', '0h'):
            return 0.0
        if isinstance(raw_value, (int, float)):
            return max(0.0, float(raw_value))

        raw = str(raw_value).strip().lower()
        if not raw:
            return 0.0

        matched = re.fullmatch(r'(\d+(?:\.\d+)?)([smh]?)', raw)
        if not matched:
            raise ValueError(f"Invalid prewarm lead format: {raw_value}")

        amount = float(matched.group(1))
        unit = matched.group(2) or 's'
        multiplier = {'s': 1.0, 'm': 60.0, 'h': 3600.0}[unit]
        return max(0.0, amount * multiplier)

    @staticmethod
    def _local_day(ts):
        """返回时间点所在的本地日历日（无时区、零点）。"""
        ts = pd.Timestamp(ts)
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts.normalize()

    @staticmethod
    def _wall_slot(reference, day, parsed_schedule):
        """在 reference 时区内把本地日历日和目标墙钟时刻组合为槽位。

        先按无时区墙钟相加，再本地化，避免 DST 当日或跨 DST 时用绝对秒数漂移 1 小时。
        春季跳过的时刻顺延到第一个有效时刻；秋季重复的时刻取第一次出现。
        """
        slot = pd.Timestamp(day).normalize() + pd.Timedelta(
            hours=int(parsed_schedule['target_h']),
            minutes=int(parsed_schedule['target_m']),
            seconds=int(parsed_schedule['target_s']),
        )
        reference = pd.Timestamp(reference)
        if reference.tzinfo is not None:
            slot = slot.tz_localize(reference.tz, ambiguous=True, nonexistent='shift_forward')
        return slot

    @classmethod
    def _calendar_slot_day(cls, now_ts, parsed_schedule):
        """返回 Nd/Nw 中不晚于 now 的最近槽位本地日和以天计的周期。"""
        weekly = parsed_schedule.get('freq_unit') == 'w'
        period = int(parsed_schedule.get('freq_n') or 1) * (7 if weekly else 1)
        epoch = pd.Timestamp('1970-01-05' if weekly else '1970-01-01')
        day = cls._local_day(now_ts)
        day -= pd.Timedelta(days=(day - epoch).days % period)
        if cls._wall_slot(now_ts, day, parsed_schedule) > now_ts:
            day -= pd.Timedelta(days=period)
        return day, period

    @classmethod
    def schedule_anchor_for_day(cls, now: datetime.datetime, parsed_schedule: dict):
        now_ts = pd.Timestamp(now)
        day_anchor = cls._local_day(now_ts)
        kind = parsed_schedule.get('kind')
        if kind == 'weekly' and parsed_schedule.get('freq_unit') == 'w':
            day_anchor -= pd.Timedelta(days=int(day_anchor.weekday()))
        elif kind == 'calendar_interval' and parsed_schedule.get('freq_unit') == 'w':
            day_anchor = pd.Timestamp('1970-01-05')
        elif kind == 'calendar_interval' and parsed_schedule.get('freq_unit') == 'd':
            day_anchor = pd.Timestamp('1970-01-01')
        return cls._wall_slot(now_ts, day_anchor, parsed_schedule)

    @staticmethod
    def format_schedule_slot_key(slot_dt) -> str:
        return pd.Timestamp(slot_dt).strftime('%Y-%m-%d %H:%M:%S')

    @classmethod
    def resolve_current_schedule_slot(cls, now: datetime.datetime, parsed_schedule: dict):
        now_ts = pd.Timestamp(now)
        anchor_dt = cls.schedule_anchor_for_day(now_ts, parsed_schedule)

        kind = parsed_schedule.get('kind')
        if kind == 'once':
            return anchor_dt if now_ts >= anchor_dt else None
        if kind == 'daily':
            return anchor_dt
        if kind == 'weekly':
            return anchor_dt if now_ts >= anchor_dt else None
        if kind == 'calendar_interval':
            if now_ts < anchor_dt:
                return None
            # 按本地日历日计数，再组合墙钟时刻，DST 前后都保持同一本地时刻。
            slot_day, _ = cls._calendar_slot_day(now_ts, parsed_schedule)
            return cls._wall_slot(now_ts, slot_day, parsed_schedule)
        if now_ts < anchor_dt:
            return None

        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        if interval_seconds <= 0:
            return None
        elapsed_seconds = max(0.0, (now_ts - anchor_dt).total_seconds())
        slot_index = int(elapsed_seconds // interval_seconds)
        return anchor_dt + pd.Timedelta(seconds=slot_index * interval_seconds)

    @classmethod
    def resolve_next_schedule_slot(cls, now: datetime.datetime, parsed_schedule: dict):
        now_ts = pd.Timestamp(now)
        anchor_dt = cls.schedule_anchor_for_day(now_ts, parsed_schedule)
        next_day = cls._local_day(anchor_dt) + pd.Timedelta(days=1)

        kind = parsed_schedule.get('kind')
        if kind in {'once', 'daily'}:
            if now_ts <= anchor_dt:
                return anchor_dt
            return cls._wall_slot(now_ts, next_day, parsed_schedule)
        if kind == 'weekly':
            if now_ts <= anchor_dt:
                return anchor_dt
            return cls._wall_slot(now_ts, next_day + pd.Timedelta(days=6), parsed_schedule)
        if kind == 'calendar_interval':
            if now_ts <= anchor_dt:
                return anchor_dt
            slot_day, period = cls._calendar_slot_day(now_ts, parsed_schedule)
            current_slot_dt = cls._wall_slot(now_ts, slot_day, parsed_schedule)
            if now_ts <= current_slot_dt:
                return current_slot_dt
            return cls._wall_slot(now_ts, slot_day + pd.Timedelta(days=period), parsed_schedule)

        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        if interval_seconds <= 0:
            return None
        if now_ts <= anchor_dt:
            return anchor_dt

        elapsed_seconds = max(0.0, (now_ts - anchor_dt).total_seconds())
        slot_index = int(elapsed_seconds // interval_seconds)
        current_slot_dt = anchor_dt + pd.Timedelta(seconds=slot_index * interval_seconds)
        current_delta = abs((now_ts - current_slot_dt).total_seconds())
        if current_delta <= 1e-9:
            next_slot_dt = current_slot_dt
        else:
            next_slot_dt = current_slot_dt + pd.Timedelta(seconds=interval_seconds)

        if next_slot_dt.date() != now_ts.date():
            return cls._wall_slot(now_ts, next_day, parsed_schedule)
        return next_slot_dt

    @classmethod
    def advance_schedule_slot(cls, slot_dt, parsed_schedule: dict):
        slot_ts = pd.Timestamp(slot_dt)
        slot_day = cls._local_day(slot_ts)
        kind = parsed_schedule.get('kind')
        if kind == 'once':
            return None
        if kind == 'daily':
            return cls._wall_slot(slot_ts, slot_day + pd.Timedelta(days=1), parsed_schedule)
        if kind == 'weekly':
            return cls._wall_slot(slot_ts, slot_day + pd.Timedelta(days=7), parsed_schedule)
        if kind == 'calendar_interval':
            _, period = cls._calendar_slot_day(slot_ts, parsed_schedule)
            return cls._wall_slot(slot_ts, slot_day + pd.Timedelta(days=period), parsed_schedule)

        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        if interval_seconds <= 0:
            return None
        next_slot_dt = slot_ts + pd.Timedelta(seconds=interval_seconds)
        if next_slot_dt.date() != slot_ts.date():
            return cls._wall_slot(slot_ts, slot_day + pd.Timedelta(days=1), parsed_schedule)
        return next_slot_dt

    @classmethod
    def build_schedule_preview(cls, now: datetime.datetime, parsed_schedule: dict,
                               prewarm_lead_seconds: float = 0.0, count: int = 3):
        previews = []
        slot_dt = cls.resolve_next_schedule_slot(now, parsed_schedule)
        if slot_dt is None:
            return previews

        try:
            max_count = max(1, int(count))
        except Exception:
            max_count = 3

        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        valid_prewarm = prewarm_lead_seconds > 0 and interval_seconds > 0 and prewarm_lead_seconds < interval_seconds

        while slot_dt is not None and len(previews) < max_count:
            slot_ts = pd.Timestamp(slot_dt)
            prewarm_ts = None
            if valid_prewarm:
                prewarm_ts = slot_ts - pd.Timedelta(seconds=float(prewarm_lead_seconds))
            previews.append({
                'slot_dt': slot_ts,
                'prewarm_dt': prewarm_ts,
            })
            slot_dt = cls.advance_schedule_slot(slot_ts, parsed_schedule)
        return previews

    @classmethod
    def print_schedule_preview(cls, now: datetime.datetime, parsed_schedule: dict,
                               prewarm_lead_seconds: float = 0.0, tz_info: str = '',
                               count: int = 3, prefix: str = '>>>'):
        previews = cls.build_schedule_preview(
            now=now,
            parsed_schedule=parsed_schedule,
            prewarm_lead_seconds=prewarm_lead_seconds,
            count=count,
        )
        if not previews:
            return

        tz_suffix = f" (Zone: {tz_info})" if tz_info else ''
        print(f"{prefix} Next schedule slots{tz_suffix}:")
        for idx, item in enumerate(previews, start=1):
            slot_text = pd.Timestamp(item['slot_dt']).strftime('%Y-%m-%d %H:%M:%S')
            prewarm_dt = item.get('prewarm_dt')
            if prewarm_dt is not None:
                prewarm_text = pd.Timestamp(prewarm_dt).strftime('%Y-%m-%d %H:%M:%S')
                print(f"{prefix}   [{idx}] run={slot_text}, prewarm={prewarm_text}")
            else:
                print(f"{prefix}   [{idx}] run={slot_text}")

    @classmethod
    def should_trigger_schedule(cls, now: datetime.datetime, parsed_schedule: dict,
                                last_schedule_run_key: str, tolerance_window: float = 5.0):
        now_ts = pd.Timestamp(now)
        current_slot_dt = cls.resolve_current_schedule_slot(now_ts, parsed_schedule)
        if current_slot_dt is None:
            next_slot_dt = cls.resolve_next_schedule_slot(now_ts, parsed_schedule)
            delta = -((next_slot_dt - now_ts).total_seconds()) if next_slot_dt is not None else -1.0
            return False, delta, None

        slot_key = cls.format_schedule_slot_key(current_slot_dt)
        delta = (now_ts - current_slot_dt).total_seconds()
        if last_schedule_run_key == slot_key:
            return False, delta, slot_key
        if delta < 0 or delta > tolerance_window:
            return False, delta, slot_key
        return True, delta, slot_key

    @staticmethod
    def should_trigger_schedule_prewarm(now: datetime.datetime, target_h: int, target_m: int, target_s: int,
                                        lead_seconds: float, last_prewarm_run_date: str,
                                        last_schedule_run_date: str):
        target_dt = now.replace(hour=target_h, minute=target_m, second=target_s, microsecond=0)
        seconds_to_schedule = (target_dt - now).total_seconds()
        current_date_str = now.strftime('%Y-%m-%d')

        if lead_seconds <= 0:
            return False, seconds_to_schedule, current_date_str
        if last_prewarm_run_date == current_date_str:
            return False, seconds_to_schedule, current_date_str
        if last_schedule_run_date == current_date_str:
            return False, seconds_to_schedule, current_date_str
        if seconds_to_schedule < 0 or seconds_to_schedule > lead_seconds:
            return False, seconds_to_schedule, current_date_str
        return True, seconds_to_schedule, current_date_str

    @classmethod
    def should_trigger_schedule_prewarm_for_rule(cls, now: datetime.datetime, parsed_schedule: dict,
                                                 lead_seconds: float, last_prewarm_run_key: str,
                                                 last_schedule_run_key: str):
        next_slot_dt = cls.resolve_next_schedule_slot(now, parsed_schedule)
        if next_slot_dt is None:
            return False, -1.0, None

        now_ts = pd.Timestamp(now)
        slot_key = cls.format_schedule_slot_key(next_slot_dt)
        seconds_to_schedule = (next_slot_dt - now_ts).total_seconds()

        if lead_seconds <= 0:
            return False, seconds_to_schedule, slot_key
        if last_prewarm_run_key == slot_key:
            return False, seconds_to_schedule, slot_key
        if last_schedule_run_key == slot_key:
            return False, seconds_to_schedule, slot_key
        if seconds_to_schedule < 0 or seconds_to_schedule > lead_seconds:
            return False, seconds_to_schedule, slot_key
        return True, seconds_to_schedule, slot_key

    @classmethod
    def build_schedule_prewarm_time_rule(cls, schedule_rule: str, lead_seconds: float):
        parsed_schedule = cls.parse_schedule_rule(schedule_rule)
        if not parsed_schedule or lead_seconds <= 0:
            return None
        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        if interval_seconds <= 0 or lead_seconds >= interval_seconds:
            return None

        target_h = parsed_schedule['target_h']
        target_m = parsed_schedule['target_m']
        target_s = parsed_schedule['target_s']
        anchor = datetime.datetime(2000, 1, 2, target_h, target_m, target_s)
        prewarm_dt = anchor - datetime.timedelta(seconds=float(lead_seconds))
        return prewarm_dt.strftime('%H:%M:%S')


