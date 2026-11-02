import optuna
import pytest
from types import SimpleNamespace

from optimizer.reporting import print_run_summary
from optimizer.runtime import OptimizationJob
from optimizer.search_policy import (
    EXHAUSTIVE_GRID_MAX_COMBINATIONS,
    build_global_pilot,
    build_trial_plan,
    block_key,
    finite_grid_size,
    parameter_key,
    should_extend,
    shrink_grid_blocks,
    summarise_trials,
)
from optimizer.study_resume import AdaptiveGridSampler


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
    assert first[0]["threshold"] == 0.1
    assert first[-1]["threshold"] == 0.9
    assert {item["mode"] for item in first} == {"a", "b"}


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


def test_should_extend_stops_when_the_latest_batch_does_not_improve():
    first = {"best": 1.0, "coverage": 0.2}
    second = {"best": 1.001, "coverage": 0.3}
    improved = {"best": 1.02, "coverage": 0.4}

    assert should_extend([first], first, target=16, max_target=64) is True
    assert should_extend([first, second], second, target=32, max_target=64) is False
    assert should_extend([first, improved], improved, target=32, max_target=64) is True
    assert should_extend([first, {"best": None}], {"best": None}, target=32, max_target=64) is False
    assert should_extend([first], first, target=64, max_target=64) is False


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


def test_explicit_budget_also_starts_with_global_pilot():
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

    inserted = job._enqueue_global_pilot(study, target_trials=8, sampler_seed=17)

    assert inserted == 8
    assert len(study.queued) == 8
    assert study.queued[0]["threshold"] == 0.0
    assert study.queued[-1]["threshold"] == 1.0


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
