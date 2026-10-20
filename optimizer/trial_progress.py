"""改写 Optuna 既有 Trial 日志，在同一行补上目标预算、剩余时间和指标进度。

不另起一行，不修改 Journal 的 trial_id，也不新增配置开关。
"""

import logging
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from multiprocessing import shared_memory


_TRIAL_LINE = re.compile(r"^Trial (\d+) (finished with|pruned\b|failed with)")
_ALREADY_REWRITTEN = re.compile(r"^(?:Metric \d+/\d+ .+ \| )?Trial \d+/\d+ ")
_APPLIED = "_quantada_trial_progress"


class TrialFinishCounter:
    """单进程完成计数。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._value = 0

    def add(self, amount=1):
        with self._lock:
            self._value += int(amount)
            return self._value

    def read(self):
        with self._lock:
            return int(self._value)

    @property
    def value(self):
        return self.read()


class SharedFinishCounter:
    """跨进程完成计数。只传递共享内存名，Windows 进程池无法序列化 Value。"""

    def __init__(self, name, lock_path, owner=False, shm=None):
        self.name = name
        self.lock_path = lock_path
        self._owner = owner
        self._shm = shm if shm is not None else shared_memory.SharedMemory(name=name)
        self._closed_value = 0

    @classmethod
    def create(cls):
        shm = shared_memory.SharedMemory(create=True, size=8)
        lock_path = None
        try:
            shm.buf[:8] = (0).to_bytes(8, "little")
            safe = "".join(ch if ch.isalnum() else "_" for ch in shm.name)
            lock_path = os.path.join(tempfile.gettempdir(), f"quantada_trial_{safe}.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            try:
                os.write(fd, b"\0")
            finally:
                os.close(fd)
            return cls(shm.name, lock_path, owner=True, shm=shm)
        except Exception:
            try:
                shm.close()
            except Exception:
                pass
            try:
                shm.unlink()
            except Exception:
                pass
            if lock_path:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
            raise

    def __getstate__(self):
        return {"name": self.name, "lock_path": self.lock_path}

    def __setstate__(self, state):
        self.name = state["name"]
        self.lock_path = state["lock_path"]
        self._owner = False
        self._closed_value = 0
        self._shm = shared_memory.SharedMemory(name=self.name)

    def add(self, amount=1):
        with _exclusive_lock(self.lock_path):
            updated = self._raw() + int(amount)
            self._shm.buf[:8] = int(updated).to_bytes(8, "little")
            return updated

    def read(self):
        if self._shm is None:
            return int(self._closed_value)
        with _exclusive_lock(self.lock_path):
            return self._raw()

    @property
    def value(self):
        return self.read()

    def close(self, unlink=False):
        if self._shm is not None:
            try:
                self._closed_value = self._raw()
            except Exception:
                pass
            try:
                self._shm.close()
            except Exception:
                pass
            if unlink and self._owner:
                try:
                    self._shm.unlink()
                except Exception:
                    pass
            self._shm = None
        if unlink and self.lock_path:
            try:
                os.remove(self.lock_path)
            except OSError:
                pass

    def _raw(self):
        return int.from_bytes(self._shm.buf[:8], "little")

    def __del__(self):
        try:
            self.close(unlink=False)
        except Exception:
            pass


@contextmanager
def _exclusive_lock(path):
    fd = os.open(path, os.O_RDWR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def format_remaining_duration(seconds):
    """把秒数格式化为 45s、12m1s、6h12m 或 2d3h。"""
    total = max(0, int(round(float(seconds))))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"



_ETA_WINDOW_SECONDS = 10 * 60


def _windowed_rate(samples):
    """用最近约 10 分钟的完成数和耗时估计速度；样本都在窗口内时用全部样本。"""
    if not samples or len(samples) < 2:
        return None
    end_count = int(samples[-1][0])
    end_time = float(samples[-1][1])
    cutoff = end_time - _ETA_WINDOW_SECONDS
    start_index = 0
    for index in range(len(samples) - 1, -1, -1):
        if float(samples[index][1]) < cutoff:
            start_index = index + 1
            break
    if start_index >= len(samples) - 1:
        start_index = len(samples) - 2
    done = end_count - int(samples[start_index][0])
    elapsed = end_time - float(samples[start_index][1])
    if done > 0 and elapsed >= 1.0:
        return done, elapsed
    return None


def progress_eta(completed, total, started_at, now, recent=None, parallel_workers=1):
    """按最近约 10 分钟的完成速度估计剩余项耗时，并复用训练 ETA 的格式化规则。"""
    remaining = max(0, int(total) - int(completed))
    window = _windowed_rate(list(recent or ()))
    if window is None:
        counted = int(completed)
        elapsed = float(now) - float(started_at)
    else:
        counted, elapsed = window
    return _format_eta(remaining, counted, elapsed, parallel_workers, observed=int(completed))


def _format_eta(remaining, counted, elapsed, parallel_workers=1, observed=None):
    """按已观测速度格式化 ETA。未满一轮并行样本或不足 1 秒时不外推。"""
    if int(remaining) <= 0:
        return "finishing"
    if int(counted) <= 0 or float(elapsed) < 1.0:
        return "ETA unavailable"
    workers = max(1, int(parallel_workers or 1))
    seen = int(counted if observed is None else observed)
    if workers > 1 and seen < workers:
        return f"ETA warming up ({seen}/{workers} workers)"
    eta = int(remaining) * float(elapsed) / int(counted)
    if int(round(eta)) <= 0:
        return "finishing"
    return f"ETA {format_remaining_duration(eta)}"


def _eta_label(remaining, counted, started_at, now, parallel_workers=1):
    """按当前轮墙钟速度格式化给定剩余 trial 的 ETA。"""
    return _format_eta(remaining, counted, float(now) - float(started_at), parallel_workers)


def remaining_label(target_trials, finished_before, counted, started_at, now, parallel_workers=1, future_remaining=0):
    """按当前指标速度估计当前及后续指标的整批剩余时间。"""
    remaining = max(0, int(target_trials) - int(finished_before) - int(counted))
    remaining += int(future_remaining)
    return _eta_label(remaining, counted, started_at, now, parallel_workers)


def metric_remaining_label(target_trials, finished_before, counted, started_at, now, parallel_workers=1):
    """按当前指标速度估计当前指标的剩余时间。"""
    remaining = max(0, int(target_trials) - int(finished_before) - int(counted))
    return _eta_label(remaining, counted, started_at, now, parallel_workers)


def make_trial_progress(
    target_trials, finished_before, counter, started_at=None, parallel_workers=1,
    metric_name=None, metrics=None, future_finished_counts=(),
    total_target_trials=None, total_finished_before=0,
):
    """组装当前指标和整批指标共用的进度状态。"""
    metric_label = None
    if metric_name is not None:
        requested_metrics = list(metrics) if metrics else [metric_name]
        if metric_name not in requested_metrics:
            requested_metrics = [metric_name]
        metric_label = f"Metric {requested_metrics.index(metric_name) + 1}/{len(requested_metrics)} {metric_name}"
    return {
        "target_trials": int(target_trials),
        "finished_before": max(0, int(finished_before)),
        "started_at": time.time() if started_at is None else float(started_at),
        "parallel_workers": max(1, int(parallel_workers or 1)),
        "counter": counter,
        "metric_label": metric_label,
        "future_remaining": sum(
            max(0, int(target_trials) - int(finished)) for finished in future_finished_counts
        ),
        "total_target_trials": int(total_target_trials or target_trials),
        "total_finished_before": max(0, int(total_finished_before)),
    }


class TrialProgressFilter(logging.Filter):
    """改写经过 Optuna 根 handler 的 Trial 行。同一条记录只计数一次。"""

    def __init__(self, progress):
        super().__init__()
        self.progress = progress

    def filter(self, record):
        try:
            self._rewrite(record)
        except Exception:
            return True
        return True

    def _rewrite(self, record):
        if getattr(record, _APPLIED, False):
            return
        message = _record_text(record)
        if message is None:
            return
        if _ALREADY_REWRITTEN.match(message):
            setattr(record, _APPLIED, True)
            return
        match = _TRIAL_LINE.match(message)
        if match is None or int(self.progress["target_trials"]) <= 0:
            return
        setattr(record, _APPLIED, True)
        counter = self.progress["counter"]
        if match.group(2) == "failed with":
            counted = counter.read()
        else:
            counted = counter.add(1)
        now = time.time()
        metric_eta = metric_remaining_label(
            self.progress["target_trials"],
            self.progress["finished_before"],
            counted,
            self.progress["started_at"],
            now,
            self.progress.get("parallel_workers", 1),
        )
        total_finished = self.progress.get("total_finished_before", self.progress["finished_before"]) + counted
        total_target = self.progress.get("total_target_trials", self.progress["target_trials"])
        total_eta = _eta_label(
            max(0, int(total_target) - int(total_finished)),
            counted,
            self.progress["started_at"],
            now,
            self.progress.get("parallel_workers", 1),
        )
        rest = message[match.end(1):]
        status_text = rest.lstrip()
        if status_text:
            status_text = status_text[0].upper() + status_text[1:]
            rest = " " + status_text
        metric_label = self.progress.get("metric_label")
        metric_prefix = f"{metric_label} | " if metric_label else ""
        record.msg = (
            f"{metric_prefix}Trial {match.group(1)}/{int(self.progress['target_trials'])} {metric_eta}"
            f" | Total Trial {int(total_finished)}/{int(total_target)} {total_eta} |{rest}"
        )
        record.args = ()
        if "message" in record.__dict__:
            record.message = record.msg


def _record_text(record):
    message = record.msg
    if not isinstance(message, str):
        return None
    if record.args:
        try:
            return message % record.args
        except Exception:
            return message
    return message


def _handlers_for_optuna():
    import optuna.logging as optuna_logging

    optuna_logging.get_logger("optuna")
    logger = logging.getLogger("optuna")
    handlers = list(logger.handlers)
    if handlers or not logger.propagate:
        return handlers
    return list(logging.getLogger().handlers)


@contextmanager
def installed_trial_progress(progress):
    """只在优化调用期间挂到 handler；子 logger 的过滤器不会随传播继承。"""
    if not progress:
        yield
        return
    progress_filter = TrialProgressFilter(progress)
    handlers = _handlers_for_optuna()
    for handler in handlers:
        handler.addFilter(progress_filter)
    try:
        yield
    finally:
        for handler in handlers:
            try:
                handler.removeFilter(progress_filter)
            except ValueError:
                pass
