"""
Process-level supervision for long-running live trading commands.

Broker SDKs are often backed by native code.  A retry loop inside the same
Python process cannot recover from a native ``exit``, a segfault, a deadlock,
or an SDK call that never returns.  This module keeps the supervisor small and
broker-agnostic: the parent process owns restart policy while the child owns
all trading state and SDK objects.

The heartbeat is deliberately a file rather than a socket or a Python queue.
It works for a command started through ``nohup``, survives SDK thread layouts,
and gives the parent a useful diagnostic snapshot when it has to terminate a
stuck worker.
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


# These are intentionally fixed operational safeguards.  They are not trading
# configuration knobs and are kept here so live/backtest configuration cannot
# accidentally alter process recovery semantics.
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
    """Structured failure categories propagated across worker generations."""

    CONNECTIVITY = "connectivity"


class LiveWorkerOperatorStop(BaseException):
    """Escape broker SDK recovery loops for an explicit operator shutdown."""


def _coerce_failure_kind(value) -> Optional[LiveWorkerFailureKind]:
    if isinstance(value, LiveWorkerFailureKind):
        return value
    try:
        return LiveWorkerFailureKind(str(value or "").strip())
    except ValueError:
        return None


def is_live_worker_process() -> bool:
    """Return whether the current process is the supervised trading worker."""

    return os.environ.get(WORKER_ENV, "").strip() == "1"


def get_previous_live_worker_failure() -> str:
    """Return the parent supervisor's last restart reason, if any."""

    return str(os.environ.get(PREVIOUS_FAILURE_ENV, "") or "").strip()


def get_previous_live_worker_failure_kind() -> Optional[LiveWorkerFailureKind]:
    """Return the structured category supplied by the parent supervisor."""

    return _coerce_failure_kind(os.environ.get(PREVIOUS_FAILURE_KIND_ENV))


def is_live_worker_restart() -> bool:
    """Return whether this worker was started after a prior worker failure."""

    return bool(get_previous_live_worker_failure())


class _WorkerHeartbeat:
    """Thread-safe heartbeat snapshot writer used only by a worker process."""

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
                    # Once shutdown is explicitly marked, a late callback
                    # must not turn it back into an unexpected exit.
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
                # Windows can briefly reject replace while the parent has the
                # old file open.  Retry state transitions before falling back
                # to the parent's stale-heartbeat recovery.
                try:
                    if os.path.exists(self.tmp_path):
                        os.unlink(self.tmp_path)
                except OSError:
                    pass
                if attempt < 2:
                    time.sleep(0.02)


def start_live_worker_heartbeat() -> bool:
    """Start the worker heartbeat when the process was launched by the supervisor."""

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
    """Reserve SIGINT for graceful operator shutdown in supervised workers."""

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
    """Stop the heartbeat thread (primarily useful for tests and clean shutdown)."""

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
    """Publish a short-lived live SDK health state to the parent supervisor.

    ``unhealthy_after_seconds`` is a bounded deadline for the state.  Passing
    ``None`` means the state is considered healthy until another report arrives.
    ``failure_kind`` is metadata for the next generation's alarm policy; it is
    never inferred from the human-readable state or detail fields.
    The value is deliberately scoped to the current worker run and never stores
    orders, positions, or trading intent.
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
    """Mark a normal worker return so the parent does not restart it."""

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
    """Ask the supervisor for a clean process restart.

    Direct adapter users (without the supervisor) retain their existing
    in-process Phoenix behavior; only a supervised worker exits with the
    reserved restart code. ``failure_kind`` travels separately from the
    diagnostic reason so policy never depends on text matching.
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
    """Read a heartbeat snapshot, returning ``None`` for an incomplete read."""

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
    """Return a concise failure reason, or ``None`` while the worker is healthy."""

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
    """Stop a child, escalating when a native call ignores the first signal."""

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
    """Run and supervise the current live command until a normal stop.

    The function is called only by the non-worker parent.  A child that exits
    unexpectedly, misses heartbeats, or exceeds an adapter health deadline is
    restarted with the original command line and a bounded backoff.
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
            # Supervisors created outside the main thread cannot install
            # process signal handlers; the child still gets normal monitoring.
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
                    # A previous generation may still be visible briefly on
                    # platforms where replacing an open file is delayed.
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
                    # A short-lived worker can exit between its final
                    # expected-exit write and the parent's next poll.  Give
                    # an atomic heartbeat replace a bounded chance to become
                    # visible before classifying a zero exit as unexpected.
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
