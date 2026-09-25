"""验证旧 Journal 匹配、重复命令预算与进程互斥。"""

import ast
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import optuna
from optuna.storages import JournalStorage
from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock
import pandas as pd
import pytest

import config
import optimizer.runtime as runtime
from optimizer.study_resume import ensure_study_config_version, legacy_owner_running, resolve_study_plan, study_run_lock
from optimizer.journal_metadata import WORKER_CONFIG_VERSION, isolate_study_journal, read_study_metadata


def _args(**overrides):
    values = dict(
        strategy="test_strategy.Strategy", selection="test_selector.Selector", symbols="AAA",
        data_source="csv", cash=100000.0, commission=0.0005, slippage=0.0001,
        timeframe="Days", compression=1, risk=None, risk_params="{}", config="{'LOT_SIZE': 1}",
        params="{'contracts': 1}", opt_params="{'x': {'type': 'int', 'low': 0, 'high': 5}}",
        train_roll_period="2y", test_roll_period="12m", train_ratio=None, train_period=None, test_period=None,
        start_date="20230923", end_date="20260923", metric="return,sharpe", n_jobs=1, n_trials=2,
        study_name=None, study_journal=None, opt_schedule=None, refresh=False, no_plot=True, plot_scope="full",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _storage(path):
    return JournalStorage(JournalFileBackend(str(path), lock_obj=JournalFileOpenLock(str(path))))


def _write_study(path, name, args, metric="return", config_version=WORKER_CONFIG_VERSION):
    study = optuna.create_study(storage=_storage(path), study_name=name, direction="maximize", load_if_exists=True)
    for key, value in vars(args).items():
        study.set_user_attr(key, str(metric if key == "metric" else value))
    if config_version is not None:
        study.set_user_attr("_optimizer_worker_config_version", config_version)
    return study


def _plan(args, directory, window=None):
    if window is None:
        window = (args.start_date, args.end_date)
    return resolve_study_plan(
        args, ast.literal_eval(args.params), ast.literal_eval(args.opt_params),
        ast.literal_eval(args.risk_params), args.metric.split(","), directory, window,
    )


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path / "data"))
    for key in ("QUANTADA_STUDY_NAME", "QUANTADA_STUDY_JOURNAL", "QUANTADA_OPTIMIZER_TERMINAL_LOG"):
        monkeypatch.delenv(key, raising=False)


def test_timestamp_named_batch_is_matched_and_missing_metrics_share_its_journal(tmp_path):
    path = tmp_path / "optuna_legacy.log"
    args = _args()
    name = "2Y_12M_return_US_TR20230923-20250922_TE20250923-20260923_RUN20260923-092823_99999999"
    _write_study(path, name, args)

    plan = _plan(args, tmp_path)

    assert plan["journal"] == str(path)
    assert plan["studies"]["return"] == name
    assert plan["studies"]["sharpe"] != name
    assert len(plan["matched"]) == 1


def test_unversioned_scores_are_loaded_by_name_and_not_relabeled(tmp_path):
    journal = tmp_path / "optuna_legacy.log"
    old = _write_study(journal, "old", _args(), config_version=None)
    old.add_trial(optuna.trial.create_trial(value=-43.0))
    before = journal.read_bytes()
    args = _args(start_date="20230924", end_date="20260924")
    plan = _plan(args, tmp_path, (None, None))
    assert journal.read_bytes() == before
    assert plan["journal"] == str(journal)
    assert plan["studies"]["return"] == "old"
    assert plan["reused_pre_config"] is True
    assert plan["incompatible"] == []
    assert (args.start_date, args.end_date) == ("20230923", "20260923")
    assert _plan(_args(), tmp_path)["studies"] == plan["studies"]
    sharpe = _write_study(journal, plan["studies"]["sharpe"], _args(), "sharpe")
    sharpe.add_trial(optuna.trial.create_trial(value=7.0))
    resumed = _plan(_args(), tmp_path)
    assert resumed["studies"]["return"] == "old"
    assert resumed["studies"]["sharpe"] == plan["studies"]["sharpe"]
    assert resumed["incompatible"] == []
    assert old.trials[0].value == -43.0
    assert "_optimizer_worker_config_version" not in old.user_attrs


def test_explicit_unversioned_study_can_be_loaded_without_relabeling(tmp_path):
    journal = tmp_path / "optuna_legacy.log"
    old = _write_study(journal, "old", _args(), config_version=None)
    old.add_trial(optuna.trial.create_trial(value=99.0))
    before = journal.read_bytes()
    plan = _plan(_args(study_name="old", study_journal=str(journal)), tmp_path)
    assert plan["studies"]["return"] == "old"
    assert journal.read_bytes() == before
    ensure_study_config_version(old)
    assert "_optimizer_worker_config_version" not in old.user_attrs
    assert old.user_attrs["_optimizer_reused_pre_config_version"] is True
    ensure_study_config_version(old)


def test_explicit_different_version_cannot_overwrite_existing_scores(tmp_path):
    journal = tmp_path / "optuna_legacy.log"
    old = _write_study(journal, "old", _args(), config_version=WORKER_CONFIG_VERSION + 1)
    old.add_trial(optuna.trial.create_trial(value=99.0))
    before = journal.read_bytes()
    with pytest.raises(ValueError, match="configuration version"):
        _plan(_args(study_name="old", study_journal=str(journal)), tmp_path)
    with pytest.raises(ValueError, match="configuration version"):
        ensure_study_config_version(old)
    assert journal.read_bytes() == before


def test_blank_study_can_be_initialized_with_current_config_version(tmp_path):
    study = optuna.create_study(storage=_storage(tmp_path / "empty.log"), study_name="blank")
    ensure_study_config_version(study)
    ensure_study_config_version(study)
    assert study.user_attrs["_optimizer_worker_config_version"] == WORKER_CONFIG_VERSION


def test_implicit_dates_keep_existing_window_across_days(tmp_path):
    path = tmp_path / "optuna_legacy.log"
    _write_study(path, "existing", _args())
    args = _args(start_date="20230924", end_date="20260924")

    plan = _plan(args, tmp_path, (None, None))

    assert plan["studies"]["return"] == "existing"
    assert (args.start_date, args.end_date) == ("20230923", "20260923")


@pytest.mark.parametrize("overrides", [
    {"end_date": "20260924"}, {"cash": 200000.0}, {"commission": 0.01}, {"slippage": 0.01},
    {"strategy": "another.Strategy"}, {"selection": "another.Selector"}, {"data_source": "theta+futu"},
    {"params": "{'contracts': 2}"}, {"opt_params": "{'x': {'type': 'int', 'low': 0, 'high': 8}}"},
    {"risk": "risk_control"}, {"risk_params": "{'limit': 0.1}"}, {"config": "{'LOT_SIZE': 100}"},
    {"timeframe": "Minutes"}, {"compression": 5}, {"test_roll_period": "6m"},
])
def test_changed_training_identity_is_not_mixed_with_existing_scores(tmp_path, overrides):
    path = tmp_path / "optuna_legacy.log"
    _write_study(path, "existing", _args())

    plan = _plan(_args(**overrides), tmp_path)

    assert plan["journal"] != str(path)
    assert plan["matched"] == []


def test_budget_workers_dict_order_and_metric_order_do_not_change_identity(tmp_path):
    path = tmp_path / "optuna_legacy.log"
    original = _args(params="{'contracts': 1, 'days': 30}")
    _write_study(path, "old_return", original)
    _write_study(path, "old_sharpe", original, "sharpe")
    args = _args(params="{ 'days': 30, 'contracts': 1 }", n_jobs=4, n_trials=10, metric="sharpe,return", no_plot=False)

    plan = _plan(args, tmp_path)

    assert plan["studies"] == {"sharpe": "old_sharpe", "return": "old_return"}


def test_latest_metadata_update_wins_over_study_creation_order(tmp_path):
    path = tmp_path / "optuna_batch.log"
    first = _write_study(path, "first", _args())
    _write_study(path, "second", _args())
    first.set_user_attr("n_trials", "10")

    assert _plan(_args(), tmp_path)["studies"]["return"] == "first"


def test_explicit_snapshot_anchor_restores_all_metrics_of_selected_data(tmp_path):
    journal = tmp_path / "optuna_versions.log"
    old_ref = {"id": "a" * 32, "sha256": "1" * 64}
    new_ref = {"id": "b" * 32, "sha256": "2" * 64}
    for prefix, reference in (("old", old_ref), ("new", new_ref)):
        for metric in ("return", "sharpe"):
            study = _write_study(journal, f"{prefix}_{metric}", _args(), metric)
            study.set_user_attr("_optimizer_data_snapshot", reference)
    selected = _plan(_args(study_journal=str(journal), study_name="old_return"), tmp_path)
    assert selected["studies"] == {"return": "old_return", "sharpe": "old_sharpe"}
    latest = _plan(_args(), tmp_path)
    assert latest["studies"] == {"return": "new_return", "sharpe": "new_sharpe"}


def test_stable_names_separate_metrics_with_same_sanitized_prefix(tmp_path):
    args = _args(metric="private.a,private_a")
    first = _plan(args, tmp_path)
    second = _plan(_args(metric="private_a,private.a"), tmp_path)
    assert first["journal"] == second["journal"]
    assert first["studies"] == second["studies"]
    assert len(set(first["studies"].values())) == 2


def test_explicit_journal_can_auto_match_old_names_without_name_argument(tmp_path):
    path = tmp_path / "custom.journal"
    _write_study(path, "old_return", _args())
    plan = _plan(_args(study_journal=str(path)), tmp_path)
    assert plan["journal"] == str(path)
    assert plan["studies"]["return"] == "old_return"


def test_snapshot_anchor_matches_sibling_without_reusing_other_metric(tmp_path):
    journal = tmp_path / "optuna_versions.log"
    reference = {"id": "a" * 32, "sha256": "1" * 64}
    for metric in ("return", "sharpe"):
        study = _write_study(journal, f"old_{metric}", _args(), metric)
        study.set_user_attr("_optimizer_data_snapshot", reference)

    plan = _plan(_args(metric="sharpe", study_journal=str(journal), study_name="old_return"), tmp_path)

    assert plan["studies"] == {"sharpe": "old_sharpe"}
    assert [study["name"] for study in plan["matched"]] == ["old_sharpe"]


def test_snapshot_anchor_rejects_reusing_name_for_missing_metric(tmp_path):
    journal = tmp_path / "optuna_versions.log"
    reference = {"id": "c" * 32, "sha256": "d" * 64}
    study = _write_study(journal, "old_return", _args(metric="return"), "return")
    study.set_user_attr("_optimizer_data_snapshot", reference)

    with pytest.raises(ValueError, match="different metric"):
        _plan(_args(metric="sharpe", study_journal=str(journal), study_name="old_return"), tmp_path)


def test_explicit_study_name_is_found_in_old_shared_journal(tmp_path):
    path = tmp_path / "optuna_old_batch.log"
    _write_study(path, "old_return", _args())
    plan = _plan(_args(study_name="old_return"), tmp_path)
    assert plan["journal"] == str(path)
    assert plan["studies"]["return"] == "old_return"


def test_boolean_and_numeric_strategy_parameters_do_not_share_scores(tmp_path):
    path = tmp_path / "optuna_legacy.log"
    _write_study(path, "existing", _args(params="{'mode': True}"))
    assert _plan(_args(params="{'mode': 1}"), tmp_path)["matched"] == []


@pytest.mark.parametrize("overrides", [{"cash": 200000.0}, {"metric": "sharpe"}])
def test_explicit_study_rejects_incompatible_configuration(tmp_path, overrides):
    path = tmp_path / "optuna_manual.log"
    _write_study(path, "manual", _args())
    with pytest.raises(ValueError, match="different"):
        _plan(_args(study_name="manual", **overrides), tmp_path)


def test_schedule_uses_new_slot_window_instead_of_freezing_old_dates(tmp_path):
    path = tmp_path / "optuna_legacy.log"
    _write_study(path, "existing", _args())
    args = _args(start_date="20230924", end_date="20260924", opt_schedule="1d:02:00")
    assert _plan(args, tmp_path, (None, None))["matched"] == []


def test_reader_handles_duplicate_creates_deletions_and_partial_tail(tmp_path):
    path = tmp_path / "optuna_legacy.log"
    args = _args()
    _write_study(path, "deleted", args)
    _write_study(path, "deleted", args)
    optuna.delete_study(study_name="deleted", storage=_storage(path))
    _write_study(path, "survivor", args)
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"op_code":2,"study_id":1')

    assert [study["name"] for study in read_study_metadata(path)] == ["survivor"]
    assert _plan(args, tmp_path)["studies"]["return"] == "survivor"


def test_incomplete_study_metadata_is_not_used_for_automatic_resume(tmp_path):
    path = tmp_path / "optuna_incomplete.log"
    study = optuna.create_study(storage=_storage(path), study_name="incomplete", direction="maximize")
    study.set_user_attr("metric", "return")
    assert _plan(_args(), tmp_path)["matched"] == []


def test_journal_lock_reports_only_contention_and_raises_other_io_errors(tmp_path, monkeypatch):
    journal = tmp_path / "optuna_lock.log"

    def fail(*_args, **_kwargs):
        raise OSError(9, "bad file descriptor")

    if os.name == "nt":
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", fail)
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", fail)
    with pytest.raises(OSError, match="bad file descriptor"):
        with study_run_lock(journal) as acquired:
            assert acquired


def test_journal_lock_is_nonblocking_and_released_after_error(tmp_path):
    journal = tmp_path / "optuna_lock.log"
    with pytest.raises(RuntimeError):
        with study_run_lock(journal) as acquired:
            assert acquired
            with study_run_lock(journal) as duplicate:
                assert duplicate is False
            raise RuntimeError("interrupted")
    with study_run_lock(journal) as acquired:
        assert acquired


def test_journal_lock_is_released_when_owner_process_dies(tmp_path):
    journal = tmp_path / "optuna_lock.log"
    code = (
        "import os, sys; from optimizer.study_resume import study_run_lock; "
        "guard=study_run_lock(sys.argv[1]); assert guard.__enter__(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", code, str(journal)], check=True, timeout=30)
    with study_run_lock(journal) as acquired:
        assert acquired


def test_legacy_live_owner_is_detected_without_signalling_it():
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    assert legacy_owner_running(f"example_RUN{stamp}_{os.getpid()}") is True
    assert legacy_owner_running("stable_hash_name") is False
    assert legacy_owner_running(f"example_RUN{stamp}_99999999") is False


@pytest.mark.skipif(os.name != "nt", reason="Windows 保留已退出父进程的 worker 父 PID")
def test_windows_orphan_workers_are_detected_for_legacy_and_new_studies(tmp_path):
    release = tmp_path / "release"
    child_code = (
        "import pathlib,sys,time; marker=pathlib.Path(sys.argv[1]); end=time.monotonic()+20\n"
        "while not marker.exists() and time.monotonic()<end: time.sleep(0.05)\n"
    )
    parent_code = (
        "import json,os,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]], "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, "
        "creationflags=subprocess.CREATE_NO_WINDOW); print(json.dumps({'pid':os.getpid()}))"
    )
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        result = subprocess.run(
            [sys.executable, "-c", parent_code, child_code, str(release)],
            capture_output=True, text=True, check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        owner = f"optimizer_RUN{stamp}_{json.loads(result.stdout)['pid']}"
        assert legacy_owner_running(owner) is True
        assert legacy_owner_running(owner, workers_only=True) is True
    finally:
        release.touch()
    deadline = time.monotonic() + 5
    while legacy_owner_running(owner) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert legacy_owner_running(owner) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows 校验进程创建时间以排除 PID 复用")
def test_windows_process_started_after_study_is_not_its_owner():
    assert legacy_owner_running(f"old_RUN20000101-000000_{os.getpid()}") is False


def test_running_job_is_skipped_before_fetching_data(monkeypatch, tmp_path, capsys):
    directory = Path(config.DATA_PATH) / "optuna"
    directory.mkdir(parents=True)
    path = directory / "optuna_legacy.log"
    _write_study(path, "old", _args())
    monkeypatch.setattr(runtime, "legacy_owner_running", lambda name, **kwargs: True)
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_: False)
    monkeypatch.setattr(runtime.OptimizationJob, "__init__", lambda *a, **k: pytest.fail("must not fetch data"))
    args = _args()

    assert runtime.run_optimizer_mode(args, ast.literal_eval(args.params), {}, []) == 0
    assert "duplicate launch skipped" in capsys.readouterr().out


def test_automatic_resume_requires_persistent_storage(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "HAS_JOURNAL", False)
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_: False)
    monkeypatch.setattr(runtime.OptimizationJob, "__init__", lambda *a, **k: pytest.fail("must not fetch data"))
    args = _args()
    assert runtime.run_optimizer_mode(args, ast.literal_eval(args.params), {}, []) == 1
    assert "requires Optuna JournalStorage" in capsys.readouterr().out


def test_resumable_study_write_failure_does_not_fall_back_to_memory(monkeypatch, tmp_path):
    def denied(**kwargs):
        error = OSError("permission denied")
        error.winerror = 1314
        raise error

    monkeypatch.setattr(runtime.optuna, "create_study", denied)
    job = runtime.OptimizationJob.__new__(runtime.OptimizationJob)
    job.args = _args(study_name="manual", shared_journal_log_file=str(tmp_path / "optuna_manual.log"))
    job.strategy_class = SimpleNamespace(option_universe=("PUT",))
    job.opt_params_def = ast.literal_eval(job.args.opt_params)
    with pytest.raises(RuntimeError, match="Cannot persist resumable Study"):
        job.run()


@pytest.mark.parametrize("interruption_state", [optuna.trial.TrialState.RUNNING, optuna.trial.TrialState.FAIL])
@pytest.mark.parametrize("legacy_scores", [False, True])
def test_repeated_multi_metric_command_reuses_trials_and_tops_up_budget(monkeypatch, tmp_path, interruption_state, legacy_scores):
    evaluated = []
    frame = pd.DataFrame({"close": [1.0, 1.1, 1.2]}, index=pd.date_range("2024-01-01", periods=3))
    context = {
        "strategy_class": SimpleNamespace(option_universe=("PUT",)), "risk_control_classes": [],
        "data_manager": None, "target_symbols": ["AAA"], "raw_datas": {"AAA": frame},
        "train_datas": {"AAA": frame}, "test_datas": {}, "train_range": ("20240101", "20240103"),
        "test_range": (None, None),
    }

    class OfflineJob(runtime.OptimizationJob):
        def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
            super().__init__(args, fixed_params, opt_params_def, risk_params, shared_context=shared_context or context)

        def _evaluate_trial_params(self, params):
            evaluated.append((self.args.metric, params["x"]))
            return float(params["x"])

    monkeypatch.setattr(runtime, "OptimizationJob", OfflineJob)
    monkeypatch.setattr(runtime, "get_class_from_name", lambda *a, **k: context["strategy_class"])
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_: False)
    monkeypatch.setattr(OfflineJob, "_launch_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(OfflineJob, "_run_main_eval_backtest", lambda *a, **k: None)
    monkeypatch.setattr(OfflineJob, "_run_yearly_validation_backtests", lambda *a, **k: [])
    monkeypatch.setattr(runtime.sys, "argv", ["run.py"])
    if legacy_scores:
        legacy_path = Path(config.DATA_PATH) / "optuna" / "optuna_legacy.log"
        legacy_path.parent.mkdir(parents=True)
        legacy = _write_study(legacy_path, "legacy-score", _args(strategy="other.Strategy"), config_version=None)
        legacy.add_trial(optuna.trial.create_trial(value=9999.0))
    interrupted = None
    for step, (budget, expected) in enumerate(((2, 4), (2, 4), (4, 8), (4, 8))):
        args = _args(n_trials=budget)
        assert runtime.run_optimizer_mode(args, ast.literal_eval(args.params), {}, ["AAA"]) == 0
        assert len(evaluated) == expected
        if step == 0:
            journal = next(
                path for path in (Path(config.DATA_PATH) / "optuna").glob("*.log")
                if path.name != "optuna_legacy.log"
            )
            name = next(info["name"] for info in read_study_metadata(journal)
                        if info["attrs"].get("_optimizer_worker_config_version") == WORKER_CONFIG_VERSION)
            study = optuna.load_study(
                study_name=name, storage=_storage(journal), sampler=optuna.samplers.GridSampler({"x": list(range(6))}),
            )
            trial = study.ask()
            sampled = trial.suggest_int("x", 0, 5)
            interrupted = (name, trial.number, sampled)
            if interruption_state == optuna.trial.TrialState.FAIL:
                study.tell(trial, state=interruption_state)
    journals = sorted((Path(config.DATA_PATH) / "optuna").glob("*.log"))
    if legacy_scores:
        assert len(journals) == 3
        assert len(legacy.trials) == 1
        assert legacy.best_value == 9999.0
        assert "_optimizer_worker_config_version" not in legacy.user_attrs
        journals = [path for path in journals if path != legacy_path]
    assert len(journals) == 2
    studies = []
    for journal in journals:
        infos = read_study_metadata(journal)
        assert len(infos) == 1
        studies.append(infos[0])
        study = optuna.load_study(study_name=infos[0]["name"], storage=_storage(journal))
        assert all(trial.number == trial._trial_id for trial in study.trials)
    for info in studies:
        journal = next(path for path in journals if read_study_metadata(path)[0]["name"] == info["name"])
        study = optuna.load_study(study_name=info["name"], storage=_storage(journal))
        assert study.best_value <= 5.0
        has_failure = info["name"] == interrupted[0] and interruption_state == optuna.trial.TrialState.FAIL
        assert len(study.trials) == (5 if has_failure else 4)
        assert sum(trial.state == optuna.trial.TrialState.COMPLETE for trial in study.trials) == 4
        if info["name"] == interrupted[0]:
            assert study.trials[interrupted[1]].params["x"] == interrupted[2]
            if has_failure:
                retry = next(trial for trial in study.trials if trial.system_attrs.get("failed_trial") == interrupted[1])
                assert retry.params["x"] == interrupted[2]
                assert retry.state == optuna.trial.TrialState.COMPLETE


def test_optimizer_cli_preserves_omitted_dates_for_resume_matching(monkeypatch):
    import run

    captured = []

    def optimize(args, **kwargs):
        captured.append((args.start_date, args.end_date))
        return 0

    monkeypatch.setattr(run.sys, "argv", ["run.py", "dummy_strategy", "--opt_params", "{'x': {'type': 'int', 'low': 1, 'high': 2}}"])
    monkeypatch.setattr(run.optimizer, "run_optimizer_mode", optimize)
    monkeypatch.setattr(run, "infer_omitted_backtest_window", lambda *_: pytest.fail("date inference is premature"))
    with pytest.raises(SystemExit) as result:
        run._run_main()
    assert result.value.code == 0
    assert captured == [(None, None)]


def test_multi_metric_groups_keep_one_window_when_wall_clock_crosses_days(monkeypatch, tmp_path):
    """一次多指标批次只推断一次窗口；即使每组开始时已跨日，日期也不能漂移。"""
    windows = []
    infer_calls = []
    context = {
        "strategy_class": SimpleNamespace(option_universe=("PUT",)), "risk_control_classes": [],
        "data_manager": None, "target_symbols": ["AAA"], "source_symbols": ["AAA"],
        "raw_datas": {}, "train_datas": {}, "test_datas": {},
        "train_range": ("20240101", "20240103"), "test_range": (None, None),
        "warmup_days": 0, "window_data_cache": {}, "indicator_cache": {},
        "raw_data_fetch_range": ("20240101", "20240103"), "snapshot_frozen": True,
    }

    class SlowWindowJob:
        def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
            windows.append((args.metric, args.start_date, args.end_date))
            self.args = args
            self.fixed_params = fixed_params
            self.target_symbols = ["AAA"]
            self.train_range = context["train_range"]
            self.test_range = context["test_range"]

        @staticmethod
        def _resolve_worker_count(value):
            return 1

        @classmethod
        def build_optuna_name_tag(cls, **kwargs):
            return "slow-window-test"

        def export_shared_context(self):
            return context

        def run(self):
            return None

    def infer_once(args):
        infer_calls.append(len(infer_calls))
        # 若入口被错误地按 metric 重复调用，这里故意推进日期来暴露漂移。
        day = "20260921" if len(infer_calls) == 1 else f"2026092{1 + len(infer_calls)}"
        args.start_date = "20230921"
        args.end_date = day

    monkeypatch.setattr(runtime, "OptimizationJob", SlowWindowJob)
    monkeypatch.setattr(runtime, "infer_omitted_backtest_window", infer_once)
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_: False)
    monkeypatch.setattr(runtime.sys, "argv", ["run.py"])
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path / "data"))
    args = _args(
        start_date=None, end_date=None, metric="return,sharpe,calmar", n_trials=1,
    )

    assert runtime.run_optimizer_mode(args, ast.literal_eval(args.params), {}, ["AAA"]) == 0
    assert infer_calls == [0]
    assert len(windows) == 4  # 1 bootstrap + 3 metric jobs
    assert {window[1:] for window in windows} == {("20230921", "20260921")}


def test_keyboard_interrupt_stops_remaining_metrics_and_preserves_trials(monkeypatch, tmp_path):
    evaluated = []
    validations = []
    context = {
        "strategy_class": SimpleNamespace(option_universe=("PUT",)), "risk_control_classes": [],
        "data_manager": None, "target_symbols": ["AAA"], "raw_datas": {}, "train_datas": {},
        "test_datas": {}, "train_range": ("20240101", "20240103"), "test_range": (None, None),
    }

    class InterruptedJob(runtime.OptimizationJob):
        def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
            super().__init__(args, fixed_params, opt_params_def, risk_params, shared_context=context)

        def _evaluate_trial_params(self, params):
            evaluated.append(self.args.metric)
            if len(evaluated) == 2:
                raise KeyboardInterrupt()
            return -43.0

    monkeypatch.setattr(runtime, "OptimizationJob", InterruptedJob)
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_: False)
    monkeypatch.setattr(InterruptedJob, "_launch_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(InterruptedJob, "_run_main_eval_backtest", lambda *a, **k: validations.append("main"))
    monkeypatch.setattr(InterruptedJob, "_run_yearly_validation_backtests", lambda *a, **k: [])
    monkeypatch.setattr(runtime.sys, "argv", ["run.py"])
    args = _args()
    with pytest.raises(KeyboardInterrupt):
        runtime.run_optimizer_mode(args, ast.literal_eval(args.params), {}, ["AAA"])
    assert evaluated == ["return", "return"]
    assert validations == []
    journal = next((Path(config.DATA_PATH) / "optuna").glob("*.log"))
    info, = read_study_metadata(journal)
    study = optuna.load_study(study_name=info["name"], storage=_storage(journal))
    assert [trial.state for trial in study.trials] == [optuna.trial.TrialState.COMPLETE, optuna.trial.TrialState.FAIL]
    assert study.trials[0].value == -43.0
    with study_run_lock(journal) as acquired:
        assert acquired


def test_failed_metrics_do_not_emit_finished_markers(monkeypatch, capsys):
    from common.terminal_log import OPTIMIZER_AI_ANALYSIS_START_MARKER

    context = {
        "strategy_class": SimpleNamespace(option_universe=("PUT",)), "risk_control_classes": [],
        "data_manager": None, "target_symbols": ["AAA"], "raw_datas": {}, "train_datas": {},
        "test_datas": {}, "train_range": ("20240101", "20240103"), "test_range": (None, None),
    }

    class EmptyJob(runtime.OptimizationJob):
        def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
            super().__init__(args, fixed_params, opt_params_def, risk_params, shared_context=context)

        def run(self):
            return None

    monkeypatch.setattr(runtime, "OptimizationJob", EmptyJob)
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_: False)
    monkeypatch.setattr(EmptyJob, "_launch_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(runtime.sys, "argv", ["run.py", "--params", "{'contracts': 1}"])
    args = _args()
    assert runtime.run_optimizer_mode(args, ast.literal_eval(args.params), {}, ["AAA"]) == 0
    output = capsys.readouterr().out
    assert "Training metrics returned no results" in output
    assert OPTIMIZER_AI_ANALYSIS_START_MARKER not in output


def test_live_cli_keeps_date_inference_when_unused_optimizer_flags_are_present(monkeypatch):
    import run
    import live_trader.engine as live_engine

    captured = {}

    def infer(args):
        args.end_date = "20260923"

    monkeypatch.setattr(run.sys, "argv", ["run.py", "dummy_strategy", "--connect", "dummy:sim", "--opt_params", "{}"])
    monkeypatch.setattr(run, "is_live_worker_process", lambda: True)
    monkeypatch.setattr(run, "infer_omitted_backtest_window", infer)
    monkeypatch.setattr(run, "mark_live_worker_expected_exit", lambda *_: None)
    monkeypatch.setattr(live_engine, "launch_live", lambda *a, **k: captured.update(k))
    assert run._run_main() == 0
    assert captured["end_date"] == "20260923"


def test_richer_unversioned_study_beats_thin_versioned_fork(tmp_path):
    journal = tmp_path / "optuna_batch.log"
    old = _write_study(journal, "old", _args(metric="return"), "return", config_version=None)
    old.add_trial(optuna.trial.create_trial(value=1.0))
    old.add_trial(optuna.trial.create_trial(value=2.0))
    thin = _write_study(journal, "thin", _args(metric="return"), "return")
    thin.add_trial(optuna.trial.create_trial(value=3.0))
    thin.set_user_attr("_optimizer_data_snapshot", {"id": "a" * 32, "sha256": "b" * 64})

    plan = _plan(_args(metric="return", study_journal=str(journal)), tmp_path)
    named = _plan(_args(metric="return", study_journal=str(journal), study_name="thin"), tmp_path)

    assert plan["studies"]["return"] == "old"
    assert plan["reused_pre_config"] is True
    assert plan["fallback_snapshot"]["id"] == "a" * 32
    assert named["studies"]["return"] == "old"
    assert named["switched_to_richer"] is True
    assert "_optimizer_worker_config_version" not in old.user_attrs


def test_loaded_grid_study_does_not_revisit_finished_combinations(tmp_path):
    journal = tmp_path / "optuna_grid.log"
    seeded = _write_study(journal, "grid-study", _args(metric="return"), "return", config_version=None)
    sampler = optuna.samplers.GridSampler({"x": list(range(6))})
    study = optuna.load_study(study_name=seeded.study_name, storage=_storage(journal), sampler=sampler)

    def objective(trial):
        return float(trial.suggest_int("x", 0, 5))

    study.optimize(objective, n_trials=2)
    visited = {trial.params["x"] for trial in study.trials if trial.state == optuna.trial.TrialState.COMPLETE}
    assert len(visited) == 2
    del study, seeded

    resumed = optuna.load_study(
        study_name="grid-study", storage=_storage(journal), sampler=optuna.samplers.GridSampler({"x": list(range(6))}),
    )
    resumed.optimize(objective, n_trials=2)
    completed = [trial for trial in resumed.trials if trial.state == optuna.trial.TrialState.COMPLETE]
    assert len(completed) == 4
    assert len({trial.params["x"] for trial in completed}) == 4
    assert visited.issubset({trial.params["x"] for trial in completed})


def test_optimizer_loads_unversioned_study_and_skips_visited_grid(monkeypatch, tmp_path):
    evaluated = []
    frame = pd.DataFrame({"close": [1.0, 1.1, 1.2]}, index=pd.date_range("2024-01-01", periods=3))
    context = {
        "strategy_class": SimpleNamespace(option_universe=("PUT",)), "risk_control_classes": [],
        "data_manager": None, "target_symbols": ["AAA"], "raw_datas": {"AAA": frame},
        "train_datas": {"AAA": frame}, "test_datas": {}, "train_range": ("20240101", "20240103"),
        "test_range": (None, None),
    }

    class OfflineJob(runtime.OptimizationJob):
        def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
            super().__init__(args, fixed_params, opt_params_def, risk_params, shared_context=shared_context or context)

        def _evaluate_trial_params(self, params):
            evaluated.append(params["x"])
            return float(params["x"])

    journal = Path(config.DATA_PATH) / "optuna" / "optuna_grid.log"
    journal.parent.mkdir(parents=True)
    seeded = _write_study(journal, "grid-study", _args(metric="return"), "return", config_version=None)
    seeded_sampler = optuna.samplers.GridSampler({"x": list(range(6))})
    seeded_study = optuna.load_study(study_name="grid-study", storage=_storage(journal), sampler=seeded_sampler)
    seeded_study.optimize(lambda trial: float(trial.suggest_int("x", 0, 5)), n_trials=2)
    visited = {trial.params["x"] for trial in seeded_study.trials}
    del seeded_study, seeded

    monkeypatch.setattr(runtime, "OptimizationJob", OfflineJob)
    monkeypatch.setattr(runtime, "get_class_from_name", lambda *a, **k: context["strategy_class"])
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_: False)
    monkeypatch.setattr(OfflineJob, "_launch_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(OfflineJob, "_run_main_eval_backtest", lambda *a, **k: None)
    monkeypatch.setattr(OfflineJob, "_run_yearly_validation_backtests", lambda *a, **k: [])
    monkeypatch.setattr(runtime.sys, "argv", ["run.py"])
    args = _args(metric="return", n_trials=4, study_journal=str(journal))
    assert runtime.run_optimizer_mode(args, ast.literal_eval(args.params), {}, ["AAA"]) == 0
    assert visited.isdisjoint(evaluated)
    assert len(evaluated) == 2
    studies = read_study_metadata(journal)
    assert [info["name"] for info in studies] == ["grid-study"]
    loaded = optuna.load_study(study_name="grid-study", storage=_storage(journal))
    assert "_optimizer_worker_config_version" not in loaded.user_attrs
    assert loaded.user_attrs["_optimizer_reused_pre_config_version"] is True
    completed = [trial for trial in loaded.trials if trial.state == optuna.trial.TrialState.COMPLETE]
    assert len(completed) == 4
    assert len({trial.params["x"] for trial in completed}) == 4


def test_isolate_aligns_trial_id_with_console_number(tmp_path):
    path = tmp_path / "optuna_batch.log"
    rich = _write_study(path, "rich", _args())
    for value in (1.0, 2.0, 3.0):
        rich.add_trial(optuna.trial.create_trial(value=value))
    thin = _write_study(path, "thin", _args())
    thin.add_trial(optuna.trial.create_trial(value=9.0))

    aligned, moved = isolate_study_journal(path, "rich", path)

    assert aligned == str(path.resolve())
    assert moved == ("thin",)
    loaded = optuna.load_study(study_name="rich", storage=_storage(path))
    assert [trial.number for trial in loaded.trials] == [0, 1, 2]
    assert [trial._trial_id for trial in loaded.trials] == [0, 1, 2]
    before = path.read_bytes()
    again, moved_again = isolate_study_journal(path, "rich", path)
    assert again == str(path.resolve())
    assert moved_again == ()
    assert path.read_bytes() == before
    trial = loaded.ask()
    assert trial.number == trial._trial_id == 3
    siblings = [item for item in tmp_path.glob("optuna_*.log") if item.resolve() != path.resolve()]
    assert len(siblings) == 1
    moved_study = optuna.load_study(study_name="thin", storage=_storage(siblings[0]))
    assert moved_study.trials[0].value == 9.0
    assert moved_study.trials[0].number == moved_study.trials[0]._trial_id == 0


def test_isolate_renumbers_interleaved_trials_and_resume_finds_sibling(tmp_path):
    path = tmp_path / "optuna_batch.log"
    args = _args()
    first = _write_study(path, "return-study", args, "return")
    second = _write_study(path, "sharpe-study", args, "sharpe")
    first.add_trial(optuna.trial.create_trial(value=1.0))
    second.add_trial(optuna.trial.create_trial(value=8.0))
    first.add_trial(optuna.trial.create_trial(value=2.0))

    isolate_study_journal(path, "return-study", path)

    loaded = optuna.load_study(study_name="return-study", storage=_storage(path))
    assert [trial.value for trial in loaded.trials] == [1.0, 2.0]
    assert [trial.number for trial in loaded.trials] == [trial._trial_id for trial in loaded.trials] == [0, 1]
    plan = _plan(args, tmp_path)
    assert plan["studies"]["return"] == "return-study"
    assert plan["studies"]["sharpe"] == "sharpe-study"
    assert Path(plan["journal"]).resolve() == path.resolve()
