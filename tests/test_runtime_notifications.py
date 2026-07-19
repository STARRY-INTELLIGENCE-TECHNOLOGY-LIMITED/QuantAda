import common.runtime_notifications as notifications


def test_runtime_notifications_forward_text_and_plan(monkeypatch):
    calls = []

    class DummyAlarmManager:
        def push_text(self, content, level='INFO'):
            calls.append(('text', content, level))

        def push_plan(self, content, level='INFO'):
            calls.append(('plan', content, level))

    monkeypatch.setattr(notifications, "_get_alarm_manager", lambda: DummyAlarmManager())

    assert notifications.push_text("order warning", level="WARNING") is True
    assert notifications.push_plan("rebalance plan") is True

    assert calls == [
        ('text', "order warning", "WARNING"),
        ('plan', "rebalance plan", "INFO"),
    ]


def test_runtime_notifications_swallow_alarm_failures(monkeypatch):
    class BrokenAlarmManager:
        def push_text(self, content, level='INFO'):
            raise RuntimeError("webhook unavailable")

        def push_plan(self, content, level='INFO'):
            raise RuntimeError("webhook unavailable")

    monkeypatch.setattr(notifications, "_get_alarm_manager", lambda: BrokenAlarmManager())

    assert notifications.push_text("warning", level="ERROR") is False
    assert notifications.push_plan("plan") is False


def test_runtime_notifications_deferred_plan_keeps_last_only(monkeypatch):
    calls = []

    monkeypatch.setattr(
        notifications,
        "push_plan",
        lambda content, level='INFO': calls.append((content, level)) or True,
    )

    notifications.clear_deferred_plan()
    notifications.defer_plan("old plan", level="INFO")
    notifications.defer_plan("latest plan", level="WARNING")

    assert notifications.flush_deferred_plan() is True
    assert calls == [("latest plan", "WARNING")]
    assert notifications.flush_deferred_plan() is False


def test_runtime_notifications_deferred_plan_keeps_last_per_key(monkeypatch):
    calls = []

    monkeypatch.setattr(
        notifications,
        "push_plan",
        lambda content, level='INFO': calls.append((content, level)) or True,
    )

    notifications.clear_deferred_plan()
    notifications.defer_plan("old ranking", key="rankings")
    notifications.defer_plan("old plan", key="plan")
    notifications.defer_plan("latest ranking", level="WARNING", key="rankings")
    notifications.defer_plan("latest plan", level="INFO", key="plan")

    assert notifications.flush_deferred_plan() is True
    assert calls == [
        ("latest ranking", "WARNING"),
        ("latest plan", "INFO"),
    ]
