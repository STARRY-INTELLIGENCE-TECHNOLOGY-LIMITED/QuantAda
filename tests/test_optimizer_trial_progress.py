"""Optuna Trial 日志应在原行显示预算和剩余时间。"""

import logging
import time
from types import SimpleNamespace

from optimizer.trial_progress import (
    TrialFinishCounter,
    TrialProgressFilter,
    SharedFinishCounter,
    format_remaining_duration,
    installed_trial_progress,
    make_trial_progress,
    remaining_label,
)


_FINISHED = "Trial {number} finished with value: 1.0 and parameters: {{}}. Best is trial 10 with value: 1.0."


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _progress(counter, started_at, target=2160, finished_before=0):
    return make_trial_progress(target, finished_before, counter, started_at=started_at)


def test_duration_uses_two_largest_units():
    assert format_remaining_duration(45) == "45s"
    assert format_remaining_duration(12 * 60 + 1) == "12m1s"
    assert format_remaining_duration(6 * 3600 + 12 * 60) == "6h12m"
    assert format_remaining_duration(2 * 86400 + 3 * 3600) == "2d3h"
    assert format_remaining_duration(60) == "1m"
    assert format_remaining_duration(3600) == "1h"
    assert format_remaining_duration(86400) == "1d"


def test_eta_uses_this_run_rate_and_skips_failures(monkeypatch):
    started = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: started + 600)
    assert remaining_label(2160, 1033, 0, started, started + 600) == "ETA unavailable"
    assert remaining_label(2160, 1033, 1, started, started + 0.2) == "ETA unavailable"
    assert remaining_label(2160, 1033, 10, started, started + 600) == "ETA 18h37m"
    assert remaining_label(2160, 1033, 1127, started, started + 600) == "finishing"


def test_child_logger_line_is_rewritten_once_and_keeps_best_trial(monkeypatch):
    started = 1_000_000.0
    monkeypatch.setattr("optimizer.trial_progress.time.time", lambda: started + 600)
    counter = TrialFinishCounter()
    capture = _Capture()
    root = logging.getLogger("optuna")
    root.addHandler(capture)
    try:
        with installed_trial_progress(_progress(counter, started, finished_before=1033)):
            logging.getLogger("optuna.study.study").info(
                "Trial 369 finished with value: -22.0 and parameters: {'min_dte': 35}. "
                "Best is trial 10 with value: 10.87481044264505."
            )
            logging.getLogger("optuna.study._optimize").info("Trial 370 pruned. ")
            logging.getLogger("optuna.study._optimize").warning(
                "Trial 371 failed with parameters: {'min_dte': 35} because of the following error: boom."
            )
            logging.getLogger("optuna.study._optimize").warning("Trial 371 failed with value None.")
            logging.getLogger("optuna.study.study").info("A new study created in memory with name: demo")
    finally:
        root.removeHandler(capture)

    assert counter.value == 2
    assert len(capture.messages) == 5
    assert capture.messages[0].startswith("Trial 369/2160 ETA ")
    assert "Best is trial 10 with value: 10.87481044264505." in capture.messages[0]
    assert capture.messages[0].endswith("10.87481044264505.")
    assert capture.messages[1].startswith("Trial 370/2160 ETA ")
    assert " pruned. " in capture.messages[1]
    assert capture.messages[2].startswith("Trial 371/2160 ")
    assert "failed with parameters" in capture.messages[2]
    assert capture.messages[3].startswith("Trial 371/2160 ")
    assert capture.messages[4] == "A new study created in memory with name: demo"
    assert not any(handler.filters for handler in root.handlers if capture not in (handler,))


def test_same_record_and_rewritten_line_are_not_counted_twice():
    counter = TrialFinishCounter()
    progress = _progress(counter, time.time() - 30, target=10)
    progress_filter = TrialProgressFilter(progress)
    record = logging.LogRecord(
        "optuna.study.study", logging.INFO, __file__, 1,
        "Trial 3 finished with value: 1.0 and parameters: {}.", (), None,
    )
    assert progress_filter.filter(record) is True
    assert progress_filter.filter(record) is True
    assert counter.value == 1
    assert record.msg.startswith("Trial 3/10 ")
    assert "/10/10" not in record.msg

    again = logging.LogRecord(
        "optuna.study.study", logging.INFO, __file__, 1,
        "Trial 8/10 finished with value: 1.0.", (), None,
    )
    assert progress_filter.filter(again) is True
    assert again.msg == "Trial 8/10 finished with value: 1.0."
    assert counter.value == 1


def _spawn_add(progress, amount):
    return progress["counter"].add(amount)


def test_shared_counter_reaches_spawn_worker():
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    counter = SharedFinishCounter.create()
    try:
        progress = make_trial_progress(2160, 4, counter, started_at=time.time() - 10)
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            result = pool.submit(_spawn_add, progress, 3).result(timeout=60)
        assert result == 3
        assert counter.read() == 3
        assert progress["target_trials"] == 2160
        assert progress["finished_before"] == 4
    finally:
        counter.close(unlink=True)


def test_worker_entry_rewrites_trial_log_without_opening_real_journal(monkeypatch):
    from optimizer import runtime as optimizer

    class DummyStudy:
        user_attrs = {"_optimizer_worker_config_version": 1}

        def optimize(self, *args, **kwargs):
            logging.getLogger("optuna.study.study").info(_FINISHED.format(number=12))

    monkeypatch.setattr(optimizer, "HAS_JOURNAL", True)
    monkeypatch.setattr(optimizer, "JournalStorage", lambda backend: object())
    monkeypatch.setattr(optimizer, "JournalFileBackendCls", lambda log_file, **kwargs: object())
    monkeypatch.setattr(optimizer.optuna, "create_study", lambda **kwargs: DummyStudy())
    monkeypatch.setattr(optimizer, "ensure_study_config_version", lambda study: None)
    monkeypatch.setattr(optimizer, "TPESampler", lambda **kwargs: SimpleNamespace(kwargs=kwargs))
    monkeypatch.setattr(optimizer.OptimizationJob, "from_worker_payload", lambda payload: SimpleNamespace(objective=lambda trial: 1.0))
    monkeypatch.setattr(optimizer.OptimizationJob, "_cleanup_shared_segments", lambda handles, unlink=False: None)
    monkeypatch.setattr(optimizer, "get_optimizer_terminal_log_path", lambda: None)

    counter = TrialFinishCounter()
    capture = _Capture()
    root = logging.getLogger("optuna")
    root.addHandler(capture)
    try:
        result = optimizer._optimize_worker_entry(
            {"args": SimpleNamespace(), "train_datas": {}, "runtime_config": {}},
            "dummy-study",
            "dummy.log",
            10,
            1,
            7,
            trial_progress=_progress(counter, time.time() - 30),
        )
    finally:
        root.removeHandler(capture)

    assert result["stopped_early"] is False
    assert counter.value == 1
    assert capture.messages[0].startswith("Trial 12/2160 ")
    assert "Best is trial 10 with value: 1.0." in capture.messages[0]


def test_multiprocess_submit_shares_target_budget_without_shifting_worker_args(monkeypatch):
    from concurrent.futures import Future

    from optimizer import runtime as optimizer

    submitted = []

    class DummyExecutor:
        def __init__(self, max_workers=None, mp_context=None):
            self.max_workers = max_workers

        def submit(self, *args, **kwargs):
            submitted.append((args, kwargs))
            future = Future()
            future.set_result({"stopped_early": False})
            return future

        def shutdown(self, wait=True, cancel_futures=False):
            return None

    monkeypatch.setattr(optimizer, "ProcessPoolExecutor", DummyExecutor)
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="dummy-study")
    job._resolve_worker_count = lambda requested: 2
    job._build_worker_payload = lambda: {"runtime_config": {}}
    job._build_spawn_shared_payload = lambda payload: (payload, [])

    job._run_multiprocess_optimization(
        n_jobs=2,
        n_trials=4,
        log_file="dummy.log",
        progress_target=2160,
        progress_finished_before=1033,
    )

    assert len(submitted) == 2
    first_progress = submitted[0][1]["trial_progress"]
    second_progress = submitted[1][1]["trial_progress"]
    assert first_progress is second_progress
    assert first_progress["target_trials"] == 2160
    assert first_progress["finished_before"] == 1033
    assert int(first_progress["counter"].value) == 0
    assert len(submitted[0][0]) == 8
    assert "trial_progress" not in submitted[0][0][-1].__class__.__name__.lower()
