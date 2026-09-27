"""汇总耗时使用试验墙钟，而不是续跑验证的进程时间。"""

from datetime import datetime, timedelta, timezone

from optimizer.reporting import training_elapsed_hours


class _Trial:
    def __init__(self, start, end):
        self.datetime_start = start
        self.datetime_complete = end


def test_parallel_trials_count_once_and_gaps_are_excluded():
    start = datetime(2026, 9, 26, 11, 28, 52)
    trials = [
        _Trial(start, start + timedelta(hours=2)),
        _Trial(start + timedelta(minutes=30), start + timedelta(hours=3)),
        _Trial(start + timedelta(hours=8), start + timedelta(hours=9)),
        _Trial(start + timedelta(hours=1), None),
    ]
    assert training_elapsed_hours(trials) == 4.0


def test_missing_timestamps_do_not_invent_duration():
    assert training_elapsed_hours([]) is None
    assert training_elapsed_hours([_Trial(None, datetime(2026, 9, 26))]) is None


def test_aware_timestamps_are_comparable():
    start = datetime(2026, 9, 26, 3, 28, tzinfo=timezone.utc)
    end = datetime(2026, 9, 26, 11, 16, tzinfo=timezone.utc)
    assert training_elapsed_hours([_Trial(start, end)]) == 7.8
