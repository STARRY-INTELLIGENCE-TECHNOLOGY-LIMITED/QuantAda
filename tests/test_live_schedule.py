import threading

import pandas as pd
import pytest

from common.live_schedule import LiveScheduleRunner
from common.schedule_planner import SchedulePlanner


def test_live_schedule_runner_rejects_invalid_schedule():
    with pytest.raises(ValueError, match='Unsupported schedule format'):
        LiveScheduleRunner(schedule_rule='bad schedule')


@pytest.mark.parametrize(
    "schedule_rule,freq_n,freq_unit,interval_seconds",
    [
        ("5m", 5, "m", 300.0),
        ("1h", 1, "h", 3600.0),
        ("1d", 1, "d", 86400.0),
        ("1w", 1, "w", 7 * 86400.0),
        ("2d", 2, "d", 2 * 86400.0),
        ("2w", 2, "w", 14 * 86400.0),
    ],
)
def test_schedule_rule_accepts_frequency_without_clock(
    schedule_rule, freq_n, freq_unit, interval_seconds,
):
    parsed = SchedulePlanner.parse_schedule_rule(schedule_rule)
    assert parsed is not None
    assert parsed["freq_n"] == freq_n
    assert parsed["freq_unit"] == freq_unit
    assert parsed["target_h"] == parsed["target_m"] == parsed["target_s"] == 0
    assert parsed["time_str"] == "00:00:00"
    assert parsed["interval_seconds"] == pytest.approx(interval_seconds)
    expected_kind = {
        "d": "daily" if freq_n == 1 else "calendar_interval",
        "w": "weekly" if freq_n == 1 else "calendar_interval",
    }.get(freq_unit, "interval")
    assert parsed["kind"] == expected_kind


def test_schedule_rule_accepts_one_time_local_clock():
    parsed = SchedulePlanner.parse_schedule_rule("02:00")

    assert parsed["kind"] == "once"
    assert parsed["time_str"] == "02:00:00"
    assert parsed["interval_seconds"] == 0.0


def test_weekly_schedule_uses_monday_anchor():
    parsed = SchedulePlanner.parse_schedule_rule("1w:02:00:00")

    before_anchor = SchedulePlanner.resolve_next_schedule_slot(
        pd.Timestamp("2026-08-31 01:00:00"), parsed,
    )
    after_anchor = SchedulePlanner.resolve_next_schedule_slot(
        pd.Timestamp("2026-09-01 03:00:00"), parsed,
    )

    assert before_anchor == pd.Timestamp("2026-08-31 02:00:00")
    assert after_anchor == pd.Timestamp("2026-09-07 02:00:00")


def test_multi_day_and_multi_week_schedules_keep_stable_calendar_anchors():
    day_rule = SchedulePlanner.parse_schedule_rule("2d:02:00:00")
    week_rule = SchedulePlanner.parse_schedule_rule("2w:02:00:00")

    assert SchedulePlanner.resolve_next_schedule_slot(
        pd.Timestamp("2026-09-01 03:00:00"), day_rule,
    ) == pd.Timestamp("2026-09-02 02:00:00")
    assert SchedulePlanner.resolve_next_schedule_slot(
        pd.Timestamp("2026-09-01 03:00:00"), week_rule,
    ) == pd.Timestamp("2026-09-14 02:00:00")


def test_wait_until_schedule_sleeps_until_next_slot():
    waits = []
    next_slot = SchedulePlanner.wait_until_schedule(
        "02:00",
        now=pd.Timestamp("2026-08-31 01:00:00"),
        sleep_func=waits.append,
    )

    assert next_slot == pd.Timestamp("2026-08-31 02:00:00")
    assert waits == [3600.0]


def test_wait_until_missed_one_time_schedule_waits_until_next_day():
    waits = []
    now = pd.Timestamp("2026-08-31 03:00:00")
    next_slot = SchedulePlanner.wait_until_schedule(
        "02:00",
        now=now,
        sleep_func=waits.append,
    )

    assert next_slot == pd.Timestamp("2026-09-01 02:00:00")
    assert waits == [23 * 3600.0]


def test_live_schedule_runner_rejects_one_time_clock():
    with pytest.raises(ValueError, match="One-time schedule"):
        LiveScheduleRunner(schedule_rule="02:00")


def test_live_schedule_runner_deduplicates_slots_and_dispatches_worker():
    calls = []
    finished = threading.Event()

    def on_slot(now, slot_key):
        calls.append((now, slot_key))
        finished.set()

    runner = LiveScheduleRunner(
        schedule_rule='1h:10:00:00',
        on_slot=on_slot,
        runtime_log=lambda _message: None,
    )
    now = pd.Timestamp('2026-08-31 10:00:02')

    first = runner.poll_once(now)
    assert first['slot_triggered'] is True
    assert finished.wait(1.0)

    duplicate = runner.poll_once(now + pd.Timedelta(seconds=1))
    assert duplicate['slot_triggered'] is False
    assert len(calls) == 1
    assert runner.last_schedule_run_key == '2026-08-31 10:00:00'


def test_live_schedule_runner_skips_overlapping_slot():
    started = threading.Event()
    release = threading.Event()
    calls = []
    logs = []

    def on_slot(_now, slot_key):
        calls.append(slot_key)
        started.set()
        release.wait(1.0)

    runner = LiveScheduleRunner(
        schedule_rule='1h:10:00:00',
        on_slot=on_slot,
        runtime_log=logs.append,
    )
    assert runner.poll_once(pd.Timestamp('2026-08-31 10:00:01'))['slot_triggered'] is True
    assert started.wait(1.0)

    overlap = runner.poll_once(pd.Timestamp('2026-08-31 11:00:01'))
    assert overlap['slot_triggered'] is True
    assert overlap['overlap'] is True
    assert calls == ['2026-08-31 10:00:00']
    assert any('overlapping slot 2026-08-31 11:00:00' in message for message in logs)
    release.set()
    worker = runner.scheduled_thread
    if worker is not None:
        worker.join(1.0)


def test_live_schedule_runner_triggers_prewarm_once_before_slot():
    prewarm_calls = []
    slot_calls = []
    slot_finished = threading.Event()

    def on_prewarm(now, slot_key):
        prewarm_calls.append((now, slot_key))

    def on_slot(_now, slot_key):
        slot_calls.append(slot_key)
        slot_finished.set()

    runner = LiveScheduleRunner(
        schedule_rule='1h:10:00:00',
        on_slot=on_slot,
        on_prewarm=on_prewarm,
        prewarm_lead_seconds=60,
        runtime_log=lambda _message: None,
    )
    prewarm = runner.poll_once(pd.Timestamp('2026-08-31 09:59:30'))
    assert prewarm['prewarm_triggered'] is True
    assert prewarm_calls == [(pd.Timestamp('2026-08-31 09:59:30'), '2026-08-31 10:00:00')]

    duplicate = runner.poll_once(pd.Timestamp('2026-08-31 09:59:40'))
    assert duplicate['prewarm_triggered'] is False
    assert len(prewarm_calls) == 1

    slot = runner.poll_once(pd.Timestamp('2026-08-31 10:00:02'))
    assert slot['slot_triggered'] is True
    assert slot_finished.wait(1.0)
    assert slot_calls == ['2026-08-31 10:00:00']


def test_live_schedule_runner_stop_interrupts_idle_run_forever():
    runner = LiveScheduleRunner(
        schedule_rule=None,
        clock=lambda: pd.Timestamp('2026-08-31 10:00:00'),
        idle_interval_seconds=1.0,
    )
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    runner.stop()
    thread.join(1.0)
    assert not thread.is_alive()


def test_live_schedule_runner_retries_failed_prewarm_same_slot():
    attempts = []

    def on_prewarm(_now, _slot_key):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('prewarm unavailable')

    runner = LiveScheduleRunner(
        schedule_rule='1h:10:00:00',
        on_prewarm=on_prewarm,
        prewarm_lead_seconds=60,
        runtime_log=lambda _message: None,
    )
    first = runner.poll_once(pd.Timestamp('2026-08-31 09:59:30'))
    second = runner.poll_once(pd.Timestamp('2026-08-31 09:59:40'))

    assert first['prewarm_triggered'] is True
    assert second['prewarm_triggered'] is True
    assert len(attempts) == 2
    assert runner.last_prewarm_run_key == '2026-08-31 10:00:00'


def test_live_schedule_runner_retries_prewarm_when_summary_has_errors():
    attempts = []

    def on_prewarm(_now, _slot_key):
        attempts.append(1)
        return {'errors': ['quote unavailable']} if len(attempts) == 1 else {'errors': []}

    runner = LiveScheduleRunner(
        schedule_rule='1h:10:00:00',
        on_prewarm=on_prewarm,
        prewarm_lead_seconds=60,
        runtime_log=lambda _message: None,
    )

    first = runner.poll_once(pd.Timestamp('2026-08-31 09:59:30'))
    second = runner.poll_once(pd.Timestamp('2026-08-31 09:59:40'))

    assert first['prewarm_triggered'] is True
    assert second['prewarm_triggered'] is True
    assert attempts == [1, 1]
    assert runner.last_prewarm_run_key == '2026-08-31 10:00:00'


def test_live_schedule_runner_retries_failed_slot_after_worker_finishes():
    attempts = []
    finished = threading.Event()

    def on_slot(_now, _slot_key):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('slot unavailable')
        finished.set()

    runner = LiveScheduleRunner(
        schedule_rule='1h:10:00:00',
        on_slot=on_slot,
        runtime_log=lambda _message: None,
    )
    assert runner.poll_once(pd.Timestamp('2026-08-31 10:00:01'))['slot_triggered'] is True
    deadline = pd.Timestamp('2026-08-31 10:00:01') + pd.Timedelta(seconds=1)
    while runner.scheduled_thread is not None and runner.scheduled_thread.is_alive():
        if pd.Timestamp.now() > deadline:
            break
        threading.Event().wait(0.01)

    retry = runner.poll_once(pd.Timestamp('2026-08-31 10:00:02'))
    assert retry['slot_triggered'] is True
    assert finished.wait(1.0)
    assert len(attempts) == 2


def test_live_schedule_slot_filter_type_error_is_not_retried():
    calls = []

    def slot_filter(_now, _slot, _phase):
        calls.append(1)
        raise TypeError('filter body failed')

    runner = LiveScheduleRunner(
        schedule_rule='1h:10:00:00',
        on_slot=lambda *_args: None,
        slot_filter=slot_filter,
        runtime_log=lambda _message: None,
    )

    result = runner.poll_once(pd.Timestamp('2026-08-31 10:00:01'))

    assert result['slot_triggered'] is False
    assert calls == [1]
