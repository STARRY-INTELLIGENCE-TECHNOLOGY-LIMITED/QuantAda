"""失败试验重试必须幂等、保留审计记录，并保持网格覆盖与有效预算。"""

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from types import SimpleNamespace

import optuna
from optuna.storages import JournalStorage
from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock
import pandas as pd
import pytest

from optimizer.runtime import OptimizationJob, _optimize_worker_entry
from optimizer.study_resume import RetryAwareGridSampler, prepare_trial_resume
from strategies.base_strategy import BaseStrategy


class EmptyStrategy(BaseStrategy):
    """只验证真实 worker 的试验调度，不产生交易。"""

    option_universe = ("PUT",)

    def init(self):
        pass

    def next(self):
        pass


def _study(tmp_path, size=4):
    path = str(tmp_path / "journal.log")
    storage = JournalStorage(JournalFileBackend(path, lock_obj=JournalFileOpenLock(path)))
    study = optuna.create_study(
        study_name="retry-test", direction="maximize", storage=storage,
        sampler=optuna.samplers.GridSampler({"x": list(range(size))}),
    )
    study.set_user_attr("_optimizer_worker_config_version", 1)
    return study, storage, path


def test_keyboard_interrupt_retry_preserves_failure_and_negative_completed_score(tmp_path):
    study, storage, _ = _study(tmp_path, size=3)

    def interrupted(trial):
        trial.suggest_int("x", 0, 2)
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        study.optimize(interrupted, n_trials=1)
    study.optimize(lambda trial: trial.suggest_int("x", 0, 2) * 0 - 43.0, n_trials=1)
    failed, completed = study.trials
    assert OptimizationJob._remaining_trial_budget(study, 3) == 2

    assert prepare_trial_resume(study, storage) == (0, 1)
    assert prepare_trial_resume(study, storage) == (0, 0)
    retry = study.trials[2]
    assert retry.state == optuna.trial.TrialState.WAITING
    assert retry.params == failed.params
    assert retry.distributions == failed.distributions
    assert retry.system_attrs["grid_id"] == failed.system_attrs["grid_id"]
    assert retry.system_attrs["failed_trial"] == failed.number
    assert retry.system_attrs["retry_history"] == [failed.number]
    assert study.trials[0] == failed
    assert study.trials[1] == completed

    study.sampler = RetryAwareGridSampler({"x": list(range(3))})
    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 2)), n_trials=2)
    assert study.trials[1].value == -43.0
    successes = [trial for trial in study.trials if trial.state == optuna.trial.TrialState.COMPLETE]
    assert {trial.params["x"] for trial in successes} == {0, 1, 2}
    assert len(study.trials) == 4
    assert OptimizationJob._remaining_trial_budget(study, 3) == 0
    assert prepare_trial_resume(study, storage) == (0, 0)


def test_all_failed_grid_is_fully_retried_without_early_sampler_stop(tmp_path):
    study, storage, _ = _study(tmp_path, size=3)

    def interrupted(trial):
        trial.suggest_int("x", 0, 2)
        raise KeyboardInterrupt()

    for _ in range(3):
        with pytest.raises(KeyboardInterrupt):
            study.optimize(interrupted, n_trials=1)
    assert prepare_trial_resume(study, storage) == (0, 3)
    study.sampler = RetryAwareGridSampler({"x": list(range(3))})
    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 2)), n_trials=3)
    successes = [trial for trial in study.trials if trial.state == optuna.trial.TrialState.COMPLETE]
    assert len(successes) == 3
    assert {trial.params["x"] for trial in successes} == {0, 1, 2}
    assert all(trial.state == optuna.trial.TrialState.FAIL for trial in study.trials[:3])


def test_failed_retry_chain_queues_only_its_leaf_and_recovers_running_retry(tmp_path):
    study, storage, _ = _study(tmp_path, size=3)
    original = study.ask()
    original.suggest_int("x", 0, 2)
    study.tell(original, state=optuna.trial.TrialState.FAIL)
    assert prepare_trial_resume(study, storage) == (0, 1)
    study.sampler = RetryAwareGridSampler({"x": list(range(3))})
    retry = study.ask()
    study.tell(retry, state=optuna.trial.TrialState.FAIL)
    assert prepare_trial_resume(study, storage) == (0, 1)
    assert prepare_trial_resume(study, storage) == (0, 0)
    retry = study.ask()
    assert retry.number == 2
    assert prepare_trial_resume(study, storage) == (1, 0)
    assert len(study.trials) == 3
    assert study.trials[2].system_attrs["retry_history"] == [0, 1]
    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 2)), n_trials=3)
    assert len(study.trials) == 5
    assert OptimizationJob._remaining_trial_budget(study, 3) == 0
    assert prepare_trial_resume(study, storage) == (0, 0)


def test_failure_before_sampling_recovers_original_grid_slot(tmp_path):
    study, storage, _ = _study(tmp_path, size=3)
    study.add_trial(optuna.trial.create_trial(state=optuna.trial.TrialState.FAIL))
    assert prepare_trial_resume(study, storage) == (0, 1)
    study.sampler = RetryAwareGridSampler({"x": list(range(3))})
    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 2)), n_trials=3)
    successes = [trial for trial in study.trials if trial.state == optuna.trial.TrialState.COMPLETE]
    assert {trial.params["x"] for trial in successes} == {0, 1, 2}
    assert {trial.system_attrs["grid_id"] for trial in successes} == {0, 1, 2}


def test_continuous_retry_keeps_sampled_values_without_inheriting_intermediate_scores(tmp_path):
    study, storage, _ = _study(tmp_path)
    study.sampler = optuna.samplers.RandomSampler(seed=7)
    original = study.ask()
    original.suggest_float("threshold", 0.01, 0.99)
    original.report(123.0, step=10)
    study.tell(original, state=optuna.trial.TrialState.FAIL)
    assert prepare_trial_resume(study, storage) == (0, 1)
    retry = study.trials[-1]
    assert retry.params == study.trials[0].params
    assert retry.intermediate_values == {}
    study.optimize(lambda trial: trial.suggest_float("threshold", 0.01, 0.99), n_trials=1)
    assert study.trials[-1].value == study.trials[0].params["threshold"]
    assert prepare_trial_resume(study, storage) == (0, 0)


def test_pruned_trials_count_towards_budget_and_are_not_retried(tmp_path):
    study, storage, _ = _study(tmp_path)
    study.add_trial(optuna.trial.create_trial(state=optuna.trial.TrialState.PRUNED))
    study.add_trial(optuna.trial.create_trial(state=optuna.trial.TrialState.COMPLETE, value=-43.0))
    study.add_trial(optuna.trial.create_trial(state=optuna.trial.TrialState.FAIL))
    assert OptimizationJob._remaining_trial_budget(study, 4) == 2
    assert prepare_trial_resume(study, storage) == (0, 1)
    assert [trial.state for trial in study.trials[:2]] == [
        optuna.trial.TrialState.PRUNED, optuna.trial.TrialState.COMPLETE,
    ]


def test_retry_journal_survives_reload_and_does_not_enqueue_again(tmp_path):
    study, storage, path = _study(tmp_path)
    study.add_trial(optuna.trial.create_trial(state=optuna.trial.TrialState.FAIL))
    assert prepare_trial_resume(study, storage) == (0, 1)
    restored = JournalStorage(JournalFileBackend(path, lock_obj=JournalFileOpenLock(path)))
    loaded = optuna.load_study(study_name=study.study_name, storage=restored)
    assert prepare_trial_resume(loaded, restored) == (0, 0)
    assert len(loaded.trials) == 2


def test_spawn_workers_retry_failures_and_cover_every_grid_once(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    monkeypatch.delenv("QUANTADA_OPTIMIZER_TERMINAL_LOG", raising=False)
    study, storage, path = _study(tmp_path, size=8)

    def initial(trial):
        value = trial.suggest_int("x", 0, 7)
        if trial.number in (0, 2):
            raise KeyboardInterrupt()
        return float(value)

    for number in range(3):
        if number in (0, 2):
            with pytest.raises(KeyboardInterrupt):
                study.optimize(initial, n_trials=1)
        else:
            study.optimize(initial, n_trials=1)
    assert prepare_trial_resume(study, storage) == (0, 2)
    payload = {
        "args": SimpleNamespace(
            strategy=f"{__name__}.EmptyStrategy", risk=None, metric="return",
            cash=100000.0, commission=0.0, slippage=0.0, timeframe="Days", compression=1,
        ),
        "fixed_params": {}, "opt_params_def": {"x": {"type": "int", "low": 0, "high": 7}},
        "risk_params": {}, "train_range": ("20240101", "20240103"), "runtime_config": {"LOG": False},
        "train_datas": {"AAA": pd.DataFrame(
            {name: [100.0, 101.0, 102.0] for name in ("open", "high", "low", "close", "volume")},
            index=pd.date_range("2024-01-01", periods=3),
        )},
    }
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(
            _optimize_worker_entry, payload, study.study_name, path, quota, index, index,
            grid_search_space={"x": list(range(8))},
        ) for index, quota in enumerate((4, 3), start=1)]
        assert all(future.result(timeout=60)["stopped_early"] is False for future in futures)
    trials = study.get_trials()
    successes = [trial for trial in trials if trial.state == optuna.trial.TrialState.COMPLETE]
    assert len(trials) == 10
    assert len(successes) == 8
    assert {trial.params["x"] for trial in successes} == set(range(8))
    assert {trial.system_attrs["grid_id"] for trial in successes} == set(range(8))
    assert OptimizationJob._remaining_trial_budget(study, 8) == 0
    assert prepare_trial_resume(study, storage) == (0, 0)


def _resume_job(path, study_name, objective, n_jobs=1):
    """构造只跑调度的 Job，避免续传预算测试进入真实回测。"""
    job = OptimizationJob.__new__(OptimizationJob)
    job.args = SimpleNamespace(
        n_jobs=n_jobs, n_trials=3, auto_launch_dashboard=False, shared_journal_log_file=str(path),
        study_name=study_name, strategy=f"{__name__}.EmptyStrategy", metric="return",
        cash=100000.0, commission=0.0, slippage=0.0, timeframe="Days", compression=1,
        start_date="20240101", end_date="20240103",
    )
    job.strategy_class = EmptyStrategy
    job.opt_params_def = {"x": {"type": "int", "low": 0, "high": 5}}
    job.fixed_params = {}
    job.test_datas = {}
    job._resume_exclusive = True
    job.objective = objective
    job._run_main_eval_backtest = lambda params: None
    job._run_test_set_backtest = lambda params, verbose=False: None
    job._run_yearly_validation_backtests = lambda params: []
    return job


def _seed_completion_gap(tmp_path):
    """1 个完成和 3 个确定性失败；目标预算 3，剩余额度小于失败数。"""
    study, storage, path = _study(tmp_path, size=6)
    study.set_user_attr("_optimizer_worker_config_version", 1)

    def fail_once(trial):
        trial.suggest_int("x", 0, 5)
        raise KeyboardInterrupt()

    for _ in range(3):
        with pytest.raises(KeyboardInterrupt):
            study.optimize(fail_once, n_trials=1)
    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 5)), n_trials=1)
    failed = {trial.params["x"] for trial in study.trials if trial.state == optuna.trial.TrialState.FAIL}
    assert len(failed) == 3
    assert OptimizationJob._remaining_trial_budget(study, 3) == 2
    return path, study.study_name, failed, storage


def test_failed_retries_do_not_consume_completion_gap(tmp_path):
    path, study_name, failed, storage = _seed_completion_gap(tmp_path)
    seen = []

    def objective(trial):
        value = trial.suggest_int("x", 0, 5)
        seen.append(value)
        if value in failed:
            raise RuntimeError("deterministic failure")
        return float(value)

    result = _resume_job(path, study_name, objective).run()
    loaded = optuna.load_study(study_name=study_name, storage=storage)
    retries = [trial for trial in loaded.trials if "failed_trial" in trial.system_attrs]
    completes = [trial for trial in loaded.trials if trial.state == optuna.trial.TrialState.COMPLETE]
    assert result["trials_completed"] == 3
    assert len(retries) == 3
    assert {trial.params["x"] for trial in retries} == failed
    assert all(trial.state == optuna.trial.TrialState.FAIL for trial in retries)
    assert not any(trial.state == optuna.trial.TrialState.WAITING for trial in loaded.trials)
    assert {trial.params["x"] for trial in completes}.isdisjoint(failed)
    assert len(completes) == 3
    assert sum(value in failed for value in seen) == 3
    assert sum(value not in failed for value in seen) == 2
    assert OptimizationJob._remaining_trial_budget(loaded, 3) == 0

    seen.clear()
    _resume_job(path, study_name, objective).run()
    loaded = optuna.load_study(study_name=study_name, storage=storage)
    assert seen == []
    assert sum(trial.state == optuna.trial.TrialState.WAITING for trial in loaded.trials) == 3
    assert sum(trial.state == optuna.trial.TrialState.COMPLETE for trial in loaded.trials) == 3


def test_multiprocess_resume_drains_waiting_before_splitting_new_trials(monkeypatch, tmp_path):
    path, study_name, failed, storage = _seed_completion_gap(tmp_path)
    seen = []
    captured = {}

    def objective(trial):
        value = trial.suggest_int("x", 0, 5)
        seen.append(value)
        if value in failed:
            raise RuntimeError("deterministic failure")
        return float(value)

    def fake_parallel(self, n_jobs, n_trials, log_file, **kwargs):
        current = optuna.load_study(study_name=study_name, storage=storage)
        captured["n_trials"] = n_trials
        captured["waiting"] = sum(trial.state == optuna.trial.TrialState.WAITING for trial in current.trials)
        captured["retries"] = sum("failed_trial" in trial.system_attrs for trial in current.trials)
        progress = kwargs.get("trial_progress")
        captured["finished_before"] = (
            progress["finished_before"] if progress is not None else kwargs.get("progress_finished_before")
        )
        resumed = optuna.load_study(
            study_name=study_name, storage=storage, sampler=RetryAwareGridSampler({"x": list(range(6))}),
        )
        resumed.optimize(objective, n_trials=n_trials)

    monkeypatch.setattr(
        OptimizationJob, "_resolve_worker_count", classmethod(lambda cls, requested: max(1, int(requested))),
    )
    monkeypatch.setattr(OptimizationJob, "_run_multiprocess_optimization", fake_parallel)
    result = _resume_job(path, study_name, objective, n_jobs=2).run()
    loaded = optuna.load_study(study_name=study_name, storage=storage)
    assert captured["waiting"] == 0
    assert captured["retries"] == 3
    assert captured["n_trials"] == 2
    assert captured["finished_before"] == 1
    assert result["trials_completed"] == 3
    assert sum(value in failed for value in seen) == 3
    assert sum(trial.state == optuna.trial.TrialState.COMPLETE for trial in loaded.trials) == 3
    assert OptimizationJob._remaining_trial_budget(loaded, 3) == 0
