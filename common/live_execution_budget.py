import math
import time

import config
from live_trader.data_bridge.data_warm import SchedulePlanner


_DEADLINE_ATTR = '_live_run_deadline_monotonic'
_BUDGET_ATTR = '_live_run_budget_seconds'
_SCHEDULE_BUDGET_RATIO = 0.8


def resolve_live_run_budget_seconds(runtime_config=None, context=None):
    """Return a bounded run budget, shortened for frequent schedules."""
    runtime_config = runtime_config or {}
    raw_max = runtime_config.get(
        'LIVE_RUN_MAX_EXECUTION_SECONDS',
        getattr(config, 'LIVE_RUN_MAX_EXECUTION_SECONDS', 600.0),
    )
    try:
        max_seconds = float(raw_max)
    except (TypeError, ValueError, OverflowError):
        max_seconds = 600.0
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        max_seconds = 600.0

    interval_seconds = None
    schedule_rule = runtime_config.get('schedule_rule') or getattr(context, 'schedule_rule', None)
    if schedule_rule:
        try:
            parsed = SchedulePlanner.parse_schedule_rule(schedule_rule)
            if parsed:
                interval_seconds = float(parsed.get('interval_seconds') or 0.0)
        except ValueError:
            raise
        except Exception:
            interval_seconds = None

    if not interval_seconds:
        timeframe = str(runtime_config.get('timeframe', '') or '').strip().lower()
        if timeframe in {'minutes', 'seconds'}:
            try:
                multiplier = 60.0 if timeframe == 'minutes' else 1.0
                interval_seconds = max(0.1, float(runtime_config.get('compression', 1) or 1) * multiplier)
            except (TypeError, ValueError, OverflowError):
                interval_seconds = 60.0 if timeframe == 'minutes' else 1.0

    if interval_seconds and math.isfinite(interval_seconds) and interval_seconds > 0:
        max_seconds = min(max_seconds, interval_seconds * _SCHEDULE_BUDGET_RATIO)
    return max(0.1, max_seconds)


def begin_live_run_budget(broker, runtime_config=None, context=None):
    budget_seconds = resolve_live_run_budget_seconds(runtime_config, context)
    deadline = time.monotonic() + budget_seconds
    setattr(broker, _BUDGET_ATTR, budget_seconds)
    setattr(broker, _DEADLINE_ATTR, deadline)
    return deadline


def get_live_run_deadline(broker):
    try:
        deadline = float(getattr(broker, _DEADLINE_ATTR, 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(deadline) or deadline <= 0:
        return None
    return deadline


def live_run_seconds_remaining(broker, reserve_seconds=0.0, deadline=None):
    if deadline is None:
        deadline = get_live_run_deadline(broker)
    if deadline is None:
        return math.inf
    try:
        reserve = max(0.0, float(reserve_seconds or 0.0))
    except (TypeError, ValueError, OverflowError):
        reserve = 0.0
    return max(0.0, float(deadline) - time.monotonic() - reserve)


def live_run_budget_expired(broker, reserve_seconds=0.0, deadline=None):
    return live_run_seconds_remaining(broker, reserve_seconds, deadline) <= 0.0


def live_run_finalization_reserve(broker):
    try:
        budget = float(getattr(broker, _BUDGET_ATTR, 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        budget = 0.0
    if not math.isfinite(budget) or budget <= 0:
        return 0.0
    return min(30.0, max(0.1, budget * 0.1))


def sleep_with_live_run_budget(broker, requested_seconds, reserve_seconds=0.0):
    try:
        requested = max(0.0, float(requested_seconds or 0.0))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    duration = min(requested, live_run_seconds_remaining(broker, reserve_seconds))
    if duration <= 0:
        return 0.0
    time.sleep(duration)
    return duration
