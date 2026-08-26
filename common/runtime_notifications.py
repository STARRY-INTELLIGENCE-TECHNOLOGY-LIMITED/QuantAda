"""运行时通知边界。

核心交易模块应通过本模块发出通知意图，不要直接导入 IM 告警实现。
"""

_DEFERRED_PLAN_KEY = "plan"
_deferred_plans = {}


def _get_alarm_manager():
    from alarms.manager import AlarmManager

    return AlarmManager()


def push_text(content, level='INFO') -> bool:
    try:
        _get_alarm_manager().push_text(content, level=level)
        return True
    except Exception:
        return False


def push_plan(content, level='INFO') -> bool:
    try:
        _get_alarm_manager().push_plan(content, level=level)
        return True
    except Exception:
        return False


def defer_plan(content, level='INFO', key=_DEFERRED_PLAN_KEY) -> bool:
    plan_key = str(key or _DEFERRED_PLAN_KEY)
    _deferred_plans[plan_key] = (content, level)
    return True


def flush_deferred_plan() -> bool:
    if not _deferred_plans:
        return False

    pending_items = list(_deferred_plans.values())
    _deferred_plans.clear()

    sent = False
    for content, level in pending_items:
        sent = push_plan(content, level=level) or sent
    return sent


def clear_deferred_plan():
    _deferred_plans.clear()
