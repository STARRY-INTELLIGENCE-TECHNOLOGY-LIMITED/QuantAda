import threading

import pandas as pd
import pytest

from common.live_schedule import LiveScheduleRunner


def test_live_schedule_runner_rejects_invalid_schedule():
    with pytest.raises(ValueError, match='Unsupported schedule format'):
        LiveScheduleRunner(schedule_rule='bad schedule')


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
