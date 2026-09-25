"""改写 Optuna 既有 Trial 日志，在同一行补上目标预算和剩余时间。

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
_ALREADY_REWRITTEN = re.compile(r"^Trial \d+/\d+ ")
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


def remaining_label(target_trials, finished_before, counted, started_at, now):
    """按本次运行墙钟速度估计剩余时间；失败不计入速度。"""
    remaining = int(target_trials) - int(finished_before) - int(counted)
    if remaining <= 0:
        return "finishing"
    elapsed = float(now) - float(started_at)
    if int(counted) <= 0 or elapsed < 1.0:
        return "ETA unavailable"
    eta = remaining * elapsed / int(counted)
    if int(round(eta)) <= 0:
        return "finishing"
    return f"ETA {format_remaining_duration(eta)}"


def make_trial_progress(target_trials, finished_before, counter, started_at=None):
    """组装可传入 worker 的进度状态。分母是指标目标预算，不是本轮剩余额度。"""
    return {
        "target_trials": int(target_trials),
        "finished_before": max(0, int(finished_before)),
        "started_at": time.time() if started_at is None else float(started_at),
        "counter": counter,
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
        label = remaining_label(
            self.progress["target_trials"],
            self.progress["finished_before"],
            counted,
            self.progress["started_at"],
            time.time(),
        )
        rest = message[match.end(1):]
        record.msg = f"Trial {match.group(1)}/{int(self.progress['target_trials'])} {label}{rest}"
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
