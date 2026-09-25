"""验证训练进程的内存边界及其对父进程环境的隔离。"""

import gc
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import weakref

import pandas as pd
import pytest

import config
import optimizer.runtime as optimizer
from backtest.backtester import Backtester
from strategies.base_strategy import BaseStrategy


def _wait_for_forced_shutdown(marker):
    """模拟仍占用计算资源的 worker，由父测试负责终止。"""
    Path(marker).write_text("started", encoding="utf-8")
    time.sleep(30)


def test_forced_shutdown_stops_spawn_worker_before_return(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    marker = tmp_path / "started.txt"
    pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    future = pool.submit(_wait_for_forced_shutdown, str(marker))
    processes = list(pool._processes.values())
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        optimizer.OptimizationJob._force_shutdown_process_pool(pool, [future])
        assert all(not process.is_alive() for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        pool.shutdown(wait=True, cancel_futures=True)


def _probe_spawn_runtime(payload, study_name, log_file, n_trials, worker_idx, seed, candidates, **kwargs):
    """在真实 spawn 进程中检查导入时已生效的线程数，不运行训练或连接行情。"""
    import numpy as np
    from threadpoolctl import threadpool_info

    report = {
        "blas_env": os.environ.get("OPENBLAS_NUM_THREADS"),
        "blas_threads": [pool["num_threads"] for pool in threadpool_info() if pool["internal_api"] == "openblas"],
        "visualization_imported": "optuna.visualization" in sys.modules,
        "result": float(np.dot([1.0, 2.0], [3.0, 4.0])),
    }
    Path(log_file).with_suffix(f".{worker_idx}.json").write_text(json.dumps(report), encoding="utf-8")
    return {"worker_idx": worker_idx, "stopped_early": False}


@pytest.mark.parametrize("explicit_threads", [None, "2"])
def test_spawn_blas_default_applies_before_import_and_preserves_parent(monkeypatch, tmp_path, explicit_threads):
    if explicit_threads is None:
        monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    else:
        monkeypatch.setenv("OPENBLAS_NUM_THREADS", explicit_threads)
    monkeypatch.setattr(optimizer, "_optimize_worker_entry", _probe_spawn_runtime)
    monkeypatch.setattr(optimizer.OptimizationJob, "_get_total_cpu_cores", staticmethod(lambda: 2))
    as_completed = optimizer.as_completed

    def wait_for_workers(futures):
        # 父进程等待长期训练任务时，临时环境值必须已经恢复。
        assert os.environ.get("OPENBLAS_NUM_THREADS") == explicit_threads
        return as_completed(futures)

    monkeypatch.setattr(optimizer, "as_completed", wait_for_workers)
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="memory-probe")
    job._build_worker_payload = lambda: {"train_datas": {}}
    log_file = tmp_path / "probe.log"

    job._run_multiprocess_optimization(n_jobs=2, n_trials=2, log_file=str(log_file))

    assert os.environ.get("OPENBLAS_NUM_THREADS") == explicit_threads
    for worker_idx in (1, 2):
        report = json.loads(log_file.with_suffix(f".{worker_idx}.json").read_text(encoding="utf-8"))
        assert report["blas_env"] == (explicit_threads or "1")
        assert all(threads == int(explicit_threads or "1") for threads in report["blas_threads"])
        assert report["visualization_imported"] is False
        assert report["result"] == 11.0


@pytest.mark.parametrize("failure_stage", ["create", "submit"])
@pytest.mark.parametrize("explicit_threads", [None, "3"])
def test_spawn_failure_restores_parent_blas_environment(monkeypatch, failure_stage, explicit_threads):
    if explicit_threads is None:
        monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    else:
        monkeypatch.setenv("OPENBLAS_NUM_THREADS", explicit_threads)
    closed = []

    class FailingExecutor:
        def __init__(self, **kwargs):
            assert os.environ["OPENBLAS_NUM_THREADS"] == (explicit_threads or "1")
            if failure_stage == "create":
                raise RuntimeError("startup failed")

        def submit(self, *args, **kwargs):
            assert os.environ["OPENBLAS_NUM_THREADS"] == (explicit_threads or "1")
            raise RuntimeError("startup failed")

        def shutdown(self, **kwargs):
            closed.append(True)

    monkeypatch.setattr(optimizer, "ProcessPoolExecutor", FailingExecutor)
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="memory-probe")
    job._build_worker_payload = lambda: {"train_datas": {}}

    with pytest.raises(RuntimeError, match="startup failed"):
        job._run_multiprocess_optimization(n_jobs=1, n_trials=1, log_file="unused.log")

    assert os.environ.get("OPENBLAS_NUM_THREADS") == explicit_threads
    assert closed == ([True] if failure_stage == "submit" else [])


def test_pool_start_reclaims_finished_backtest_cycles(monkeypatch):
    monkeypatch.setattr(config, "PRINT_PLAN", False)

    class NoopStrategy(BaseStrategy):
        def init(self):
            pass

        def next(self):
            pass

    class ObservingExecutor:
        def __init__(self, **kwargs):
            assert engine_ref() is None
            raise RuntimeError("stop before spawning")

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="memory-probe")
    job._build_worker_payload = lambda: {"train_datas": {}}
    monkeypatch.setattr(optimizer, "ProcessPoolExecutor", ObservingExecutor)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        frame = pd.DataFrame(
            {name: [100.0] * 3 for name in ("open", "high", "low", "close", "volume")},
            index=pd.date_range("2024-01-01", periods=3),
        )
        engine = Backtester(datas={"AAA": frame}, strategy_class=NoopStrategy, enable_plot=False, verbose=False)
        engine.run()
        engine_ref = weakref.ref(engine.cerebro)
        del engine
        assert engine_ref() is not None

        with pytest.raises(RuntimeError, match="stop before spawning"):
            job._run_multiprocess_optimization(n_jobs=1, n_trials=1, log_file="unused.log")
    finally:
        if gc_was_enabled:
            gc.enable()


def test_fork_keeps_existing_blas_environment(monkeypatch):
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    monkeypatch.setattr(optimizer.sys, "platform", "linux")
    monkeypatch.setattr(optimizer.mp, "get_context", lambda method: method)
    monkeypatch.setattr(optimizer, "as_completed", lambda futures: futures)

    class Future:
        def result(self):
            return {"worker_idx": 1, "stopped_early": False}

    class ForkExecutor:
        def __init__(self, **kwargs):
            assert kwargs["mp_context"] == "fork"
            assert "OPENBLAS_NUM_THREADS" not in os.environ

        def submit(self, *args, **kwargs):
            assert "OPENBLAS_NUM_THREADS" not in os.environ
            return Future()

        def shutdown(self, **kwargs):
            pass

    monkeypatch.setattr(optimizer, "ProcessPoolExecutor", ForkExecutor)
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="memory-probe")
    job._build_worker_payload = lambda: {"train_datas": {}}

    job._run_multiprocess_optimization(n_jobs=1, n_trials=1, log_file="unused.log", prefer_fork_cow=True)

    assert "OPENBLAS_NUM_THREADS" not in os.environ
    assert optimizer._FORK_SHARED_WORKER_PAYLOAD is None
