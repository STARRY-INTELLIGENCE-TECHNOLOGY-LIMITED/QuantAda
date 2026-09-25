"""跨交易日恢复固定数据、原始命令回显及失效快照隔离。"""

import copy
import ast
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from optimizer.data_snapshot import load_training_snapshot, save_training_snapshot
from optimizer.runtime import OptimizationJob


class SnapshotStrategy:
    """测试只计算数据分数，保留期权有限网格入口。"""

    option_universe = ("PUT",)


class SnapshotSelector:
    """可变化的选股结果，用于验证恢复时不会重新运行。"""

    symbols = ["AAA"]
    calls = 0

    def __init__(self, data_manager):
        pass

    def run_selection(self):
        type(self).calls += 1
        return list(type(self).symbols)


def _context():
    frame = pd.DataFrame(
        {"close": [1.25, 2.5], "kind": pd.Series(["PUT", "PUT"], dtype="string").array},
        index=pd.date_range("2026-09-21", periods=2, tz="America/New_York"),
    )
    frame.attrs["contract_multiplier"] = 100
    return {"raw_datas": {"OPTION": frame}, "train_datas": {"OPTION": frame.iloc[:1]},
            "test_datas": {"OPTION": frame.iloc[1:]}, "target_symbols": ["SPY", "OPTION"],
            "source_symbols": ["SPY"], "warmup_days": 400, "train_range": ("20260921", "20260921"),
            "test_range": ("20260922", "20260922"), "raw_data_fetch_range": ("20250101", "20260922")}


def test_snapshot_roundtrip_preserves_data_universe_timezone_and_splits(tmp_path):
    journal = tmp_path / "batch.log"
    context = _context()
    original = ["private.Strategy", "--params", "{'name': '$HOME `value`'}"]
    args = SimpleNamespace(start_date="20260921", end_date="20260922", timeframe="Days", compression=1)
    reference = save_training_snapshot(journal, context, args, original, {"LOT_SIZE": 7})
    restored, manifest = load_training_snapshot(journal, reference)
    for field in ("raw_datas", "train_datas", "test_datas"):
        pd.testing.assert_frame_equal(restored[field]["OPTION"], context[field]["OPTION"])
    assert restored["target_symbols"] == ["SPY", "OPTION"]
    assert restored["source_symbols"] == ["SPY"]
    assert restored["runtime_config"] == {"LOT_SIZE": 7}
    assert manifest["original_argv"] == original
    assert manifest["coverage"]["OPTION"]["timezone"] == "America/New_York"
    context["raw_datas"]["OPTION"].iloc[0, 0] = 999
    again, _ = load_training_snapshot(journal, reference)
    assert again["raw_datas"]["OPTION"].iloc[0, 0] == 1.25


@pytest.mark.parametrize("filename", ["manifest.json", "data.pkl"])
def test_corrupt_snapshot_is_rejected_before_use(tmp_path, filename):
    journal = tmp_path / "batch.log"
    reference = save_training_snapshot(journal, _context(), SimpleNamespace(start_date="20260921", end_date="20260922"), ["demo.Strategy"], {})
    path = Path(str(journal) + ".snapshots") / reference["id"] / filename
    path.write_bytes(path.read_bytes() + b"damaged")
    with pytest.raises(ValueError, match="校验失败"):
        load_training_snapshot(journal, reference)


def test_snapshot_write_failure_leaves_no_committed_snapshot(monkeypatch, tmp_path):
    import optimizer.data_snapshot as snapshots

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(snapshots.pickle, "dump", broken)
    journal = tmp_path / "batch.log"
    with pytest.raises(OSError, match="disk full"):
        save_training_snapshot(journal, _context(), SimpleNamespace(start_date="20260921", end_date="20260922"), [], {})
    assert list(Path(str(journal) + ".snapshots").iterdir()) == []


def test_frozen_validation_never_fetches_missing_or_short_history():
    job = OptimizationJob.__new__(OptimizationJob)
    job._snapshot_frozen = True
    job._window_data_cache = {}
    job._raw_data_fetch_range = (None, None)
    job.warmup_days = 0
    job.raw_datas = {"AAA": pd.DataFrame({"close": [1.0, 2.0]}, index=pd.date_range("2024-01-02", periods=2))}
    job.target_symbols = ["AAA", "missing"]
    job.data_manager = SimpleNamespace(get_data=lambda *a, **k: pytest.fail("frozen data must stay offline"))
    result = job._fetch_datas_for_window("20240101", "20240105")
    assert list(result) == ["AAA"]
    assert result["AAA"]["close"].tolist() == [1.0, 2.0]


@pytest.mark.parametrize("restart", ["refresh", "missing", "corrupt"])
def test_cross_day_resume_freezes_selection_options_and_scores_then_isolates_new_data(monkeypatch, tmp_path, restart, capsys):
    import config
    import optuna
    import optimizer.runtime as runtime
    import data_providers.option_universe as universe
    from optimizer.journal_metadata import read_study_metadata
    from test_optimizer_resume import _args, _storage

    fetched = []
    expanded = []
    scores = []
    market = {"value": 100.0, "option": "OLD_OPTION"}
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setattr(config, "LOG", False)
    monkeypatch.delenv("QUANTADA_OPTIMIZER_TERMINAL_LOG", raising=False)
    monkeypatch.setattr(SnapshotSelector, "symbols", ["AAA"])
    monkeypatch.setattr(SnapshotSelector, "calls", 0)

    class DataManager:
        def get_data(self, symbol, **kwargs):
            fetched.append((symbol, kwargs.get("refresh")))
            return pd.DataFrame({"close": [market["value"]] * 3}, index=pd.date_range("2024-01-01", periods=3))

        def close_after_fetch(self):
            pass

    def expand(symbols, **kwargs):
        expanded.append(list(symbols))
        return [*symbols, market["option"]]

    class OfflineJob(OptimizationJob):
        def _evaluate_trial_params(self, params):
            value = float(self.train_datas[self._source_symbols[0]]["close"].iloc[0])
            scores.append((list(self._source_symbols), list(self.target_symbols), value))
            return value

    monkeypatch.setattr(runtime, "DataManager", DataManager)
    monkeypatch.setattr(runtime, "OptimizationJob", OfflineJob)
    monkeypatch.setattr(universe, "expand_option_universe", expand)
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_: False)
    monkeypatch.setattr(OfflineJob, "_launch_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(OfflineJob, "_run_main_eval_backtest", lambda *a, **k: None)
    monkeypatch.setattr(OfflineJob, "_run_yearly_validation_backtests", lambda *a, **k: [])
    original = [f"{__name__}.SnapshotStrategy", "--selection", f"{__name__}.SnapshotSelector", "--train_roll_period", "2y"]
    monkeypatch.setattr(runtime.sys, "argv", ["run.py", *original])

    def args(**changes):
        return _args(strategy=original[0], selection=original[2], **changes)

    def infer_next_day(value):
        value.start_date = value.start_date or "20230924"
        value.end_date = value.end_date or "20260924"

    monkeypatch.setattr(runtime, "infer_omitted_backtest_window", infer_next_day)
    first = args(n_trials=1)
    assert runtime.run_optimizer_mode(first, ast.literal_eval(first.params), {}, []) == 0
    journals = sorted((tmp_path / "data/optuna").glob("*.log"))
    journal = next(path for path in journals if Path(str(path) + ".snapshots").is_dir())
    before = [study for path in journals for study in read_study_metadata(path)]
    reference = before[0]["attrs"]["_optimizer_data_snapshot"]
    assert before[0]["attrs"]["_optimizer_original_argv"] == original
    assert len(fetched) == 2 and len(expanded) == SnapshotSelector.calls == 1
    market.update(value=700.0, option="NEW_OPTION")
    SnapshotSelector.symbols = ["BBB"]

    second = args(n_trials=2, start_date=None, end_date=None)
    assert runtime.run_optimizer_mode(second, ast.literal_eval(second.params), {}, []) == 0
    assert second.end_date == "20260923"
    assert len(fetched) == 2 and len(expanded) == SnapshotSelector.calls == 1
    assert len(scores) == 4
    assert all(item == (["AAA"], ["AAA", "OLD_OPTION"], 100.0) for item in scores)
    for info in before:
        owner = next(path for path in journals if any(item["name"] == info["name"] for item in read_study_metadata(path)))
        study = optuna.load_study(study_name=info["name"], storage=_storage(owner))
        assert len(study.trials) == 2
        assert study.best_value == 100.0
        assert all(trial.number == trial._trial_id for trial in study.trials)

    if restart != "refresh":
        data_path = Path(str(journal) + ".snapshots") / reference["id"] / "data.pkl"
        if restart == "missing":
            data_path.unlink()
        else:
            data_path.write_bytes(data_path.read_bytes() + b"corrupt")
    third = args(n_trials=1, start_date=None, end_date=None, refresh=restart == "refresh")
    assert runtime.run_optimizer_mode(third, ast.literal_eval(third.params), {}, []) == 0
    assert len(fetched) == 4 and len(expanded) == SnapshotSelector.calls == 2
    if restart == "refresh":
        assert scores[-1] == (["BBB"], ["BBB", "NEW_OPTION"], 700.0)
        assert third.end_date == "20260924"
        assert fetched[-1][1] is True
    else:
        # 原预算已完成，损坏快照只重新绑定数据，不把旧评分混进新 Study。
        assert third.end_date == "20260923"
        assert scores[-1] == (["AAA"], ["AAA", "OLD_OPTION"], 100.0)
    fourth = args(n_trials=3 if restart != "refresh" else 1, start_date=third.start_date, end_date=third.end_date)
    assert runtime.run_optimizer_mode(fourth, ast.literal_eval(fourth.params), {}, []) == 0
    assert len(fetched) == 4 and len(scores) == 6
    if restart != "refresh":
        assert scores[-1] == (["BBB"], ["BBB", "NEW_OPTION"], 700.0)
    assert "Original launch command" in capsys.readouterr().out


def test_snapshot_restore_reapplies_explicit_config(monkeypatch, tmp_path):
    """快照恢复训练环境后，本轮已解析的 --config 仍覆盖同名键。"""
    import config
    import optimizer.runtime as runtime
    from contextlib import nullcontext

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "LOT_SIZE", 9)
    monkeypatch.setattr(config, "ANNUAL_FACTOR", 252)
    monkeypatch.delenv("QUANTADA_OPTIMIZER_TERMINAL_LOG", raising=False)
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_args: False)
    monkeypatch.setattr(runtime, "study_run_lock", lambda _path: nullcontext(True))
    monkeypatch.setattr(runtime, "resolve_study_plan", lambda *_args, **_kwargs: {
        "journal": str(tmp_path / "optuna_saved.log"),
        "studies": {"return": "saved"},
        "matched": [{"name": "saved", "attrs": {
            "metric": "return",
            "_optimizer_data_snapshot": {"id": "a" * 32, "sha256": "b" * 64},
        }}],
        "source_studies": [],
        "incompatible": [],
    })

    def load_snapshot(_journal, _reference):
        return {
            "raw_datas": {}, "train_datas": {}, "test_datas": {}, "target_symbols": [],
            "source_symbols": [], "train_range": ("20240101", "20240102"),
            "test_range": ("20240103", "20240104"), "warmup_days": 0,
            "raw_data_fetch_range": ("20240101", "20240104"), "snapshot_frozen": True,
            "runtime_config": {"LOT_SIZE": 1, "ANNUAL_FACTOR": 360},
        }, {"original_argv": ["demo"], "original_exact": True}

    monkeypatch.setattr(runtime, "load_training_snapshot", load_snapshot)
    seen = {}

    def stop_after_config(self, **_kwargs):
        seen["lot"] = config.LOT_SIZE
        seen["annual"] = config.ANNUAL_FACTOR
        raise RuntimeError("stop after config restore")

    monkeypatch.setattr(runtime.OptimizationJob, "__init__", stop_after_config)
    args = SimpleNamespace(
        metric="return", strategy=None, opt_params="{}", opt_schedule=None, refresh=False,
        start_date="20240101", end_date="20240131", config="{'LOT_SIZE': 9, 'UNKNOWN_KEY': 1}",
        n_jobs=1, test_period=None, test_roll_period=None,
    )

    assert runtime.run_optimizer_mode(args, {}, {}, []) == 1
    assert seen == {"lot": 9, "annual": 360}
    assert config.LOT_SIZE == 9
    assert config.ANNUAL_FACTOR == 360


class PlainStrategy:
    """快照加载测试使用的空策略，不展开期权池。"""


def _optimizer_harness(monkeypatch, tmp_path):
    """屏蔽提权、看板和验证回测，只记录是否重新构造数据管理器。"""
    import config
    import optimizer.runtime as runtime

    constructed = []
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setattr(config, "LOG", False)
    monkeypatch.setattr(config, "ANNUAL_FACTOR", 111)
    monkeypatch.setattr(config, "LOT_SIZE", 3)
    monkeypatch.delenv("QUANTADA_OPTIMIZER_TERMINAL_LOG", raising=False)
    monkeypatch.setattr(runtime.process_elevation, "request_optimizer_elevation_if_needed", lambda *_args: False)
    monkeypatch.setattr(runtime.OptimizationJob, "_launch_dashboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.OptimizationJob, "_run_main_eval_backtest", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.OptimizationJob, "_run_yearly_validation_backtests", lambda *args, **kwargs: [])
    monkeypatch.setattr(runtime.OptimizationJob, "_run_test_set_backtest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime.OptimizationJob, "_split_data",
        lambda self: (self.raw_datas, {}, ("20240101", "20260922"), ("20260923", "20260923")),
    )

    class DataManager:
        def __init__(self):
            constructed.append("dm")

        def get_data(self, symbol, **kwargs):
            constructed.append(("fetch", symbol))
            return pd.DataFrame({"close": [1.0]}, index=pd.date_range("2026-09-23", periods=1))

        def close_after_fetch(self):
            pass

    monkeypatch.setattr(runtime, "DataManager", DataManager)
    monkeypatch.setattr(runtime.sys, "argv", ["run.py", f"{__name__}.PlainStrategy"])
    return runtime, constructed


def _seed_snapshot_journal(tmp_path, reference, *, own_snapshot=None):
    """旧 Study 没有快照且试验更多；同 Journal 的薄 Study 持有可沿用快照。"""
    import optuna
    from test_optimizer_resume import _args, _write_study

    journal = tmp_path / "data" / "optuna" / "optuna_batch.log"
    journal.parent.mkdir(parents=True, exist_ok=True)
    base = _args(
        strategy=f"{__name__}.PlainStrategy", selection=None, symbols="AAA", metric="return",
        n_trials=2, study_journal=str(journal),
    )
    old = _write_study(journal, "old", base, "return", config_version=None)
    old.add_trial(optuna.trial.create_trial(value=1.0))
    old.add_trial(optuna.trial.create_trial(value=2.0))
    if own_snapshot is not None:
        old.set_user_attr("_optimizer_data_snapshot", own_snapshot)
    thin = _write_study(journal, "thin", base, "return")
    thin.add_trial(optuna.trial.create_trial(value=3.0))
    thin.set_user_attr("_optimizer_data_snapshot", reference)
    del old, thin
    return journal, base


def _study_attrs(directory, name):
    from optimizer.journal_metadata import read_study_metadata

    found = None
    for path in Path(directory).glob("*.log"):
        for study in read_study_metadata(path):
            if study["name"] == name:
                found = study["attrs"]
    return found


def test_fallback_snapshot_is_loaded_without_fetch(monkeypatch, tmp_path, capsys):
    """没有快照的原 Study 必须实际加载同 Journal 快照，不能只打印复用后重新取数。"""
    import config
    from optimizer.data_snapshot import save_training_snapshot

    runtime, constructed = _optimizer_harness(monkeypatch, tmp_path)
    journal = tmp_path / "data" / "optuna" / "optuna_batch.log"
    journal.parent.mkdir(parents=True, exist_ok=True)
    from test_optimizer_resume import _args
    base = _args(
        strategy=f"{__name__}.PlainStrategy", selection=None, symbols="AAA", metric="return",
        n_trials=2, study_journal=str(journal),
    )
    reference = save_training_snapshot(
        journal, _context(), base, ["demo"], {"LOT_SIZE": 7, "ANNUAL_FACTOR": 360},
    )
    journal, base = _seed_snapshot_journal(tmp_path, reference)
    assert runtime.run_optimizer_mode(base, ast.literal_eval(base.params), ast.literal_eval(base.risk_params), []) == 0
    attrs = _study_attrs(journal.parent, "old")
    assert constructed == []
    assert attrs["_optimizer_data_snapshot"]["id"] == reference["id"]
    assert attrs["_optimizer_reused_pre_snapshot"] is True
    assert config.LOT_SIZE == 1
    assert config.ANNUAL_FACTOR == 360
    output = capsys.readouterr().out
    assert f"Loaded sibling snapshot {reference['id']}" in output
    assert "not treated as results of this snapshot" in output
    assert "Reusing a snapshot already saved" not in output
    assert "could not be loaded" not in output
    saved = [path.name for path in Path(str(journal) + ".snapshots").iterdir() if path.is_dir()]
    assert saved == [reference["id"]]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unreadable_fallback_snapshot_is_not_bound(monkeypatch, tmp_path, damage, capsys):
    """沿用失败时不保留损坏引用，重新取数并绑定新快照。"""
    from optimizer.data_snapshot import save_training_snapshot

    runtime, constructed = _optimizer_harness(monkeypatch, tmp_path)
    journal = tmp_path / "data" / "optuna" / "optuna_batch.log"
    journal.parent.mkdir(parents=True, exist_ok=True)
    from test_optimizer_resume import _args
    base = _args(
        strategy=f"{__name__}.PlainStrategy", selection=None, symbols="AAA", metric="return",
        n_trials=2, study_journal=str(journal),
    )
    reference = save_training_snapshot(journal, _context(), base, ["demo"], {"LOT_SIZE": 1, "ANNUAL_FACTOR": 252})
    journal, base = _seed_snapshot_journal(tmp_path, reference)
    snapshot = Path(str(journal) + ".snapshots") / reference["id"]
    if damage == "missing":
        import shutil
        shutil.rmtree(snapshot)
    else:
        data = snapshot / "data.pkl"
        data.write_bytes(data.read_bytes() + b"corrupt")
    assert runtime.run_optimizer_mode(base, ast.literal_eval(base.params), ast.literal_eval(base.risk_params), []) == 0
    attrs = _study_attrs(journal.parent, "old")
    bound = attrs["_optimizer_data_snapshot"]["id"]
    assert "dm" in constructed
    assert ("fetch", "AAA") in constructed
    assert bound != reference["id"]
    assert attrs["_optimizer_reused_pre_snapshot"] is True
    assert attrs.get("_optimizer_rebound_snapshot") is not True
    output = capsys.readouterr().out
    assert "could not be loaded" in output
    assert "Loaded sibling snapshot" not in output
    assert "skipping symbol selection and market fetch" not in output
    assert (Path(str(journal) + ".snapshots") / bound).is_dir()


def test_unreadable_own_snapshot_does_not_switch_to_sibling(monkeypatch, tmp_path, capsys):
    """本 Study 快照损坏时重新准备，不能改用同 Journal 的另一份快照。"""
    import optuna
    from optimizer.data_snapshot import save_training_snapshot
    from test_optimizer_resume import _args, _write_study

    runtime, constructed = _optimizer_harness(monkeypatch, tmp_path)
    journal = tmp_path / "data" / "optuna" / "optuna_batch.log"
    journal.parent.mkdir(parents=True, exist_ok=True)
    base = _args(
        strategy=f"{__name__}.PlainStrategy", selection=None, symbols="AAA", metric="return",
        n_trials=1, study_journal=str(journal),
    )
    sibling = save_training_snapshot(journal, _context(), base, ["demo"], {"LOT_SIZE": 1, "ANNUAL_FACTOR": 252})
    own = {"id": "1" * 32, "sha256": "2" * 64}
    saved = _write_study(journal, "saved", base, "return")
    saved.add_trial(optuna.trial.create_trial(value=1.0))
    saved.set_user_attr("_optimizer_data_snapshot", own)
    del saved

    def plan(*_args, **_kwargs):
        return {
            "journal": str(journal), "studies": {"return": "saved"},
            "matched": [{"name": "saved", "attrs": {"metric": "return", "_optimizer_data_snapshot": own}}],
            "source_studies": [], "incompatible": [], "fallback_snapshot": sibling,
        }

    monkeypatch.setattr(runtime, "resolve_study_plan", plan)
    assert runtime.run_optimizer_mode(base, ast.literal_eval(base.params), ast.literal_eval(base.risk_params), []) == 0
    attrs = _study_attrs(journal.parent, "saved")
    bound = attrs["_optimizer_data_snapshot"]["id"]
    assert "dm" in constructed
    assert bound not in {own["id"], sibling["id"]}
    assert attrs["_optimizer_rebound_snapshot"] is True
    output = capsys.readouterr().out
    assert "Training snapshot is unreadable" in output
    assert "Loaded sibling snapshot" not in output
    assert "skipping symbol selection and market fetch" not in output
