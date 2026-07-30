import datetime
import re

import pandas as pd

from common import runtime_notifications


DAILY_SCHEDULE_HEALTH_LEAD_SECONDS = 30 * 60


class SchedulePlanner:
    """
    通用实盘 schedule 计算器。

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
        - 1d:HH:MM[:SS]
        - Nm:HH:MM[:SS]
        - Nh:HH:MM[:SS]

        语义:
        - 1d: 每日固定时刻触发
        - Nm/Nh: 以 time 为每日 anchor，在当天内按固定频率重复触发
        """
        if not schedule_rule or not isinstance(schedule_rule, str):
            return None

        raw = str(schedule_rule).strip().lower()
        if re.match(r'^\d+s(?:$|:)', raw):
            raise ValueError(
                f"Second-level schedule '{schedule_rule}' is not supported. "
                "Use a long-lived broker connection with event-driven bars/ticks and "
                "timeframe='Seconds' instead. Supported schedule frequencies: 1d, Nm, Nh."
            )

        matched = re.fullmatch(r'(\d+)([dmh]):(\d{1,2}):(\d{2})(?::(\d{2}))?', raw)
        if not matched:
            return None

        freq_n = int(matched.group(1))
        freq_unit = matched.group(2)
        target_h = int(matched.group(3))
        target_m = int(matched.group(4))
        target_s = int(matched.group(5) or 0)

        if freq_n <= 0:
            raise ValueError(f"Invalid schedule frequency: {schedule_rule}")
        if not (0 <= target_h <= 23 and 0 <= target_m <= 59 and 0 <= target_s <= 59):
            raise ValueError(f"Invalid schedule time value: {schedule_rule}")
        if freq_unit == 'd' and freq_n != 1:
            raise ValueError(f"Unsupported daily schedule frequency: {schedule_rule}")

        unit_seconds = {'m': 60, 'h': 3600}
        interval_seconds = 86400 if freq_unit == 'd' else freq_n * unit_seconds[freq_unit]
        return {
            'raw': schedule_rule,
            'freq_n': freq_n,
            'freq_unit': freq_unit,
            'target_h': target_h,
            'target_m': target_m,
            'target_s': target_s,
            'time_str': f"{target_h:02d}:{target_m:02d}:{target_s:02d}",
            'interval_seconds': float(interval_seconds),
            'kind': 'daily' if freq_unit == 'd' else 'interval',
        }

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
    def schedule_anchor_for_day(now: datetime.datetime, parsed_schedule: dict):
        now_ts = pd.Timestamp(now)
        return now_ts.normalize() + pd.Timedelta(
            hours=int(parsed_schedule['target_h']),
            minutes=int(parsed_schedule['target_m']),
            seconds=int(parsed_schedule['target_s']),
        )

    @staticmethod
    def format_schedule_slot_key(slot_dt) -> str:
        return pd.Timestamp(slot_dt).strftime('%Y-%m-%d %H:%M:%S')

    @classmethod
    def resolve_current_schedule_slot(cls, now: datetime.datetime, parsed_schedule: dict):
        now_ts = pd.Timestamp(now)
        anchor_dt = cls.schedule_anchor_for_day(now_ts, parsed_schedule)

        if parsed_schedule.get('kind') == 'daily':
            return anchor_dt
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

        if parsed_schedule.get('kind') == 'daily':
            if now_ts <= anchor_dt:
                return anchor_dt
            return anchor_dt + pd.Timedelta(days=1)

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
            return anchor_dt + pd.Timedelta(days=1)
        return next_slot_dt

    @classmethod
    def advance_schedule_slot(cls, slot_dt, parsed_schedule: dict):
        slot_ts = pd.Timestamp(slot_dt)
        if parsed_schedule.get('kind') == 'daily':
            return cls.schedule_anchor_for_day(slot_ts + pd.Timedelta(days=1), parsed_schedule)

        interval_seconds = float(parsed_schedule.get('interval_seconds') or 0.0)
        if interval_seconds <= 0:
            return None
        next_slot_dt = slot_ts + pd.Timedelta(seconds=interval_seconds)
        if next_slot_dt.date() != slot_ts.date():
            return cls.schedule_anchor_for_day(slot_ts + pd.Timedelta(days=1), parsed_schedule)
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


class BrokerDataWarmBridge:
    """
    Broker 预热 bridge。

    设计边界:
    - 通过组合持有 broker host，而不是让 broker 继承预热逻辑
    - 这里只放依赖 broker 原子能力的预热执行、告警去重与兜底处理
    """

    def __init__(self, host):
        self._host = host
        self._prewarm_alarm_keys = set()

    @staticmethod
    def pick_prewarm_symbol(symbols=None, datas=None):
        for data in datas or []:
            name = str(getattr(data, '_name', '') or '').strip()
            if name:
                return name
        if isinstance(symbols, str):
            cleaned = symbols.strip()
            return cleaned or None
        for symbol in symbols or []:
            cleaned = str(symbol or '').strip()
            if cleaned:
                return cleaned
        return None

    @staticmethod
    def build_prewarm_window(now_input, timeframe='Days', compression=1):
        now_ts = pd.Timestamp(now_input or pd.Timestamp.now())
        tf = str(timeframe or 'Days')
        try:
            cp = max(1, int(compression or 1))
        except Exception:
            cp = 1

        if tf in {'Minutes', 'Seconds'}:
            delta = pd.Timedelta(minutes=cp * 3) if tf == 'Minutes' else pd.Timedelta(seconds=cp * 3)
            start_ts = now_ts - delta
            return (
                start_ts.strftime('%Y-%m-%d %H:%M:%S'),
                now_ts.strftime('%Y-%m-%d %H:%M:%S'),
            )

        start_ts = now_ts - pd.Timedelta(days=2)
        return (
            start_ts.strftime('%Y-%m-%d'),
            now_ts.strftime('%Y-%m-%d'),
        )

    def _resolve_context_now(self, now=None):
        context = getattr(self._host, '_context', None)
        return now or getattr(context, 'now', None) or datetime.datetime.now()

    def alarm_schedule_prewarm_issue_once(self, schedule_rule, now=None, slot_key=None, summary=None,
                                          error=None, level='ERROR') -> bool:
        try:
            ts = pd.Timestamp(self._resolve_context_now(now=now))
        except Exception:
            ts = pd.Timestamp(datetime.datetime.now())
        issue_scope = slot_key or ts.strftime('%Y-%m-%d')
        issue_type = 'exception' if error is not None else 'summary'
        alarm_key = f"{issue_scope}:{schedule_rule or 'N/A'}:{issue_type}"
        if alarm_key in self._prewarm_alarm_keys:
            return False
        self._prewarm_alarm_keys.add(alarm_key)
        if len(self._prewarm_alarm_keys) > 5000:
            self._prewarm_alarm_keys.clear()
            self._prewarm_alarm_keys.add(alarm_key)

        if error is not None:
            msg = (
                f"[Broker Warning] Schedule prewarm failed before {schedule_rule}: {error}. "
                'Normal schedule will continue.'
            )
        else:
            summary = summary or {}
            msg = (
                f"[Broker Warning] Schedule prewarm finished with errors before {schedule_rule}. "
                f"source={summary.get('source')}, "
                f"symbol={summary.get('symbol')}, "
                f"extras={summary.get('extras')}, "
                f"errors={summary.get('errors')}. "
                'Normal schedule will continue.'
            )

        print(msg)
        if not runtime_notifications.push_text(msg, level=level):
            print("[Broker Warning] failed to push prewarm alarm")
        return True

    def run_schedule_prewarm(self, schedule_rule, data_provider=None, symbols=None,
                             timeframe='Days', compression=1, now=None) -> dict:
        slot_key = None
        parsed_schedule = SchedulePlanner.parse_schedule_rule(schedule_rule)
        if parsed_schedule is not None:
            try:
                slot_dt = SchedulePlanner.resolve_next_schedule_slot(
                    self._resolve_context_now(now=now),
                    parsed_schedule,
                )
                if slot_dt is not None:
                    slot_key = SchedulePlanner.format_schedule_slot_key(slot_dt)
            except Exception:
                slot_key = None
        try:
            summary = self.prewarm_before_schedule(
                data_provider=data_provider,
                symbols=symbols,
                timeframe=timeframe,
                compression=compression,
                now=now,
            )
        except Exception as exc:
            self.alarm_schedule_prewarm_issue_once(
                schedule_rule=schedule_rule,
                now=now,
                slot_key=slot_key,
                error=exc,
                level='ERROR',
            )
            return {
                'attempted': False,
                'source': None,
                'symbol': None,
                'price': 0.0,
                'history_rows': 0,
                'extras': [],
                'errors': [f"exception:{exc}"],
            }

        if summary.get('errors'):
            self.alarm_schedule_prewarm_issue_once(
                schedule_rule=schedule_rule,
                now=now,
                slot_key=slot_key,
                summary=summary,
                level='WARNING',
            )
        return summary

    def prewarm_before_schedule(self, data_provider=None, symbols=None,
                                timeframe='Days', compression=1, now=None) -> dict:
        summary = {
            'attempted': False,
            'source': None,
            'symbol': None,
            'price': 0.0,
            'history_rows': 0,
            'extras': [],
            'errors': [],
        }

        datas = getattr(self._host, 'datas', None) or []
        first_data = next((d for d in datas if getattr(d, '_name', None)), None)
        if first_data is not None:
            summary['attempted'] = True
            summary['source'] = 'broker'
            summary['symbol'] = str(getattr(first_data, '_name', '') or '').strip()
            try:
                price = self._host.get_current_price(first_data)
                if price:
                    summary['price'] = float(price)
            except Exception as exc:
                summary['errors'].append(f"broker:{exc}")
        else:
            first_symbol = self.pick_prewarm_symbol(symbols=symbols, datas=datas)
            if first_symbol and data_provider and hasattr(data_provider, 'get_history'):
                summary['attempted'] = True
                summary['source'] = 'data_provider'
                summary['symbol'] = first_symbol
                start_date, end_date = self.build_prewarm_window(
                    now_input=now,
                    timeframe=timeframe,
                    compression=compression,
                )
                try:
                    df = data_provider.get_history(
                        first_symbol,
                        start_date,
                        end_date,
                        timeframe=timeframe,
                        compression=compression,
                    )
                    if df is not None:
                        try:
                            summary['history_rows'] = int(len(df))
                        except Exception:
                            summary['history_rows'] = 0
                except Exception as exc:
                    summary['errors'].append(f"data_provider:{exc}")

        try:
            summary['extras'] = list(self._host.prewarm_additional_connections(now=now) or [])
        except Exception as exc:
            summary['errors'].append(f"extras:{exc}")

        return summary
