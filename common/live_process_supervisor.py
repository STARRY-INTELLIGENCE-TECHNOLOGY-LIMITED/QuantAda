"""长时间运行的实盘命令的进程级监督器。

券商 SDK 经常依赖原生代码。同一 Python 进程内的重试循环无法恢复原生 ``exit``、段错误、死锁或永不返回的 SDK 调用。本模块保持监督器精简且与券商无关：父进程负责重启策略，子进程负责交易状态和 SDK 对象。

心跳有意使用文件，而不是 socket 或 Python 队列。它适用于通过 ``nohup`` 启动的命令，能够适应 SDK 的线程布局，并在父进程终止卡死 worker 时提供有用的诊断快照。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from enum import Enum
from typing import Iterable, Optional

from common.live_runtime import runtime_print


# 这些是有意固定的运行安全阈值，不是交易配置项；集中放在这里可避免 live/backtest 配置意外改变进程恢复语义。
HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_STALE_SECONDS = 90.0
HEALTH_MONITOR_INTERVAL_SECONDS = 2.0
HEALTH_KILL_GRACE_SECONDS = 10.0
OPERATOR_STOP_GRACE_SECONDS = 25.0
EXPECTED_EXIT_VISIBILITY_GRACE_SECONDS = 1.0
WORKER_STARTUP_DEADLINE_SECONDS = 10 * 60.0
STABLE_WORKER_SECONDS = 10 * 60.0
RESTART_BACKOFF_SECONDS = (5.0, 10.0, 20.0, 40.0, 60.0)
WORKER_RESTART_EXIT_CODE = 75

WORKER_ENV = "QUANTADA_LIVE_WORKER"
HEARTBEAT_ENV = "QUANTADA_LIVE_HEARTBEAT"
WORKER_ID_ENV = "QUANTADA_LIVE_WORKER_ID"
PREVIOUS_FAILURE_ENV = "QUANTADA_LIVE_PREVIOUS_FAILURE"
PREVIOUS_FAILURE_KIND_ENV = "QUANTADA_LIVE_PREVIOUS_FAILURE_KIND"

_ORIGINAL_ARGV = tuple(sys.argv)
_heartbeat_lock = threading.Lock()
_worker_heartbeat = None
_operator_stop_requested = False


class LiveWorkerFailureKind(str, Enum):
    """跨 worker generation 传递的结构化故障类别。"""

    CONNECTIVITY = "connectivity"


class LiveWorkerOperatorStop(BaseException):
    """在 operator 明确关闭时跳出 broker SDK 恢复循环。"""


def _coerce_failure_kind(value) -> Optional[LiveWorkerFailureKind]:
    if isinstance(value, LiveWorkerFailureKind):
        return value
    try:
        return LiveWorkerFailureKind(str(value or "").strip())
    except ValueError:
        return None


def is_live_worker_process() -> bool:
    """返回当前进程是否为受监督的交易 worker。"""

    return os.environ.get(WORKER_ENV, "").strip() == "1"


def get_previous_live_worker_failure() -> str:
    """返回父 supervisor 最近一次重启原因，没有则返回空值。"""

    return str(os.environ.get(PREVIOUS_FAILURE_ENV, "") or "").strip()


def get_previous_live_worker_failure_kind() -> Optional[LiveWorkerFailureKind]:
    """返回父 supervisor 提供的结构化故障类别。"""

    return _coerce_failure_kind(os.environ.get(PREVIOUS_FAILURE_KIND_ENV))


def is_live_worker_restart() -> bool:
    """返回当前 worker 是否在上一个 worker 故障后启动。"""

    return bool(get_previous_live_worker_failure())


class _WorkerHeartbeat:
    """仅供 worker 进程使用的线程安全 heartbeat 快照写入器。"""

    def __init__(self, path: str, interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS):
        self.path = os.path.abspath(path)
        self.tmp_path = f"{self.path}.{os.getpid()}.tmp"
        self.interval_seconds = max(0.2, float(interval_seconds))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        now_monotonic = time.monotonic()
        self._snapshot = {
            "pid": os.getpid(),
            "worker_id": str(os.environ.get(WORKER_ID_ENV, "") or "").strip(),
            "state": "worker_starting",
            "detail": "",
            "expected_exit": False,
            "state_since_monotonic": now_monotonic,
            "updated_monotonic": now_monotonic,
            "updated_at": time.time(),
            "unhealthy_after_seconds": WORKER_STARTUP_DEADLINE_SECONDS,
            "failure_kind": "",
        }

    def start(self):
        with self._lock:
            self._write_locked()
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="quantada-live-heartbeat",
                daemon=True,
            )
            self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval_seconds + 0.5))

    def update(
        self,
        state: str,
        unhealthy_after_seconds=None,
        detail=None,
        expected_exit=False,
        failure_kind=None,
        refresh_deadline=False,
    ):
        state_text = str(state or "worker_running").strip() or "worker_running"
        try:
            timeout = (
                None
                if unhealthy_after_seconds is None
                else max(0.0, float(unhealthy_after_seconds))
            )
        except (TypeError, ValueError):
            timeout = None
        normalized_failure_kind = _coerce_failure_kind(failure_kind)

        now_monotonic = time.monotonic()
        with self._lock:
            if state_text != self._snapshot.get("state") or refresh_deadline:
                self._snapshot["state_since_monotonic"] = now_monotonic
            self._snapshot.update(
                {
                    "state": state_text,
                    "detail": str(detail or "")[-800:],
                    # shutdown 明确标记后，迟到的 callback 不得将其重新变成意外退出。
                    "expected_exit": bool(expected_exit) or bool(
                        self._snapshot.get("expected_exit")
                    ),
                    "updated_monotonic": now_monotonic,
                    "updated_at": time.time(),
                    "unhealthy_after_seconds": timeout,
                    "failure_kind": (
                        normalized_failure_kind.value if normalized_failure_kind else ""
                    ),
                }
            )
            self._write_locked()

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            with self._lock:
                self._snapshot["updated_monotonic"] = time.monotonic()
                self._snapshot["updated_at"] = time.time()
                self._write_locked()

    def _write_locked(self):
        directory = os.path.dirname(self.path)
        for attempt in range(3):
            try:
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(self.tmp_path, "w", encoding="utf-8") as handle:
                    json.dump(
                        self._snapshot,
                        handle,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                os.replace(self.tmp_path, self.path)
                return
            except (OSError, ValueError):
                # Windows 在父进程占用旧文件时可能短暂拒绝 replace；先重试状态写入，再回退到父进程的 stale-heartbeat 恢复。
                try:
                    if os.path.exists(self.tmp_path):
                        os.unlink(self.tmp_path)
                except OSError:
                    pass
                if attempt < 2:
                    time.sleep(0.02)


def start_live_worker_heartbeat() -> bool:
    """进程由 supervisor 启动时开启 worker heartbeat。"""

    global _worker_heartbeat
    if not is_live_worker_process():
        return False

    path = str(os.environ.get(HEARTBEAT_ENV, "") or "").strip()
    if not path:
        return False

    with _heartbeat_lock:
        if _worker_heartbeat is None:
            _worker_heartbeat = _WorkerHeartbeat(path)
            _worker_heartbeat.start()
    return True


def install_live_worker_operator_stop_handler() -> bool:
    """在受监督 worker 中保留 SIGINT，用于 operator 优雅关闭。"""

    global _operator_stop_requested
    if not is_live_worker_process():
        return False

    def _raise_operator_stop(_sig, _frame):
        global _operator_stop_requested
        if _operator_stop_requested:
            return
        _operator_stop_requested = True
        raise LiveWorkerOperatorStop()

    try:
        signal.signal(signal.SIGINT, _raise_operator_stop)
    except (ValueError, OSError):
        return False
    _operator_stop_requested = False
    return True


def stop_live_worker_heartbeat() -> None:
    """停止 heartbeat 线程，主要用于测试和干净关闭。"""

    global _worker_heartbeat
    with _heartbeat_lock:
        heartbeat = _worker_heartbeat
        _worker_heartbeat = None
    if heartbeat is not None:
        heartbeat.stop()


def report_live_worker_state(
    state: str,
    *,
    unhealthy_after_seconds=None,
    detail: Optional[str] = None,
    failure_kind: Optional[LiveWorkerFailureKind] = None,
    refresh_deadline=False,
) -> bool:
    """向父 supervisor 发布短生命周期的 live SDK 健康状态。

    ``unhealthy_after_seconds`` 是状态的有界期限，``None`` 表示下次报告前持续视为健康。``failure_kind`` 是 worker 告警策略的元数据，不从可读的 state/detail 文本推断；该值只属于当前 worker run，不保存订单、持仓或意图。
    """

    heartbeat = _worker_heartbeat
    if heartbeat is None:
        return False
    heartbeat.update(
        state,
        unhealthy_after_seconds=unhealthy_after_seconds,
        detail=detail,
        expected_exit=False,
        failure_kind=failure_kind,
        refresh_deadline=refresh_deadline,
    )
    return True


def mark_live_worker_expected_exit(detail: str = "worker completed normally") -> bool:
    """标记 worker 正常返回，避免父进程重启它。"""

    heartbeat = _worker_heartbeat
    if heartbeat is None:
        return False
    heartbeat.update(
        "worker_expected_exit",
        unhealthy_after_seconds=None,
        detail=detail,
        expected_exit=True,
        failure_kind=None,
    )
    return True


def request_live_worker_restart(
    reason: str,
    *,
    failure_kind: Optional[LiveWorkerFailureKind] = None,
) -> None:
    """请求 supervisor 执行干净的进程重启。

    不经过 supervisor 的直接 adapter 调用仍保留进程内 Phoenix 行为；只有受监督 worker 才以保留退出码退出。``failure_kind`` 与诊断原因分开传递，策略不依赖文本匹配。
    """

    if not is_live_worker_process():
        return

    reason_text = str(reason or "worker requested restart").strip()[-800:]
    heartbeat = _worker_heartbeat
    if heartbeat is not None:
        heartbeat.update(
            "worker_restart_requested",
            unhealthy_after_seconds=None,
            detail=reason_text,
            expected_exit=False,
            failure_kind=failure_kind,
        )
    raise SystemExit(WORKER_RESTART_EXIT_CODE)


def read_live_worker_heartbeat(path: str):
    """读取 heartbeat 快照；读取不完整时返回 ``None``。"""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
        if not isinstance(snapshot, dict):
            return None
        return snapshot
    except (OSError, ValueError, TypeError):
        return None


def evaluate_live_worker_health(
    snapshot,
    now_monotonic: float,
    started_monotonic: float,
    worker_pid=None,
    worker_id=None,
):
    """返回简洁故障原因；worker 健康时返回 ``None``。"""

    if snapshot is None:
        if now_monotonic - started_monotonic >= HEARTBEAT_STALE_SECONDS:
            return "heartbeat missing"
        return None

    snapshot_matches_worker = (
        snapshot.get("worker_id") == worker_id
        if worker_id is not None
        else worker_pid is None or snapshot.get("pid") == worker_pid
    )
    if not snapshot_matches_worker:
        if now_monotonic - started_monotonic >= HEARTBEAT_STALE_SECONDS:
            return "heartbeat belongs to a different worker"
        return None

    try:
        updated = float(snapshot.get("updated_monotonic"))
    except (TypeError, ValueError):
        updated = started_monotonic
    if now_monotonic - updated >= HEARTBEAT_STALE_SECONDS:
        state = str(snapshot.get("state") or "unknown")
        return f"heartbeat stale (state={state})"

    try:
        timeout = float(snapshot.get("unhealthy_after_seconds") or 0.0)
        state_since = float(snapshot.get("state_since_monotonic"))
    except (TypeError, ValueError):
        timeout = 0.0
        state_since = now_monotonic
    if timeout > 0 and now_monotonic - state_since >= timeout:
        state = str(snapshot.get("state") or "unknown")
        detail = str(snapshot.get("detail") or "").strip()
        suffix = f": {detail}" if detail else ""
        return f"health deadline exceeded (state={state}){suffix}"

    return None


def _remove_heartbeat_file(path: str) -> None:
    for candidate in (path, f"{path}.{os.getpid()}.tmp"):
        try:
            if os.path.exists(candidate):
                os.unlink(candidate)
        except OSError:
            pass


def _describe_exit(code) -> str:
    if code is None:
        return "worker did not report an exit code"
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return f"worker exited with code {code}"
    if code_int < 0 and os.name == "posix":
        try:
            signal_name = signal.Signals(-code_int).name
        except (ValueError, TypeError):
            signal_name = f"signal {-code_int}"
        return f"worker terminated by {signal_name} (returncode={code_int})"
    return f"worker exited with code {code_int}"


def _stop_child(process, reason: str, *, operator_stop: bool = False) -> None:
    """停止子进程；native 调用忽略首个信号时逐级升级处理。"""

    try:
        if operator_stop:
            process.send_signal(signal.SIGINT)
        else:
            process.terminate()
    except Exception as exc:
        runtime_print(f"[Supervisor] Failed to signal worker ({reason}): {exc}")
        if operator_stop:
            try:
                process.terminate()
            except Exception as terminate_exc:
                runtime_print(
                    f"[Supervisor] Failed to terminate worker ({reason}): "
                    f"{terminate_exc}"
                )

    grace_seconds = (
        OPERATOR_STOP_GRACE_SECONDS if operator_stop else HEALTH_KILL_GRACE_SECONDS
    )
    deadline = time.monotonic() + grace_seconds
    while True:
        try:
            if process.poll() is not None:
                return
        except Exception:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))

    try:
        process.kill()
    except Exception as exc:
        runtime_print(f"[Supervisor] Failed to kill stuck worker ({reason}): {exc}")
    try:
        process.wait(timeout=2)
    except Exception:
        pass


def supervise_live_process(command: Optional[Iterable[str]] = None) -> int:
    """运行并监督当前 live command，直到正常停止。

    该函数只由非 worker 父进程调用；子进程意外退出、heartbeat 丢失或超过 adapter 健康期限时，将使用原始命令行和有界 backoff 重启。
    """

    if is_live_worker_process():
        raise RuntimeError("A live worker cannot supervise itself.")

    if command is None:
        if not _ORIGINAL_ARGV:
            raise RuntimeError("Cannot supervise live worker without the original command line.")
        worker_command = [sys.executable, "-u", *_ORIGINAL_ARGV]
    else:
        worker_command = [str(item) for item in command]
    heartbeat_path = os.path.join(
        tempfile.gettempdir(),
        f"quantada-live-{os.getpid()}.json",
    )
    stop_event = threading.Event()
    previous_handlers = {}

    def _request_stop(_sig, _frame):
        stop_event.set()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            # 在主线程之外创建的 supervisor 不能安装进程信号处理器；子进程仍可正常接受监控。
            continue

    generation = 0
    consecutive_failures = 0
    previous_failure = ""
    previous_failure_kind = None

    try:
        while not stop_event.is_set():
            generation += 1
            _remove_heartbeat_file(heartbeat_path)
            env = os.environ.copy()
            env[WORKER_ENV] = "1"
            env[HEARTBEAT_ENV] = heartbeat_path
            worker_id = uuid.uuid4().hex
            env[WORKER_ID_ENV] = worker_id
            if previous_failure:
                env[PREVIOUS_FAILURE_ENV] = previous_failure[-800:]
            else:
                env.pop(PREVIOUS_FAILURE_ENV, None)
            if previous_failure_kind is not None:
                env[PREVIOUS_FAILURE_KIND_ENV] = previous_failure_kind.value
            else:
                env.pop(PREVIOUS_FAILURE_KIND_ENV, None)

            popen_kwargs = {"env": env}
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            else:
                creation_flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
                if creation_flags:
                    popen_kwargs["creationflags"] = creation_flags

            try:
                process = subprocess.Popen(worker_command, **popen_kwargs)
            except Exception as exc:
                previous_failure = f"failed to start worker: {exc}"
                previous_failure_kind = None
                runtime_print(f"[Supervisor] {previous_failure}; retrying in 10s.")
                consecutive_failures += 1
                if stop_event.wait(10.0):
                    break
                continue

            started_monotonic = time.monotonic()
            runtime_print(
                f"[Supervisor] Started live worker pid={getattr(process, 'pid', 'unknown')} "
                f"generation={generation}."
            )
            failure_reason = ""
            failure_kind = None
            last_snapshot = None
            last_logged_state = None

            while not stop_event.is_set():
                try:
                    return_code = process.poll()
                except Exception as exc:
                    return_code = None
                    failure_reason = f"worker poll failed: {exc}"

                snapshot = read_live_worker_heartbeat(heartbeat_path)
                if snapshot is not None and snapshot.get("worker_id") != worker_id:
                    # 在替换打开文件存在延迟的平台上，上一代 generation 可能短暂可见。
                    snapshot = None
                if snapshot is not None:
                    last_snapshot = snapshot
                    state = str(snapshot.get("state") or "unknown")
                    if state != last_logged_state:
                        runtime_print(
                            f"[Supervisor] Worker health state={state} "
                            f"heartbeat_pid={snapshot.get('pid')}."
                        )
                        last_logged_state = state

                if return_code is not None:
                    # 短生命周期 worker 可能在写入 expected-exit 与父进程下一次轮询之间退出。
                    # 在将零退出判为意外前，给原子 heartbeat replace 一个有界机会变得可见。
                    if return_code == 0 and not (
                        last_snapshot
                        and last_snapshot.get("worker_id") == worker_id
                        and last_snapshot.get("expected_exit")
                    ):
                        final_read_deadline = (
                            time.monotonic() + EXPECTED_EXIT_VISIBILITY_GRACE_SECONDS
                        )
                        while time.monotonic() < final_read_deadline:
                            final_snapshot = read_live_worker_heartbeat(heartbeat_path)
                            if (
                                final_snapshot is not None
                                and final_snapshot.get("worker_id") == worker_id
                            ):
                                last_snapshot = final_snapshot
                                if final_snapshot.get("expected_exit"):
                                    break
                            time.sleep(0.01)
                    if (
                        last_snapshot
                        and last_snapshot.get("worker_id") == worker_id
                        and last_snapshot.get("expected_exit")
                    ):
                        runtime_print(
                            f"[Supervisor] Worker completed normally (returncode={return_code})."
                        )
                        return int(return_code)

                    detail = ""
                    if last_snapshot:
                        failure_kind = _coerce_failure_kind(
                            last_snapshot.get("failure_kind")
                        )
                        if last_snapshot.get("state") == "worker_restart_requested":
                            detail = str(last_snapshot.get("detail") or "").strip()
                    failure_reason = detail or failure_reason or _describe_exit(return_code)
                    runtime_print(
                        f"[Supervisor] Worker stopped unexpectedly: {failure_reason}."
                    )
                    break

                health_failure = evaluate_live_worker_health(
                    last_snapshot,
                    now_monotonic=time.monotonic(),
                    started_monotonic=started_monotonic,
                    worker_pid=getattr(process, "pid", None),
                    worker_id=worker_id,
                )
                if health_failure:
                    failure_reason = health_failure
                    failure_kind = _coerce_failure_kind(
                        last_snapshot.get("failure_kind") if last_snapshot else None
                    )
                    runtime_print(f"[Supervisor] Worker health failure: {health_failure}.")
                    _stop_child(process, health_failure)
                    try:
                        return_code = process.poll()
                    except Exception:
                        return_code = None
                    runtime_print(
                        f"[Supervisor] Worker terminated after health failure "
                        f"({_describe_exit(return_code)})."
                    )
                    break

                if stop_event.wait(HEALTH_MONITOR_INTERVAL_SECONDS):
                    break

            if stop_event.is_set():
                _stop_child(process, "operator stop", operator_stop=True)
                break

            runtime_seconds = time.monotonic() - started_monotonic
            if runtime_seconds >= STABLE_WORKER_SECONDS:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            previous_failure = str(failure_reason or "worker failure")[-800:]
            previous_failure_kind = failure_kind
            backoff_index = min(consecutive_failures - 1, len(RESTART_BACKOFF_SECONDS) - 1)
            delay = RESTART_BACKOFF_SECONDS[max(0, backoff_index)]
            runtime_print(
                f"[Supervisor] Restarting clean live worker in {delay:.0f}s "
                f"(failure_count={consecutive_failures})."
            )
            if stop_event.wait(delay):
                break
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        _remove_heartbeat_file(heartbeat_path)

    runtime_print("[Supervisor] Live supervisor stopped by operator.")
    return 0
