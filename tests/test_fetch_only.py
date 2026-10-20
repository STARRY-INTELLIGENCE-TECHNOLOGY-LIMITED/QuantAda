import pytest

import optimizer.runtime as runtime
from optimizer.runtime import OptimizationJob, run_fetch_only


class _FakeDataManager:
    def __init__(self):
        self.closed = False

    def close_after_fetch(self):
        self.closed = True


def _args(**overrides):
    values = dict(
        strategy="strategies.sample",
        selection=None,
        symbols="AAA",
        risk=None,
        fetch_only=True,
        start_date="20240101",
        end_date="20240131",
        connect=None,
    )
    values.update(overrides)
    return type("Args", (), values)()


def test_fetch_only_job_stops_after_cache_write(monkeypatch):
    calls = []

    def fetch(self):
        calls.append("fetch")
        self._raw_data_fetch_range = ("20200101", "20240131")
        self.target_symbols = ["AAA", "US.AAPL240119P00100000"]
        return {"AAA": object(), "US.AAPL240119P00100000": object()}

    monkeypatch.setattr(runtime, "DataManager", _FakeDataManager)
    monkeypatch.setattr(runtime, "get_class_from_name", lambda *args, **kwargs: object())
    monkeypatch.setattr(OptimizationJob, "_fetch_all_data", fetch)
    monkeypatch.setattr(OptimizationJob, "_split_data", lambda self: calls.append("split"))
    monkeypatch.setattr(OptimizationJob, "_auto_refine_study_name", lambda self: calls.append("study"))

    job = OptimizationJob(_args(), {"min_dte": 15}, {"min_dte": {"type": "int", "low": 15, "high": 30}}, {})

    assert calls == ["fetch"]
    assert job.data_manager.closed is True
    assert set(job.raw_datas) == {"AAA", "US.AAPL240119P00100000"}
    assert job.train_datas == {}


def test_run_fetch_only_does_not_open_study(monkeypatch):
    created = []

    class _Job:
        def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
            created.append((args.strategy, fixed_params, opt_params_def, shared_context))
            self.raw_datas = {"AAA": object()}
            self.target_symbols = ["AAA"]
            self._raw_data_fetch_range = ("20240101", "20240131")

    monkeypatch.setattr(runtime, "OptimizationJob", _Job)
    monkeypatch.setattr(runtime, "resolve_study_plan", lambda *args, **kwargs: pytest.fail("study"))

    code = run_fetch_only(_args(), {"contracts": 1}, {}, ["AAA"])

    assert code == 0
    assert created == [("strategies.sample", {"contracts": 1}, {}, None)]


def test_run_fetch_only_rejects_comma_groups():
    code = run_fetch_only(_args(strategy="A,B"), {}, {}, None)
    assert code == 1


def test_fetch_only_cli_skips_optimizer(monkeypatch):
    import run

    called = {}
    monkeypatch.setattr(run.sys, "argv", [
        "run.py", "dummy_strategy", "--symbols", "AAA",
        "--opt_params", "{'x': {'type': 'int', 'low': 1, 'high': 2}}",
        "--fetch_only", "--start_date", "20240101", "--end_date", "20240131",
    ])
    monkeypatch.setattr(run.optimizer, "run_optimizer_mode", lambda **kwargs: called.setdefault("train", True))
    def _fetch(**kwargs):
        called["fetch"] = kwargs
        return 0

    monkeypatch.setattr(run.optimizer, "run_fetch_only", _fetch)

    with pytest.raises(SystemExit) as result:
        run._run_main()

    assert result.value.code == 0
    assert "train" not in called
    assert called["fetch"]["fixed_params"] == {}
    assert called["fetch"]["args"].fetch_only is True


def test_fetch_only_rejects_live_connect(monkeypatch):
    import run

    monkeypatch.setattr(run.sys, "argv", [
        "run.py", "dummy_strategy", "--connect", "gm_broker:sim", "--fetch_only",
    ])
    with pytest.raises(SystemExit) as result:
        run._run_main()
    assert result.value.code == 2
