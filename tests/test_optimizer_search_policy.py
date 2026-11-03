import json
from types import SimpleNamespace

import numpy as np
import optuna
import pytest

from optimizer.reporting import print_run_summary
from optimizer.runtime import OptimizationJob, TrialEvaluationError
from optimizer.study_resume import AdaptiveGridSampler, BlockConstrainedTPESampler
from optimizer.search_policy import (
    EXHAUSTIVE_GRID_MAX_COMBINATIONS,
    GRID_SAMPLER_MAX_COMBINATIONS,
    _block_dimensions,
    _parameter_bucket,
    _t_crit_95,
    build_block_balanced_pilot,
    build_global_pilot,
    build_trial_plan,
    block_key,
    decode_block_keys,
    encode_block_keys,
    extend_history_record,
    finite_axis_size,
    finite_grid_size,
    finite_search_space_size,
    grid_block_ids,
    grid_sampler_is_safe,
    grid_signature,
    parameter_key,
    possible_block_keys,
    recommended_pilot_count,
    repair_params_to_active_blocks,
    restore_extend_history,
    should_extend,
    shrink_grid_blocks,
    shrink_param_blocks,
    spread_pilot_count,
    stabilize_block_keys,
    summarise_trials,
)


def test_large_finite_grid_avoids_grid_sampler_materialization():
    assert grid_sampler_is_safe(EXHAUSTIVE_GRID_MAX_COMBINATIONS)
    assert grid_sampler_is_safe(GRID_SAMPLER_MAX_COMBINATIONS)
    assert not grid_sampler_is_safe(GRID_SAMPLER_MAX_COMBINATIONS + 1)
    assert not grid_sampler_is_safe(386_736_039_909_273_600)


def test_oversized_int_axis_skips_grid_materialization():
    job = OptimizationJob.__new__(OptimizationJob)
    job.opt_params_def = {"x": {"type": "int", "low": 0, "high": 10**9}}

    assert job._build_grid_search_space() is None
    assert finite_axis_size(job.opt_params_def["x"]) == 10**9 + 1
    assert finite_search_space_size(job.opt_params_def) == 10**9 + 1
    assert not grid_sampler_is_safe(finite_search_space_size(job.opt_params_def))


def test_oversized_axis_product_skips_grid_materialization():
    job = OptimizationJob.__new__(OptimizationJob)
    job.opt_params_def = {
        "a": {"type": "int", "low": 0, "high": 499},
        "b": {"type": "int", "low": 0, "high": 499},
        "c": {"type": "int", "low": 0, "high": 499},
    }

    assert job._build_grid_search_space() is None
    assert finite_search_space_size(job.opt_params_def) == 500 ** 3


def test_global_pilot_samples_huge_int_axis_by_index():
    space = {"x": {"type": "int", "low": 0, "high": 10**9, "step": 1}}
    points = build_global_pilot(space, 16, seed=1)
    again = build_global_pilot(space, 16, seed=1)

    assert len(points) == 16
    values = [item["x"] for item in points]
    assert min(values) == 0
    assert max(values) == 10**9
    assert len(set(values)) == 16
    assert [item["x"] for item in again] == values


def test_auto_trial_budget_is_independent_of_cpu_count(monkeypatch):
    job = OptimizationJob.__new__(OptimizationJob)
    job.opt_params_def = {
        "lookback": {"type": "int", "low": 5, "high": 100, "step": 5},
        "weight": {"type": "float", "low": 0.0, "high": 1.0, "step": 0.1},
    }
    monkeypatch.setattr(
        OptimizationJob,
        "_get_total_cpu_cores",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("CPU must not affect trial budget"))),
    )

    assert job._estimate_n_trials() > 0


def test_small_finite_space_is_scheduled_exhaustively():
    plan = build_trial_plan(
        initial_budget=400,
        finite_size=1200,
        explicit=False,
    )

    assert plan["exhaustive"] is True
    assert plan["target"] == 1200
    assert plan["max_target"] == 1200
    assert EXHAUSTIVE_GRID_MAX_COMBINATIONS >= 1200


def test_large_finite_space_keeps_a_bounded_adaptive_budget():
    plan = build_trial_plan(
        initial_budget=5000,
        finite_size=1_000_000,
        explicit=False,
    )

    assert plan["adaptive"] is True
    assert plan["target"] == 5000
    assert plan["max_target"] == 20_000
    assert plan["batch"] <= 256


def test_global_pilot_is_deterministic_and_contains_boundaries():
    space = {
        "threshold": {"type": "float", "low": 0.1, "high": 0.9},
        "lookback": {"type": "int", "low": 5, "high": 25, "step": 5},
        "mode": {"type": "categorical", "choices": ["a", "b"]},
    }

    first = build_global_pilot(space, 16, seed=123)
    second = build_global_pilot(space, 16, seed=123)

    assert [parameter_key(item) for item in first] == [parameter_key(item) for item in second]
    thresholds = [item["threshold"] for item in first]
    assert min(thresholds) == 0.1
    assert max(thresholds) == 0.9
    assert {item["mode"] for item in first} == {"a", "b"}
    assert recommended_pilot_count(200, 6) == 60


def test_trial_summary_reports_unique_coverage_and_duplicates():
    study = optuna.create_study(direction="maximize")
    study.add_trial(
        optuna.trial.create_trial(
            value=1.0,
            params={"x": 1},
            distributions={"x": optuna.distributions.IntDistribution(0, 2)},
        )
    )
    study.add_trial(
        optuna.trial.create_trial(
            value=2.0,
            params={"x": 1},
            distributions={"x": optuna.distributions.IntDistribution(0, 2)},
        )
    )
    study.add_trial(
        optuna.trial.create_trial(
            value=3.0,
            params={"x": 2},
            distributions={"x": optuna.distributions.IntDistribution(0, 2)},
        )
    )

    summary = summarise_trials(
        study.trials,
        {"x": {"type": "int", "low": 0, "high": 2}},
        finite_size=3,
    )

    assert finite_grid_size({"x": [0, 1, 2]}) == 3
    assert summary["finished"] == 3
    assert summary["unique"] == 2
    assert summary["duplicates"] == 1
    assert summary["coverage"] == 2 / 3
    assert summary["best"] == 3.0


def test_adaptive_grid_sampler_keeps_global_grid_ids_unique():
    space = {"x": [0, 1, 2], "mode": ["a", "b"]}
    study = optuna.create_study(
        direction="maximize",
        sampler=AdaptiveGridSampler(space, active_grid_ids={0, 1, 2}, seed=0),
    )

    study.optimize(
        lambda trial: float(
            trial.suggest_int("x", 0, 2)
            + (trial.suggest_categorical("mode", ["a", "b"]) == "b")
        ),
        n_trials=3,
    )

    grid_ids = [trial.system_attrs["grid_id"] for trial in study.trials]
    assert len(grid_ids) == len(set(grid_ids)) == 3
    assert set(grid_ids).issubset({0, 1, 2})


def test_should_extend_stops_at_coverage_or_max():
    low = {"best": 1.0, "coverage": 0.2}
    covered = {"best": 1.0, "coverage": 0.95}
    unknown = {"best": 1.0, "coverage": None}

    assert should_extend(low, target=16, max_target=64) is True
    assert should_extend(unknown, target=48, max_target=64) is True
    assert should_extend(covered, target=16, max_target=64) is False
    assert should_extend(low, target=64, max_target=64) is False


def test_adaptive_grid_sampler_never_falls_back_to_abandoned_ids():
    space = {"x": list(range(20))}
    study = optuna.create_study(
        direction="maximize",
        sampler=AdaptiveGridSampler(space, active_grid_ids={0, 1, 2, 3, 4}, seed=0),
    )

    study.optimize(
        lambda trial: float(trial.suggest_int("x", 0, 19)),
        n_trials=8,
        n_jobs=8,
    )

    complete = [
        trial for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    grid_ids = [trial.system_attrs["grid_id"] for trial in complete]
    assert set(grid_ids).issubset({0, 1, 2, 3, 4})
    assert len(grid_ids) == len(set(grid_ids))
    assert all("grid_id" not in trial.system_attrs or trial.system_attrs["grid_id"] in {0, 1, 2, 3, 4}
               for trial in study.trials)


def test_exhausted_adaptive_grid_does_not_reassign_visited_ids():
    space = {"x": list(range(4))}
    study = optuna.create_study(
        direction="maximize",
        sampler=AdaptiveGridSampler(space, active_grid_ids={0, 1}, seed=0),
    )

    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 3)), n_trials=6)

    complete = [
        trial for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    grid_ids = [trial.system_attrs["grid_id"] for trial in complete]
    assert set(grid_ids) <= {0, 1}
    assert len(grid_ids) == len(set(grid_ids))
    assert len(complete) <= 2


def test_empty_active_grid_ids_do_not_reopen_the_full_grid():
    space = {"x": list(range(8))}
    study = optuna.create_study(
        direction="maximize",
        sampler=AdaptiveGridSampler(space, active_grid_ids=set(), seed=0),
    )

    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 7)), n_trials=3)

    assert all(trial.state != optuna.trial.TrialState.COMPLETE for trial in study.trials)


def test_grid_block_shrink_is_conservative_and_keeps_best_block():
    space = {"x": list(range(16))}
    sampler = optuna.samplers.GridSampler(space, seed=0)
    study = optuna.create_study(direction="maximize")
    search_space = {"x": list(range(16))}
    for _repeat in range(2):
        for grid_id, value in enumerate(sampler._all_grids):
            x_value = value[0]
            if x_value < 4:
                score = 0.0
            elif x_value >= 12:
                score = 10.0
            else:
                continue
            study.add_trial(
                optuna.trial.create_trial(
                    value=score,
                    params={"x": x_value},
                    distributions={"x": optuna.distributions.IntDistribution(0, 15)},
                    system_attrs={"grid_id": grid_id, "search_space": search_space},
                )
            )

    active, abandoned = shrink_grid_blocks(
        study,
        search_space,
        set(range(16)),
        seed=0,
        min_samples=4,
    )

    assert abandoned
    assert active
    assert block_key({"x": 15}, {"x": {"type": "int", "low": 0, "high": 15}}) not in set(abandoned)


def test_grid_block_shrink_keeps_equal_noise_blocks():
    space = {"x": list(range(16))}
    sampler = optuna.samplers.GridSampler(space, seed=0)
    study = optuna.create_study(direction="maximize")
    search_space = {"x": list(range(16))}
    for _repeat in range(2):
        for grid_id, value in enumerate(sampler._all_grids):
            x_value = value[0]
            if x_value < 4 or x_value >= 12:
                study.add_trial(
                    optuna.trial.create_trial(
                        value=1.0,
                        params={"x": x_value},
                        distributions={"x": optuna.distributions.IntDistribution(0, 15)},
                        system_attrs={"grid_id": grid_id, "search_space": search_space},
                    )
                )

    active, abandoned = shrink_grid_blocks(
        study,
        search_space,
        set(range(16)),
        seed=0,
        min_samples=4,
    )

    assert abandoned == []
    assert active == set(range(16))


def test_t_crit_95_uses_matching_degrees_not_bin_top():
    assert _t_crit_95(7) == pytest.approx(2.365)
    assert _t_crit_95(8) == pytest.approx(2.306)
    assert _t_crit_95(20) == pytest.approx(2.086)


def test_block_dimensions_use_discrete_levels_not_raw_range():
    space = {
        "lookback": {"type": "int", "low": 5, "high": 20, "step": 5},
        "threshold": {"type": "float", "low": 0.0, "high": 1.0, "step": 0.1},
        "profit_take": {"type": "float", "low": 0.4, "high": 0.6, "step": 0.1},
    }

    assert _block_dimensions(space) == ["threshold", "lookback"]
    low = block_key({"lookback": 5, "threshold": 0.0, "profit_take": 0.4}, space)
    high = block_key({"lookback": 20, "threshold": 1.0, "profit_take": 0.6}, space)
    assert len(low) == 2
    assert low != high


def test_grid_block_ids_keep_unit_interval_float_axes():
    grid = {
        "threshold": [round(index * 0.1, 12) for index in range(11)],
        "profit_take": [0.4, 0.5, 0.6],
    }

    mapping = grid_block_ids(grid, seed=0)

    assert len(set(mapping.values())) > 1
    assert all(len(key) == 2 for key in mapping.values())


def test_adaptive_grid_assigns_smallest_unvisited_after_shrink():
    space = {"x": list(range(10))}
    study = optuna.create_study(
        direction="maximize",
        sampler=AdaptiveGridSampler(space, active_grid_ids={3, 4, 7, 8}, seed=0),
    )

    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 9)), n_trials=4)

    complete = [
        trial for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    grid_ids = [trial.system_attrs["grid_id"] for trial in complete]
    assert grid_ids == [3, 4, 7, 8]


def test_adaptive_grid_picks_smallest_id_when_unvisited_order_is_unsorted():
    space = {"x": list(range(10))}
    sampler = AdaptiveGridSampler(space, active_grid_ids={3, 4, 7, 8}, seed=0)
    original = AdaptiveGridSampler._get_unvisited_grid_ids

    def reversed_unvisited(study):
        return list(reversed(original(sampler, study)))

    sampler._get_unvisited_grid_ids = reversed_unvisited
    study = optuna.create_study(direction="maximize", sampler=sampler)

    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 9)), n_trials=4)

    complete = [
        trial for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    grid_ids = [trial.system_attrs["grid_id"] for trial in complete]
    assert grid_ids == [3, 4, 7, 8]


def test_adaptive_grid_reroutes_stale_min_claim_to_next_id():
    space = {"x": list(range(10))}
    sampler = AdaptiveGridSampler(space, active_grid_ids={3, 4, 7, 8}, seed=0)
    study = optuna.create_study(direction="maximize", sampler=sampler)

    first = study.ask()
    assert study._storage.get_trial_system_attrs(first._trial_id)["grid_id"] == 3
    study.tell(first, 1.0)

    original = sampler._get_unvisited_grid_ids
    calls = {"n": 0}

    def stale_then_real(study_arg):
        if calls["n"] == 0:
            calls["n"] += 1
            return [3, 4, 7, 8]
        return original(study_arg)

    sampler._get_unvisited_grid_ids = stale_then_real
    second = study.ask()
    assert study._storage.get_trial_system_attrs(second._trial_id)["grid_id"] == 4
    study.tell(second, 1.0)


def test_adaptive_grid_parallel_min_claims_are_unique():
    space = {"x": list(range(10))}
    study = optuna.create_study(
        direction="maximize",
        sampler=AdaptiveGridSampler(space, active_grid_ids={3, 4, 7, 8}, seed=0),
    )

    study.optimize(lambda trial: float(trial.suggest_int("x", 0, 9)), n_trials=8, n_jobs=8)

    complete = [
        trial for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    grid_ids = [trial.system_attrs["grid_id"] for trial in complete]
    assert len(grid_ids) == len(set(grid_ids))
    assert set(grid_ids) <= {3, 4, 7, 8}
    assert len(complete) <= 4


def test_run_summary_prints_search_coverage(capsys):
    print_run_summary(
        final_reports=[
            {
                "metric_name": "return",
                "trials_completed": 4,
                "search_audit": {
                    "finished": 4,
                    "unique": 3,
                    "duplicates": 1,
                    "coverage": 0.3,
                    "target_trials": 10,
                    "max_target_trials": 40,
                },
            }
        ],
        total_metrics=1,
    )

    output = capsys.readouterr().out
    assert "Search coverage:" in output
    assert "unique=3" in output
    assert "coverage=30.00%" in output


def test_auto_budget_extends_when_global_pilot_fills_initial_budget():
    args = SimpleNamespace(
        n_jobs=1,
        n_trials=None,
        auto_launch_dashboard=False,
        shared_journal_log_file=None,
        study_name="search-policy-dynamic-test",
        metric="sharpe",
        strategy="dummy",
        cash=100000.0,
        commission=0.0,
        slippage=0.0,
        timeframe="Days",
        compression=1,
        study_journal=None,
    )
    job = OptimizationJob.__new__(OptimizationJob)
    job.args = args
    job.fixed_params = {}
    job.opt_params_def = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
    job.risk_params = {}
    job._indicator_cache = {}
    job.test_datas = {}
    job.test_range = (None, None)
    job.strategy_class = SimpleNamespace(option_universe=None)
    job._requested_metrics = ["sharpe"]
    job._future_metric_finished_counts = ()
    job._total_finished_before = 0
    job._estimate_n_trials = lambda: 4
    job.objective = lambda trial: float(trial.number)
    job._run_main_eval_backtest = lambda params: None
    job._run_test_set_backtest = lambda params, verbose=False: None
    job._run_yearly_validation_backtests = lambda params: []

    result = job.run()

    assert result["search_audit"]["finished"] == 16
    assert result["search_audit"]["target_trials"] == 16
    assert result["search_audit"]["max_target_trials"] == 16


def test_tpe_batch_without_finished_trials_stops_metric():
    args = SimpleNamespace(
        n_jobs=1,
        n_trials=8,
        auto_launch_dashboard=False,
        shared_journal_log_file=None,
        study_name="search-policy-fail-stop",
        metric="sharpe",
        strategy="dummy",
        cash=100000.0,
        commission=0.0,
        slippage=0.0,
        timeframe="Days",
        compression=1,
        study_journal=None,
    )
    job = OptimizationJob.__new__(OptimizationJob)
    job.args = args
    job.fixed_params = {}
    job.opt_params_def = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
    job.risk_params = {}
    job._indicator_cache = {}
    job.test_datas = {}
    job.test_range = (None, None)
    job.strategy_class = SimpleNamespace(option_universe=None)
    job._requested_metrics = ["sharpe"]
    job._future_metric_finished_counts = ()
    job._total_finished_before = 0
    calls = {"n": 0}

    def failing_objective(trial):
        calls["n"] += 1
        trial.suggest_float("x", 0.0, 1.0)
        raise TrialEvaluationError("plugin boom")

    job.objective = failing_objective
    job._run_main_eval_backtest = lambda params: None
    job._run_test_set_backtest = lambda params, verbose=False: None
    job._run_yearly_validation_backtests = lambda params: []

    result = job.run()

    assert result is None
    assert 8 < calls["n"] <= 16


def test_explicit_budget_also_starts_with_block_spread():
    class FakeStudy:
        def __init__(self):
            self.trials = []
            self.queued = []
            self.attrs = {}

        def enqueue_trial(self, params, **kwargs):
            self.queued.append(dict(params))

        def set_user_attr(self, key, value):
            self.attrs[key] = value

    job = OptimizationJob.__new__(OptimizationJob)
    job.opt_params_def = {"threshold": {"type": "float", "low": 0.0, "high": 1.0}}
    study = FakeStudy()

    inserted = job._enqueue_block_spread(study, target_trials=8, sampler_seed=17)

    assert inserted == 8
    assert len(study.queued) == 8
    thresholds = [item["threshold"] for item in study.queued]
    assert min(thresholds) == 0.0
    assert max(thresholds) == 1.0


def test_categorical_buckets_split_choices_evenly():
    config = {"type": "categorical", "choices": list("abcdefghij")}
    assert _parameter_bucket("a", config) == 0
    assert _parameter_bucket("d", config) == 1
    assert _parameter_bucket("j", config) == 3


@pytest.mark.parametrize(
    "space",
    [
        {"x": {"type": "int", "low": 5, "high": 1}},
        {"x": {"type": "float", "low": 0.0, "high": 1.0, "step": 0.0}},
        {"x": {"type": "categorical", "choices": []}},
    ],
)
def test_invalid_finite_search_space_fails_with_value_error(space):
    job = OptimizationJob.__new__(OptimizationJob)
    job.opt_params_def = space

    with pytest.raises(ValueError):
        job._build_grid_search_space()


def test_extend_history_round_trips_through_journal_records():
    record = extend_history_record({"best": 1.25, "coverage": 0.4, "finished": 32, "unique": 30}, 32)
    restored = restore_extend_history([record, {"target": "bad"}, "skip", {"target": 40, "best": 1.25}])

    assert restored[0]["target"] == 32
    assert restored[0]["best"] == pytest.approx(1.25)
    assert restored[1]["target"] == 40
    assert should_extend(restored[-1], 40, 64) is True
    restored.append(extend_history_record({"best": 1.25, "coverage": 0.41, "finished": 48}, 48))
    assert should_extend(restored[-1], 64, 64) is False


def _search_job(args, space, objective):
    job = OptimizationJob.__new__(OptimizationJob)
    job.args = args
    job.fixed_params = {}
    job.opt_params_def = space
    job.risk_params = {}
    job._indicator_cache = {}
    job.test_datas = {}
    job.test_range = (None, None)
    job.strategy_class = SimpleNamespace(option_universe=None)
    job._requested_metrics = [args.metric]
    job._future_metric_finished_counts = ()
    job._total_finished_before = 0
    job._resume_exclusive = True
    job.objective = objective
    job._run_main_eval_backtest = lambda params: None
    job._run_test_set_backtest = lambda params, verbose=False: None
    job._run_yearly_validation_backtests = lambda params: []
    return job


def test_auto_budget_resume_continues_to_max_without_percent_stop(tmp_path):
    from optuna.storages import JournalStorage
    from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock

    path = str(tmp_path / "continue-max.log")
    storage = JournalStorage(JournalFileBackend(path, lock_obj=JournalFileOpenLock(path)))
    study = optuna.create_study(
        study_name="continue-max", direction="maximize", storage=storage,
    )
    study.set_user_attr("_optimizer_search_policy", "block_spread_tpe")
    study.set_user_attr("_optimizer_initial_trials", 16)
    study.set_user_attr("_optimizer_target_trials", 48)
    study.set_user_attr("_optimizer_max_trials", 64)
    for index in range(48):
        study.add_trial(optuna.trial.create_trial(
            value=1.001,
            params={"x": index / 48.0},
            distributions={"x": optuna.distributions.FloatDistribution(0.0, 1.0)},
        ))

    calls = {"n": 0}

    def objective(trial):
        calls["n"] += 1
        return float(trial.suggest_float("x", 0.0, 1.0))

    args = SimpleNamespace(
        n_jobs=1, n_trials=None, auto_launch_dashboard=False,
        shared_journal_log_file=path, study_name="continue-max",
        metric="sharpe", strategy="dummy", cash=100000.0, commission=0.0,
        slippage=0.0, timeframe="Days", compression=1, study_journal=None,
    )
    job = _search_job(args, {"x": {"type": "float", "low": 0.0, "high": 1.0}}, objective)
    job._estimate_n_trials = lambda: 16
    result = job.run()

    assert calls["n"] == 16
    assert result["search_audit"]["finished"] == 64
    assert result["search_audit"]["target_trials"] == 64
    loaded = optuna.load_study(study_name="continue-max", storage=storage)
    assert loaded.user_attrs["_optimizer_target_trials"] == 64


def test_single_process_resume_keeps_shrunk_active_grid_ids(tmp_path):
    from optuna.storages import JournalStorage
    from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock

    path = str(tmp_path / "shrunk.log")
    storage = JournalStorage(JournalFileBackend(path, lock_obj=JournalFileOpenLock(path)))
    space = {"x": list(range(8))}
    study = optuna.create_study(
        study_name="shrunk-grid", direction="maximize", storage=storage,
        sampler=AdaptiveGridSampler(space, active_grid_ids={0, 1}, seed=0),
    )
    study.set_user_attr("_optimizer_search_policy", "adaptive_grid")
    study.set_user_attr("_optimizer_grid_signature", grid_signature(space))
    study.set_user_attr("_optimizer_active_grid_ids", [0, 1])

    def objective(trial):
        return float(trial.suggest_int("x", 0, 7))

    args = SimpleNamespace(
        n_jobs=1, n_trials=4, auto_launch_dashboard=False,
        shared_journal_log_file=path, study_name="shrunk-grid",
        metric="sharpe", strategy="dummy", cash=100000.0, commission=0.0,
        slippage=0.0, timeframe="Days", compression=1, study_journal=None,
    )
    job = _search_job(args, {"x": {"type": "int", "low": 0, "high": 7}}, objective)
    job.run()
    loaded = optuna.load_study(study_name="shrunk-grid", storage=storage)
    complete = [
        trial for trial in loaded.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    grid_ids = [trial.system_attrs.get("grid_id") for trial in complete]
    assert set(grid_ids) <= {0, 1}
    assert len(complete) == 2


def test_spread_pilot_count_covers_blocks_before_tpe():
    assert spread_pilot_count(4695, 15, 16) == 256
    assert spread_pilot_count(8, 1, 4) == 8
    assert spread_pilot_count(20, 1, 4) == 20


def test_block_balanced_pilot_covers_all_blocks_without_materializing_huge_axis():
    space = {
        "x": {"type": "int", "low": 0, "high": 10**9, "step": 1},
        "y": {"type": "float", "low": 0.0, "high": 1.0, "step": 0.1},
    }
    keys = possible_block_keys(space)
    points = build_block_balanced_pilot(space, 32, seed=3)
    covered = {block_key(item, space) for item in points}

    assert len(keys) == 16
    assert covered == set(keys)
    assert min(item["x"] for item in points) == 0
    assert max(item["x"] for item in points) == 10**9
    assert encode_block_keys(keys) == encode_block_keys(decode_block_keys(encode_block_keys(keys)))


def test_repair_params_rotates_active_blocks_by_trial_number():
    space = {
        "x": {"type": "float", "low": 0.0, "high": 1.0},
        "y": {"type": "float", "low": 0.0, "high": 1.0},
    }
    active = [(("x", 0), ("y", 0)), (("x", 3), ("y", 3))]
    first = repair_params_to_active_blocks(
        {"x": 0.9, "y": 0.9}, space, active, rng=np.random.default_rng(1), trial_number=0,
    )
    second = repair_params_to_active_blocks(
        {"x": 0.05, "y": 0.05}, space, active, rng=np.random.default_rng(2), trial_number=1,
    )

    assert block_key(first, space) == (("x", 0), ("y", 0))
    assert block_key(second, space) == (("x", 3), ("y", 3))
    assert first["x"] < 0.25
    assert second["x"] >= 0.75


def test_repair_params_spreads_inside_target_block():
    space = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
    active = [(("x", 0),)]
    points = [
        repair_params_to_active_blocks(
            {"x": 0.9}, space, active, rng=np.random.default_rng(seed), trial_number=0,
        )["x"]
        for seed in range(24)
    ]

    assert all(0.0 <= value < 0.25 for value in points)
    assert len({round(value, 8) for value in points}) > 1


def test_repair_small_discrete_stays_in_target_block():
    space = {"x": {"type": "int", "low": 0, "high": 4, "step": 1}}
    active = [(("x", 1),)]
    points = [
        repair_params_to_active_blocks(
            {"x": 4}, space, active, rng=np.random.default_rng(seed), trial_number=0,
        )
        for seed in range(80)
    ]

    assert all(block_key(item, space) == (("x", 1),) for item in points)
    assert {item["x"] for item in points} == {1}


def test_repair_float_step_stays_in_target_block():
    space = {"x": {"type": "float", "low": 0.0, "high": 1.0, "step": 0.1}}
    active = [(("x", 1),)]
    points = [
        repair_params_to_active_blocks(
            {"x": 0.9}, space, active, rng=np.random.default_rng(seed), trial_number=0,
        )
        for seed in range(80)
    ]

    assert all(block_key(item, space) == (("x", 1),) for item in points)
    assert {item["x"] for item in points} <= {0.3, 0.4}


def test_block_balanced_pilot_small_int_stays_in_assigned_blocks():
    space = {
        "lookback": {"type": "int", "low": 5, "high": 25, "step": 5},
        "weight": {"type": "float", "low": 0.0, "high": 1.0},
    }
    points = build_block_balanced_pilot(space, 32, seed=7)

    assert points
    assert all(block_key(item, space) in set(possible_block_keys(space)) for item in points)
    assert {block_key(item, space) for item in points} == set(possible_block_keys(space))


def test_block_balanced_pilot_reuses_other_axes_when_unique_lhc_is_short():
    space = {
        "mode": {"type": "categorical", "choices": ["a", "b"]},
        "side": {"type": "categorical", "choices": ["left", "right"]},
    }
    points = build_block_balanced_pilot(space, 12, seed=4)

    assert points
    assert all("mode" in item and "side" in item for item in points)


def test_shrink_param_blocks_keeps_best_and_abandons_separated_blocks():
    space = {"x": {"type": "float", "low": 0.0, "high": 1.0, "step": 0.1}}
    study = optuna.create_study(direction="maximize")
    for value, score in [(0.0, 0.0), (0.05, 0.1), (0.1, 0.0), (0.15, 0.05),
                         (0.2, 0.1), (0.25, 0.0), (0.3, 0.05), (0.35, 0.1),
                         (0.9, 10.0), (0.92, 9.8), (0.94, 10.2), (0.96, 9.9),
                         (0.98, 10.1), (1.0, 9.7), (0.91, 10.0), (0.93, 9.6)]:
        study.add_trial(optuna.trial.create_trial(
            value=score,
            params={"x": value},
            distributions={"x": optuna.distributions.FloatDistribution(0.0, 1.0, step=0.01)},
        ))

    remaining, abandoned = shrink_param_blocks(study, space, min_samples=4)
    remaining_buckets = {key[0][1] for key in remaining if key}
    abandoned_buckets = {key[0][1] for key in abandoned if key}

    assert 3 in remaining_buckets
    assert 0 in abandoned_buckets
    assert 0 not in remaining_buckets


def test_active_block_order_is_stable_across_set_iteration():
    shuffled = [(("x", 3), ("y", 1)), (("x", 0), ("y", 0)), (("x", 2), ("y", 2))]
    reversed_keys = list(reversed(shuffled))
    as_set = set(shuffled)

    assert stabilize_block_keys(shuffled) == stabilize_block_keys(reversed_keys)
    assert stabilize_block_keys(shuffled) == stabilize_block_keys(as_set)
    assert stabilize_block_keys(shuffled)[0] == (("x", 0), ("y", 0))


def test_shrink_param_blocks_returns_stable_remaining_order():
    space = {"x": {"type": "float", "low": 0.0, "high": 1.0, "step": 0.1}}
    study = optuna.create_study(direction="maximize")
    remaining, _abandoned = shrink_param_blocks(study, space, min_samples=4)

    assert remaining == stabilize_block_keys(possible_block_keys(space))


def test_block_constrained_tpe_sorts_active_blocks():
    space = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
    shuffled = [(("x", 3),), (("x", 0),), (("x", 2),), (("x", 1),)]
    sampler = BlockConstrainedTPESampler(
        seed=1, n_startup_trials=4, opt_params_def=space, active_blocks=shuffled,
    )

    assert sampler._active_blocks == tuple(stabilize_block_keys(shuffled))
    sampler.set_active_blocks(list(reversed(shuffled)))
    assert sampler._active_blocks == tuple(stabilize_block_keys(shuffled))


def test_block_constrained_tpe_repairs_into_active_block():
    space = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
    sampler = BlockConstrainedTPESampler(
        seed=1, n_startup_trials=4, opt_params_def=space,
        active_blocks=[(("x", 0),)],
    )
    study = optuna.create_study(direction="maximize", sampler=sampler)
    for index in range(4):
        study.add_trial(optuna.trial.create_trial(
            value=float(index),
            params={"x": 0.05 * index},
            distributions={"x": optuna.distributions.FloatDistribution(0.0, 1.0)},
        ))
    study.optimize(lambda trial: float(trial.suggest_float("x", 0.0, 1.0)), n_trials=3)
    new_trials = study.trials[4:]
    assert new_trials
    assert all(block_key(trial.params, space) == (("x", 0),) for trial in new_trials)


def test_block_constrained_tpe_constant_liar_stores_repaired_params():
    space = {"x": {"type": "float", "low": 0.0, "high": 1.0}}
    sampler = BlockConstrainedTPESampler(
        seed=1, n_startup_trials=4, constant_liar=True, opt_params_def=space,
        active_blocks=[(("x", 0),)],
    )
    study = optuna.create_study(direction="maximize", sampler=sampler)
    for index in range(4):
        study.add_trial(optuna.trial.create_trial(
            value=float(index),
            params={"x": 0.05 * index},
            distributions={"x": optuna.distributions.FloatDistribution(0.0, 1.0)},
        ))
    trial = study.ask()
    value = trial.suggest_float("x", 0.0, 1.0)
    stored = json.loads(
        study._storage.get_trial_system_attrs(trial._trial_id).get("tpe:relative_params:0") or "{}"
    )

    assert block_key({"x": value}, space) == (("x", 0),)
    assert stored.get("x") == value
    study.tell(trial, value)


def test_constant_score_runs_to_max_budget(tmp_path):
    from optuna.storages import JournalStorage
    from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock

    path = str(tmp_path / "to-max.log")
    calls = {"n": 0}

    def objective(trial):
        calls["n"] += 1
        trial.suggest_float("x", 0.0, 1.0)
        return 1.0

    args = SimpleNamespace(
        n_jobs=1, n_trials=None, auto_launch_dashboard=False,
        shared_journal_log_file=path, study_name="to-max",
        metric="sharpe", strategy="dummy", cash=100000.0, commission=0.0,
        slippage=0.0, timeframe="Days", compression=1, study_journal=None,
    )
    job = _search_job(args, {"x": {"type": "float", "low": 0.0, "high": 1.0}}, objective)
    job._estimate_n_trials = lambda: 16
    result = job.run()

    assert result["search_audit"]["finished"] == 64
    assert result["search_audit"]["target_trials"] == 64
    assert result["search_audit"]["max_target_trials"] == 64
    assert calls["n"] == 64
