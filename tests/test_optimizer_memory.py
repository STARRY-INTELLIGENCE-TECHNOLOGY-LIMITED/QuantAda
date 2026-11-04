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
from optimizer.memory_policy import (
    per_worker_bytes,
    read_available_memory,
    resolve_memory_worker_count,
    usable_memory_bytes,
    windows_physical_and_swap,
)
import optimizer.memory_policy as memory_policy
from optimizer.search_policy import recommended_pilot_count
from backtest.backtester import Backtester
from strategies.base_strategy import BaseStrategy


_GB = 1024 ** 3


def test_windows_commit_limit_is_not_added_to_physical_again():
    physical, swap = windows_physical_and_swap(int(11.8 * _GB), int(22.8 * _GB))
    assert physical + swap == int(22.8 * _GB)
    usable = usable_memory_bytes(physical, swap)
    assert usable < int(22.8 * _GB)
    assert usable > physical * 0.90


def test_windows_psutil_commit_is_converted_to_pagefile(monkeypatch):
    monkeypatch.setattr(memory_policy, "_windows_memory", lambda: None)
    monkeypatch.setattr(
        memory_policy,
        "_psutil_memory",
        lambda: (int(11.8 * _GB), int(22.8 * _GB)),
    )
    monkeypatch.setattr(memory_policy.os, "name", "nt")
    monkeypatch.setattr(memory_policy.sys, "platform", "win32")

    physical, swap = read_available_memory()
    expected = windows_physical_and_swap(int(11.8 * _GB), int(22.8 * _GB))
    assert (physical, swap) == expected


def test_memory_policy_limits_payload_copy_workers():
    assert resolve_memory_worker_count(
        13,
        2 * _GB,
        shared_memory=False,
        available_bytes=4 * _GB,
    ) == 1
    shared = resolve_memory_worker_count(
        13,
        2 * _GB,
        shared_memory=True,
        available_bytes=16 * _GB,
    )
    assert 1 <= shared <= 13
    assert shared == min(13, usable_memory_bytes(16 * _GB, 0) // per_worker_bytes(2 * _GB, True))


def test_pilot_count_follows_10d_and_ignores_hardware():
    assert recommended_pilot_count(21762, 15) == 150
    assert recommended_pilot_count(8, 15) == 8
    assert recommended_pilot_count(21762, 1) == 16
    assert recommended_pilot_count(21762, 80) == 800


def test_memory_reserve_ratio_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZER_MEMORY_RESERVE_RATIO", 0.50)
    assert resolve_memory_worker_count(
        13,
        1 * _GB,
        shared_memory=False,
        available_bytes=4 * _GB,
    ) == 1

    monkeypatch.setattr(config, "OPTIMIZER_MEMORY_RESERVE_RATIO", 0.05)
    workers = resolve_memory_worker_count(
        13,
        1 * _GB,
        shared_memory=False,
        available_bytes=4 * _GB,
    )
    assert workers == min(13, usable_memory_bytes(4 * _GB, 0) // per_worker_bytes(1 * _GB, False))


def test_cpu_worker_count_for_16c_and_32c(monkeypatch):
    monkeypatch.setattr(optimizer.OptimizationJob, "_get_total_cpu_cores", staticmethod(lambda: 16))
    assert optimizer.OptimizationJob._resolve_worker_count(-1) == 13
    monkeypatch.setattr(optimizer.OptimizationJob, "_get_total_cpu_cores", staticmethod(lambda: 32))
    assert optimizer.OptimizationJob._resolve_worker_count(-1) == 27
    assert optimizer.OptimizationJob._resolve_worker_count(31) == 31


def test_worker_budget_for_windows_16c32g_and_linux_32c128g():
    """16C32G Windows 大行情必须降并发；32C128G 显式 31 worker 在共享行情下发满。"""
    windows_copy = resolve_memory_worker_count(
        13,
        2 * _GB,
        shared_memory=False,
        available_bytes=12 * _GB,
        swap_bytes=8 * _GB,
    )
    assert 1 <= windows_copy <= 6

    windows_small = resolve_memory_worker_count(
        13,
        50 * 1024 ** 2,
        shared_memory=False,
        available_bytes=12 * _GB,
        swap_bytes=8 * _GB,
    )
    assert windows_small == 13

    linux_shared = resolve_memory_worker_count(
        31,
        4 * _GB,
        shared_memory=True,
        available_bytes=110 * _GB,
        swap_bytes=8 * _GB,
    )
    assert linux_shared == 31

    linux_heavy = resolve_memory_worker_count(
        31,
        8 * _GB,
        shared_memory=True,
        available_bytes=110 * _GB,
        swap_bytes=8 * _GB,
    )
    assert linux_heavy == 31


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


def test_force_shutdown_marks_reaped_worker_not_alive(monkeypatch):
    """Linux 上 waitpid 被抢走后，已消失的 worker 不能再让 is_alive() 一直为真。"""
    if os.name == "nt":
        pytest.skip("Windows 退出码由 join 回收，不走 ECHILD 补写")

    class Popen:
        def __init__(self):
            self.pid = 4242
            self.returncode = None

    class Proc:
        def __init__(self):
            self._popen = Popen()
            self.pid = 4242

        def is_alive(self):
            return self._popen.returncode is None

        def terminate(self):
            return None

        def kill(self):
            return None

        def join(self, timeout=None):
            return None

    proc = Proc()

    class Executor:
        def __init__(self):
            self._processes = {proc.pid: proc}

        def shutdown(self, wait=True, cancel_futures=False):
            self._processes = None

    def fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError(pid)
        return None

    def fake_waitpid(pid, flag):
        raise ChildProcessError(pid)

    monkeypatch.setattr(optimizer.os, "kill", fake_kill)
    monkeypatch.setattr(optimizer.os, "waitpid", fake_waitpid)
    optimizer.OptimizationJob._force_shutdown_process_pool(Executor(), [])
    assert proc._popen.returncode is not None
    assert proc.is_alive() is False


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

        def _finished_backtest():
            # 独立作用域结束后才检查回收，避免 Python 3.10 的 f_locals 快照把引擎留在当前测试帧。
            engine = Backtester(datas={"AAA": frame}, strategy_class=NoopStrategy, enable_plot=False, verbose=False)
            engine.run()
            ref = weakref.ref(engine.cerebro)
            del engine
            return ref

        engine_ref = _finished_backtest()
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
