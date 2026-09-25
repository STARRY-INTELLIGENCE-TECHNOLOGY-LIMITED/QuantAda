"""多指标 dashboard 只读聚合各 Journal，不合并训练文件。"""

from pathlib import Path
import logging
import threading

import optuna
from optuna.storages import InMemoryStorage, JournalStorage
from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock

from optimizer.dashboard_view import build_dashboard_storage, launch_multi_metric_dashboard


def _write_study(path, name, value):
    path = Path(path)
    storage = JournalStorage(JournalFileBackend(str(path), lock_obj=JournalFileOpenLock(str(path))))
    study = optuna.create_study(study_name=name, direction="maximize", storage=storage)
    study.set_user_attr("metric_name", name)

    def objective(trial):
        trial.suggest_int("x", 0, 10)
        return float(value)

    study.optimize(objective, n_trials=1)
    return path.read_bytes()


def _values(storage):
    return {
        name: optuna.load_study(study_name=name, storage=storage).best_value
        for name in optuna.get_all_study_names(storage)
    }


def test_single_journal_stays_on_the_original_file(tmp_path):
    path = tmp_path / "one.log"
    before = _write_study(path, "mix_score_defender", 4)
    view = build_dashboard_storage([str(path)])

    assert view.aggregated is False
    assert not isinstance(view.storage, InMemoryStorage)
    assert view.study_names == ("mix_score_defender",)
    assert view.storage.get_study_user_attrs(view.storage.get_study_id_from_name("mix_score_defender"))["metric_name"] == "mix_score_defender"
    assert path.read_bytes() == before


def test_metric_journals_are_aggregated_without_rewriting_sources(tmp_path):
    names = ("mix_score_defender", "us_robust", "option_carry_robust")
    paths = []
    before = {}
    for index, name in enumerate(names, 1):
        path = tmp_path / f"{name}.log"
        before[path] = _write_study(path, name, index)
        paths.append(str(path))

    view = build_dashboard_storage(paths)
    copied = optuna.load_study(study_name="us_robust", storage=view.storage)
    copied.set_user_attr("dashboard_note", "local only")

    assert view.aggregated is True
    assert isinstance(view.storage, InMemoryStorage)
    assert view.study_names == names
    assert _values(view.storage) == {
        "mix_score_defender": 1.0,
        "us_robust": 2.0,
        "option_carry_robust": 3.0,
    }
    assert all(path.read_bytes() == before[path] for path in before)
    restored = JournalStorage(JournalFileBackend(paths[1], lock_obj=JournalFileOpenLock(paths[1])))
    assert "dashboard_note" not in optuna.load_study(study_name="us_robust", storage=restored).user_attrs


def test_duplicate_study_names_get_a_suffix_and_keep_both_values(tmp_path):
    first = tmp_path / "a.log"
    second = tmp_path / "b.log"
    _write_study(first, "shared", 1)
    _write_study(second, "shared", 9)
    view = build_dashboard_storage([str(first), str(second)])

    assert view.study_names == ("shared", "shared__2")
    assert _values(view.storage) == {"shared": 1.0, "shared__2": 9.0}


def test_unreadable_journal_is_skipped_and_missing_file_is_not_created(tmp_path):
    good = tmp_path / "good.log"
    bad = tmp_path / "bad.log"
    missing = tmp_path / "missing.log"
    before = _write_study(good, "kept", 6)
    bad.write_text("{bad\n{also\n", encoding="utf-8")
    bad_before = bad.read_bytes()

    view = build_dashboard_storage([str(good), str(bad), str(missing), str(good)])

    assert view.aggregated is False
    assert view.study_names == ("kept",)
    assert _values(view.storage) == {"kept": 6.0}
    assert any("skipped" in message and str(bad) in message for message in view.warnings)
    assert any("not found" in message for message in view.warnings)
    assert not missing.exists()
    assert good.read_bytes() == before
    assert bad.read_bytes() == bad_before


def test_all_unreadable_journals_do_not_invent_a_storage(tmp_path):
    first = tmp_path / "a.log"
    last = tmp_path / "b.log"
    first.write_text("{bad\n{also\n", encoding="utf-8")
    last.write_text("{bad\n{also\n", encoding="utf-8")
    before = last.read_bytes()

    view = build_dashboard_storage([str(first), str(last)])

    assert view.storage is None
    assert view.aggregated is False
    assert any("aggregation failed" in message for message in view.warnings)
    assert last.read_bytes() == before


def test_multi_metric_launch_passes_every_journal_not_only_the_last(tmp_path, capsys):
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.log"
        path.write_text("placeholder\n", encoding="utf-8")
        paths.append(str(path))
    captured = {}

    class Job:
        def _launch_dashboard(self, log_file, port=8080, background=True, log_files=None):
            captured.update(log_file=log_file, port=port, background=background, log_files=list(log_files))

    assert launch_multi_metric_dashboard(Job(), paths, port=8090, port_in_use=lambda port: port == 8090)
    assert captured["log_files"] == paths
    assert captured["log_file"] == paths[-1]
    assert captured["background"] is False
    assert captured["port"] == 8091
    assert "3 journals" in capsys.readouterr().out


def test_multi_metric_launch_does_not_start_without_a_journal(tmp_path, capsys):
    called = False

    class Job:
        def _launch_dashboard(self, *args, **kwargs):
            nonlocal called
            called = True

    assert launch_multi_metric_dashboard(
        Job(), [str(tmp_path / "missing.log")], port=8090, port_in_use=lambda port: False,
    ) is False
    assert called is False
    assert "not found" in capsys.readouterr().out


def test_launch_dashboard_serves_every_metric_study(tmp_path, monkeypatch, capsys):
    runtime = __import__("optimizer.runtime", fromlist=["OptimizationJob"])
    if not runtime.HAS_DASHBOARD:
        return
    names = ("mix_score_defender", "us_robust", "option_carry_robust")
    paths = []
    before = {}
    for index, name in enumerate(names, 1):
        path = tmp_path / f"{name}.log"
        before[path] = _write_study(path, name, index)
        paths.append(str(path))
    captured = {}

    def fake_run_server(storage, host, port, **kwargs):
        captured["host"] = host
        captured["port"] = port
        captured["names"] = tuple(optuna.get_all_study_names(storage))
        captured["values"] = _values(storage)

    class ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            if self._target:
                self._target(*self._args)

    monkeypatch.setattr(runtime, "run_server", fake_run_server)
    monkeypatch.setattr(runtime.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(runtime.time, "sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.threading, "Thread", ImmediateThread)
    job = runtime.OptimizationJob.__new__(runtime.OptimizationJob)
    logger = logging.getLogger("optuna")
    saved_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        job._launch_dashboard(paths[-1], port=8090, background=False, log_files=paths)
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(saved_level)

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8090
    assert captured["names"] == names
    assert captured["values"] == {
        "mix_score_defender": 1.0,
        "us_robust": 2.0,
        "option_carry_robust": 3.0,
    }
    assert "3 studies" in capsys.readouterr().out
    assert all(path.read_bytes() == before[path] for path in before)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _dashboard_filters():
    from optimizer.dashboard_view import _DashboardThreadLogFilter

    logger = logging.getLogger("optuna")
    return [
        item
        for handler in logger.handlers
        for item in list(handler.filters)
        if isinstance(item, _DashboardThreadLogFilter)
    ]


def test_background_dashboard_keeps_training_optuna_logs(tmp_path, monkeypatch):
    runtime = __import__("optimizer.runtime", fromlist=["OptimizationJob"])
    if not runtime.HAS_DASHBOARD:
        return
    path = tmp_path / "one.log"
    _write_study(path, "mix_score_defender", 1)
    started = threading.Event()
    release = threading.Event()

    def fake_run_server(storage, host, port, **kwargs):
        logging.getLogger("optuna.study.study").info(
            "Trial 9 finished with value: 9.0 and parameters: {}."
        )
        started.set()
        assert release.wait(5)

    created = []
    real_thread = runtime.threading.Thread

    class _RecordingThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(runtime, "run_server", fake_run_server)
    monkeypatch.setattr(runtime.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(runtime.time, "sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.threading, "Thread", _RecordingThread)
    logger = logging.getLogger("optuna")
    original_level = logger.level
    logger.setLevel(logging.DEBUG)
    capture = _Capture()
    logger.addHandler(capture)
    job = runtime.OptimizationJob.__new__(runtime.OptimizationJob)
    try:
        job._launch_dashboard(str(path), port=8090, background=True)
        assert started.wait(5)
        logging.getLogger("optuna.study.study").info(
            "Trial 1 finished with value: 1.0 and parameters: {}."
        )
        assert logger.level == logging.DEBUG
        assert "Trial 1 finished with value: 1.0 and parameters: {}." in capture.messages
        assert "Trial 9 finished with value: 9.0 and parameters: {}." not in capture.messages
        assert created
        assert _dashboard_filters()
    finally:
        release.set()
        for thread in created:
            thread.join(5)
        logger.removeHandler(capture)
        logger.setLevel(original_level)
    assert created and not any(thread.is_alive() for thread in created)
    assert not _dashboard_filters()


def test_dashboard_log_filter_does_not_stack_or_keep_caller_silent(tmp_path):
    from optimizer.dashboard_view import begin_dashboard_log_scope, end_dashboard_log_scope

    runtime = __import__("optimizer.runtime", fromlist=["OptimizationJob"])
    logger = logging.getLogger("optuna")
    original_level = logger.level
    logger.setLevel(logging.INFO)
    capture = _Capture()
    logger.addHandler(capture)
    try:
        saved = begin_dashboard_log_scope()
        again = begin_dashboard_log_scope()
        active = _dashboard_filters()
        assert active
        assert len({id(item) for item in active}) == 1
        assert logger.level == logging.INFO
        logging.getLogger("optuna.study.study").info(
            "Trial 2 finished with value: 1.0 and parameters: {}."
        )
        assert capture.messages == []
        end_dashboard_log_scope(again)
        end_dashboard_log_scope(saved)
        assert not _dashboard_filters()
        assert logger.level == logging.INFO
        if runtime.HAS_DASHBOARD:
            job = runtime.OptimizationJob.__new__(runtime.OptimizationJob)
            job._launch_dashboard(str(tmp_path / "missing.log"), port=8093, background=False)
            assert logger.level == logging.INFO
            assert not _dashboard_filters()
        logging.getLogger("optuna.study.study").info(
            "Trial 4 finished with value: 1.0 and parameters: {}."
        )
        assert capture.messages == ["Trial 4 finished with value: 1.0 and parameters: {}."]
    finally:
        logger.removeHandler(capture)
        logger.setLevel(original_level)
        end_dashboard_log_scope(logger.level)
