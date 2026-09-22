import datetime

import pandas as pd

from common import runtime_notifications
from common.schedule_planner import SchedulePlanner


DAILY_SCHEDULE_HEALTH_LEAD_SECONDS = 30 * 60


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
