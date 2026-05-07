from alarms.live_alarm import LiveAlarmDeduper


def test_live_alarm_deduper_seen_and_forget():
    deduper = LiveAlarmDeduper()

    assert deduper.seen("risk:x") is False
    assert deduper.seen("risk:x") is True

    deduper.forget("risk:x")
    assert deduper.seen("risk:x") is False


def test_live_alarm_schedule_key_uses_daily_slot():
    key = LiveAlarmDeduper.schedule_key(
        "missing_order_account",
        schedule_rule="1d:15:45:00",
        now="2026-05-05 16:00:00",
    )

    assert key == "missing_order_account:1d:15:45:00:2026-05-05 15:45:00"


def test_live_alarm_schedule_key_uses_interval_slot():
    key = LiveAlarmDeduper.schedule_key(
        "missing_order_account",
        schedule_rule="5m:09:30:00",
        now="2026-05-05 09:35:04",
    )

    assert key == "missing_order_account:5m:09:30:00:2026-05-05 09:35:00"
