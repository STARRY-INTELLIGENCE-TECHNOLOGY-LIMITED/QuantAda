import base64
import hashlib
import json
import re
import os
import signal
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
STRUCTURED_TOOL_PROTOCOLS = {"2025-11-25", "2025-06-18"}
SERVER_INFO = {"name": "quantada-backtest-mcp", "version": "0.1.0"}
DEFAULT_ALLOWED_FLAGS = {
    "--symbols", "--cash", "--commission", "--slippage",
    "--start_date", "--end_date", "--timeframe", "--compression",
    "--params", "--risk", "--risk_params", "--selection", "--data_source",
    "--no_plot", "--plot_scope", "--opt_params", "--n_trials", "--n_jobs",
    "--metric", "--train_roll_period", "--test_roll_period", "--train_ratio",
    "--train_period", "--test_period", "--execution_price", "--refresh",
    "--fetch_only", "--desc",
}
FORBIDDEN_FLAGS = {
    "--connect", "--force_schedule", "--train_resume", "--train_kill",
    "--ui", "--ui_ip", "--ui_port", "--config",
}
BOOLEAN_FLAGS = {"--no_plot", "--refresh", "--fetch_only"}
VALUE_FLAGS = DEFAULT_ALLOWED_FLAGS - BOOLEAN_FLAGS
ALLOWED_ENV_OVERRIDES = {
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "PYTHONIOENCODING", "PYTHONUTF8",
}
MAX_QUEUED_JOBS = 100
MAX_TOOL_CALLS_PER_MINUTE = 300
DEFAULT_TASK_TTL_MS = 24 * 60 * 60 * 1000
MAX_TASK_TTL_MS = 7 * DEFAULT_TASK_TTL_MS
TASK_POLL_INTERVAL_MS = 1000
MAX_RESULT_TAIL_LINES = 2000
MAX_RESULT_TAIL_BYTES = 1024 * 1024


def _eprint(*args):
    print(*args, file=sys.stderr, flush=True)


def _now_ts():
    return time.time()


def _json_line(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _safe_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def _tail_text(path, max_lines):
    try:
        if max_lines <= 0:
            return ""
        p = Path(path)
        if not p.exists():
            return ""
        size = p.stat().st_size
        start = max(0, size - MAX_RESULT_TAIL_BYTES)
        with p.open("rb") as handle:
            handle.seek(start)
            data = handle.read(MAX_RESULT_TAIL_BYTES)
        if start:
            first_line = data.find(b"\n")
            data = data[first_line + 1:] if first_line >= 0 else b""
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-min(int(max_lines), MAX_RESULT_TAIL_LINES):])
    except Exception as exc:
        return f"[tail_error] {exc}"


def _repo_root():
    return Path(__file__).resolve().parents[1]


def _resolve_workdir(repo_root):
    env_dir = os.getenv("MCP_BACKTEST_WORKDIR", "").strip()
    if env_dir:
        return env_dir
    return str(repo_root)


def _resolve_base_command(repo_root):
    cmd_json = os.getenv("MCP_BACKTEST_CMD_JSON", "").strip()
    if cmd_json:
        parsed = json.loads(cmd_json)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("MCP_BACKTEST_CMD_JSON must be a non-empty JSON array")
        return [str(x) for x in parsed]

    cmd_str = os.getenv("MCP_BACKTEST_CMD", "").strip()
    if cmd_str:
        return shlex.split(cmd_str, posix=(os.name != "nt"))

    default_run = repo_root / "run.py"
    if not default_run.exists():
        raise FileNotFoundError("Default runner not found: run.py")
    return [sys.executable, str(default_run)]


def _allowed_flags():
    raw = os.getenv("MCP_BACKTEST_ALLOWED_FLAGS", "").strip()
    if not raw:
        return set(DEFAULT_ALLOWED_FLAGS)
    items = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            items.add(part)
    return items.intersection(DEFAULT_ALLOWED_FLAGS) | {"--no_plot"}


def _auto_dedupe_enabled():
    val = os.getenv("MCP_BACKTEST_DEDUPE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _auto_request_id(base_cmd, workdir, args, env_overrides):
    payload = {
        "cmd": list(base_cmd),
        "workdir": str(workdir),
        "args": list(args),
        "env": env_overrides or {},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "auto:" + hashlib.sha256(raw).hexdigest()


def _validate_args(clean_args, allowlist):
    allowlist = set(allowlist or ())
    expects_value = False
    for arg in clean_args:
        if expects_value:
            if arg.startswith("-") and not re.fullmatch(
                r"-(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", arg,
            ):
                flag = arg.split("=", 1)[0]
                if flag in FORBIDDEN_FLAGS:
                    return f"flag is forbidden for the backtest MCP server: {flag}"
                return f"CLI option before {flag} is missing its value"
            expects_value = False
            continue
        if not arg.startswith("-"):
            continue
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            return f"flag is forbidden for the backtest MCP server: {flag}"
        if flag not in allowlist:
            return f"flag not allowed: {flag}"
        if "=" not in arg and flag in VALUE_FLAGS:
            expects_value = True
    if expects_value:
        return "CLI option is missing its value"
    return None


def _utc_iso(timestamp):
    """把 Unix 时间戳格式化成 MCP Task 使用的 UTC 时间。"""
    if timestamp is None:
        timestamp = _now_ts()
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _normalize_task_ttl(value):
    if value is None:
        return DEFAULT_TASK_TTL_MS
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("task.ttl must be a positive integer in milliseconds")
    return min(value, MAX_TASK_TTL_MS)


def _encode_cursor(scope, offset):
    payload = _json_line({"scope": scope, "offset": int(offset)}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor, scope):
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor or len(cursor) > 256:
        raise ValueError("invalid cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        offset = payload.get("offset")
        if payload.get("scope") != scope or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("invalid cursor")
        return offset
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def _read_int_env(name, default_val):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default_val
    try:
        return int(raw)
    except Exception:
        return default_val


class JobStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._cache = {}
        self._tasks = {}
        self._load_existing()

    def _load_existing(self):
        for job_dir in self.data_dir.iterdir():
            meta = job_dir / "job.json"
            if not meta.exists():
                continue
            try:
                job = json.loads(meta.read_text(encoding="utf-8"))
                if isinstance(job, dict) and "id" in job:
                    self._cache[job["id"]] = job
                    task_dir = job_dir / "tasks"
                    if task_dir.is_dir():
                        for task_path in task_dir.glob("*.json"):
                            try:
                                task = json.loads(task_path.read_text(encoding="utf-8"))
                                if isinstance(task, dict) and task.get("task_id"):
                                    self._tasks[task["task_id"]] = task
                            except (OSError, ValueError, TypeError):
                                continue
            except Exception:
                continue

    def _job_dir(self, job_id):
        return self.data_dir / job_id

    def _meta_path(self, job_id):
        return self._job_dir(job_id) / "job.json"

    def _write_job(self, job):
        job_id = job["id"]
        meta = self._meta_path(job_id)
        tmp = meta.with_suffix(".tmp")
        tmp.write_text(_json_line(job), encoding="utf-8")
        os.replace(tmp, meta)

    def create_job(self, request_id, cmd, workdir, args, max_runtime_sec, env_overrides=None):
        job_id = uuid.uuid4().hex
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        now = _now_ts()
        job = {
            "id": job_id,
            "request_id": request_id,
            "status": "queued",
            "created_at": now,
            "started_at": None,
            "ended_at": None,
            "exit_code": None,
            "error": None,
            "pid": None,
            "cancel_requested_at": None,
            "queued_at": now,
            "queued_reason": None,
            "cmd": list(cmd),
            "args": list(args),
            "workdir": workdir,
            "env_overrides": dict(env_overrides or {}),
            "stdout_path": str(job_dir / "stdout.log"),
            "stderr_path": str(job_dir / "stderr.log"),
            "result_path": str(job_dir / "result.json"),
            "max_runtime_sec": max_runtime_sec,
        }
        with self._lock:
            self._cache[job_id] = job
            self._write_job(job)
        return job

    def create_task(self, job_id, ttl_ms):
        with self._changed:
            if job_id not in self._cache:
                return None
            now = _now_ts()
            task_id = uuid.uuid4().hex
            task = {
                "task_id": task_id,
                "job_id": job_id,
                "status": "working",
                "status_message": "Backtest job accepted.",
                "created_at": now,
                "last_updated_at": now,
                "ttl_ms": int(ttl_ms),
            }
            self._tasks[task_id] = task
            task_path = self._job_dir(job_id) / "tasks" / f"{task_id}.json"
            task_path.parent.mkdir(parents=True, exist_ok=True)
            self._sync_task_locked(task, self._cache[job_id])
            self._write_task(task_path, task)
            self._changed.notify_all()
            return dict(task)

    @staticmethod
    def _write_task(path, task):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_json_line(task), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _mapped_task_status(job):
        return {
            "queued": ("working", "Backtest job is queued."),
            "running": ("working", "Backtest job is running."),
            "done": ("completed", "Backtest job completed successfully."),
            "failed": ("failed", job.get("error") or "Backtest job failed."),
            "canceled": ("cancelled", "Backtest job was cancelled."),
        }.get(job.get("status"), ("failed", "Backtest job has an unknown status."))

    def _sync_task_locked(self, task, job):
        if task.get("status") in {"completed", "failed", "cancelled"}:
            return
        status, message = self._mapped_task_status(job)
        if task.get("status") == status and task.get("status_message") == message:
            return
        task["status"] = status
        task["status_message"] = message
        task["last_updated_at"] = _now_ts()
        task_path = self._job_dir(task["job_id"]) / "tasks" / f"{task['task_id']}.json"
        self._write_task(task_path, task)

    def _sync_job_tasks_locked(self, job_id, job):
        for task in self._tasks.values():
            if task.get("job_id") == job_id:
                self._sync_task_locked(task, job)

    def get_task(self, task_id):
        with self._changed:
            task = self._tasks.get(str(task_id))
            if not task:
                return None
            expires_at = float(task["created_at"]) + int(task.get("ttl_ms") or 0) / 1000.0
            if _now_ts() >= expires_at:
                self._tasks.pop(str(task_id), None)
                path = self._job_dir(task["job_id"]) / "tasks" / f"{task_id}.json"
                try:
                    path.unlink()
                except OSError:
                    pass
                self._changed.notify_all()
                return None
            job = self._cache.get(task["job_id"])
            if job:
                self._sync_task_locked(task, job)
            return dict(task)

    def update_task(self, task_id, status, status_message=None):
        with self._changed:
            task = self._tasks.get(str(task_id))
            if not task:
                return None
            if task.get("status") in {"completed", "failed", "cancelled"}:
                return dict(task)
            task["status"] = status
            task["status_message"] = status_message
            task["last_updated_at"] = _now_ts()
            task_path = self._job_dir(task["job_id"]) / "tasks" / f"{task_id}.json"
            self._write_task(task_path, task)
            self._changed.notify_all()
            return dict(task)

    def wait_for_task_terminal(self, task_id, timeout):
        deadline = _now_ts() + max(0.0, float(timeout))
        with self._changed:
            while True:
                task = self._tasks.get(str(task_id))
                if task is None:
                    return None
                job = self._cache.get(task["job_id"])
                if job:
                    self._sync_task_locked(task, job)
                if task.get("status") in {"completed", "failed", "cancelled"}:
                    return dict(task)
                expires_at = float(task["created_at"]) + int(task.get("ttl_ms") or 0) / 1000.0
                remaining = min(deadline - _now_ts(), expires_at - _now_ts())
                if remaining <= 0:
                    return self.get_task(task_id)
                self._changed.wait(timeout=min(remaining, 1.0))

    def update_job(self, job_id, **updates):
        with self._changed:
            job = self._cache.get(job_id)
            if not job:
                job = self.get_job(job_id)
                if not job:
                    return None
            job.update(updates)
            job["last_updated_at"] = _now_ts()
            self._cache[job_id] = job
            self._write_job(job)
            self._sync_job_tasks_locked(job_id, job)
            self._changed.notify_all()
            return job

    def get_job(self, job_id):
        if (
            not isinstance(job_id, str)
            or len(job_id) != 32
            or any(char not in "0123456789abcdef" for char in job_id.lower())
        ):
            return None
        job_id = job_id.lower()
        with self._lock:
            cached = self._cache.get(job_id)
            if cached:
                return dict(cached)
        meta = self._meta_path(job_id)
        if not meta.exists():
            return None
        try:
            job = json.loads(meta.read_text(encoding="utf-8"))
            if isinstance(job, dict):
                with self._lock:
                    self._cache[job_id] = job
                return dict(job)
        except Exception:
            return None
        return None

    def list_jobs(self, status=None, limit=50, offset=0):
        with self._lock:
            jobs = [dict(job) for job in self._cache.values()]
        jobs.sort(key=lambda j: j.get("created_at") or 0, reverse=True)
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        if offset < 0:
            offset = 0
        if limit is None or limit <= 0:
            return jobs[offset:]
        return jobs[offset:offset + limit]

    def find_by_request_id(self, request_id):
        if not request_id:
            return None
        with self._lock:
            for job in self._cache.values():
                if job.get("request_id") == request_id:
                    return dict(job)
        return None


class BacktestRunner:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._procs = {}

    @staticmethod
    def _terminate_pid_tree(pid, grace_sec=8):
        """终止 CLI 及其训练 worker；回测优化不能留下孤儿进程。"""
        try:
            pid = int(pid)
        except (TypeError, ValueError, OverflowError):
            return False
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=max(5, int(grace_sec) + 5),
                )
                return result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                return False
        return True

    def recover_existing_jobs(self):
        """新 Server 不接管旧进程；queued 与 running 一律失败并清理进程树。"""
        for status in ("running", "queued"):
            for job in self.store.list_jobs(status=status, limit=None, offset=0):
                pid = job.get("pid")
                if pid:
                    self._terminate_pid_tree(pid, grace_sec=5)
                self.store.update_job(
                    job["id"],
                    status="failed",
                    ended_at=_now_ts(),
                    error="MCP server restarted; the previous job was not recoverable.",
                )

    def start(self, job_id, cmd, workdir, env, max_runtime_sec):
        job = self.store.get_job(job_id)
        if not job:
            raise RuntimeError("job not found")
        stdout_path = Path(job["stdout_path"])
        stderr_path = Path(job["stderr_path"])
        stdout_f = stdout_path.open("wb")
        stderr_f = stderr_path.open("wb")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                **(
                    {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                    if os.name == "nt"
                    else {"start_new_session": True}
                ),
            )
        except Exception:
            stdout_f.close()
            stderr_f.close()
            raise
        with self._lock:
            self._procs[job_id] = proc
        self.store.update_job(
            job_id,
            status="running",
            pid=proc.pid,
            started_at=_now_ts(),
            queued_reason=None,
        )
        t = threading.Thread(
            target=self._wait_proc,
            args=(job_id, proc, stdout_f, stderr_f, max_runtime_sec),
            daemon=True,
        )
        t.start()
        return proc.pid

    def cancel(self, job_id, grace_sec=8):
        with self._lock:
            proc = self._procs.get(job_id)
        job = self.store.get_job(job_id)
        try:
            grace = max(1, int(grace_sec or 8))
        except (TypeError, ValueError):
            grace = 8
        pid = getattr(proc, "pid", None)
        if pid is None and job:
            pid = job.get("pid")
        if pid is None:
            return False
        self.store.update_job(job_id, cancel_requested_at=_now_ts())
        self._terminate_pid_tree(pid, grace_sec=grace)
        if proc is None:
            return True
        try:
            proc.wait(timeout=grace)
        except Exception:
            try:
                self._terminate_pid_tree(proc.pid, grace_sec=2)
                proc.kill()
            except Exception:
                return False
        return True

    def _wait_proc(self, job_id, proc, stdout_f, stderr_f, max_runtime_sec):
        exit_code = None
        error = None
        try:
            if max_runtime_sec:
                try:
                    exit_code = proc.wait(timeout=max_runtime_sec)
                except subprocess.TimeoutExpired:
                    error = "timeout"
                    self._terminate_pid_tree(proc.pid, grace_sec=10)
                    try:
                        exit_code = proc.wait(timeout=10)
                    except Exception:
                        try:
                            proc.kill()
                            exit_code = proc.wait(timeout=10)
                        except Exception as exc:
                            error = f"timeout_kill_failed: {exc}"
            else:
                exit_code = proc.wait()
        except Exception as exc:
            error = f"runner_exception: {exc}"
        finally:
            try:
                stdout_f.close()
            except Exception:
                pass
            try:
                stderr_f.close()
            except Exception:
                pass

        with self._lock:
            self._procs.pop(job_id, None)

        if exit_code is None:
            exit_code = -1
        status = "done" if exit_code == 0 and not error else "failed"
        job = self.store.get_job(job_id)
        if job and job.get("cancel_requested_at"):
            status = "canceled"
            if not error:
                error = "canceled"
        self.store.update_job(
            job_id,
            status=status,
            ended_at=_now_ts(),
            exit_code=exit_code,
            error=error,
        )
        job = self.store.get_job(job_id)
        if job:
            try:
                result = {
                    "id": job.get("id"),
                    "status": job.get("status"),
                    "exit_code": job.get("exit_code"),
                    "error": job.get("error"),
                    "created_at": job.get("created_at"),
                    "started_at": job.get("started_at"),
                    "ended_at": job.get("ended_at"),
                    "duration_sec": None,
                }
                if job.get("started_at") and job.get("ended_at"):
                    result["duration_sec"] = max(0.0, job["ended_at"] - job["started_at"])
                Path(job["result_path"]).write_text(_json_line(result), encoding="utf-8")
            except Exception:
                pass

    def shutdown(self):
        """Server 退出时清理所有仍运行的 CLI 进程。"""
        with self._lock:
            job_ids = list(self._procs)
        for job_id in job_ids:
            self.cancel(job_id, grace_sec=5)


OUTPUT_SCHEMA = {"type": "object", "additionalProperties": True}

TOOLS = [
    {
        "name": "backtest_run",
        "description": "Start a backtest or optimizer job via the configured CLI runner. Live trading and framework/configuration control flags are blocked. Supports native MCP Tasks for long-running jobs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Strategy name and allowlisted run.py flags. --connect, --config, UI, train-resume, and train-kill flags are rejected.",
                },
                "request_id": {
                    "type": "string",
                    "description": "Optional request identifier used to deduplicate matching jobs.",
                },
                "allow_plot": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow matplotlib plotting. Default is false (headless).",
                },
                "env": {
                    "type": "object",
                    "properties": {name: {"type": "string"} for name in sorted(ALLOWED_ENV_OVERRIDES)},
                    "additionalProperties": False,
                    "description": "Optional process-tuning environment overrides. Credentials and arbitrary environment variables are not accepted.",
                },
                "max_runtime_sec": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional timeout in seconds.",
                },
            },
            "required": ["args"],
        },
        "outputSchema": OUTPUT_SCHEMA,
        "execution": {"taskSupport": "optional"},
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "backtest_status",
        "description": "Get status for a job id.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        "outputSchema": OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "backtest_result",
        "description": "Fetch result logs and metadata for a job id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 200},
                "include_stdout": {"type": "boolean", "default": True},
                "include_stderr": {"type": "boolean", "default": True},
            },
            "required": ["job_id"],
        },
        "outputSchema": OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "backtest_cancel",
        "description": "Cancel a running backtest job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "grace_sec": {"type": "integer", "default": 8},
            },
            "required": ["job_id"],
        },
        "outputSchema": OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "backtest_list",
        "description": "List recent jobs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "offset": {"type": "integer", "default": 0},
            },
        },
        "outputSchema": OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


class MCPServer:
    def __init__(self):
        repo_root = _repo_root()
        self.workdir = _resolve_workdir(repo_root)
        self.base_cmd = _resolve_base_command(repo_root)
        data_dir = os.getenv("MCP_BACKTEST_DATA_DIR", "").strip()
        if not data_dir:
            data_dir = str(repo_root / ".data" / "mcp_backtest_jobs")
        self.store = JobStore(data_dir)
        self.runner = BacktestRunner(self.store)
        self.runner.recover_existing_jobs()
        self.max_concurrent = _read_int_env("MCP_BACKTEST_MAX_CONCURRENT", 1)
        if self.max_concurrent < 0:
            self.max_concurrent = 1
        self._scheduler_interval = max(1, _read_int_env("MCP_BACKTEST_SCHED_INTERVAL_SEC", 1))
        self._scheduler_thread = None
        self._schedule_lock = threading.Lock()
        if self.max_concurrent and self.max_concurrent > 0:
            self._start_scheduler()
        self._initialized = False
        self._protocol_version = None
        self._received_message = False
        self._first_method = None
        self._send_lock = threading.Lock()
        self._task_result_slots = threading.BoundedSemaphore(16)
        self._tool_rate_lock = threading.Lock()
        self._tool_call_times = deque()

    def _start_scheduler(self):
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        t = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread = t
        t.start()

    def _scheduler_loop(self):
        while True:
            try:
                self._try_schedule()
            except Exception as exc:
                _eprint(f"[mcp] scheduler error: {exc}")
            time.sleep(self._scheduler_interval)

    def _running_count(self):
        with self.runner._lock:
            return len(self.runner._procs)

    def _try_schedule(self):
        if not self.max_concurrent or self.max_concurrent <= 0:
            return
        with self._schedule_lock:
            if self._running_count() >= self.max_concurrent:
                return
            queued = self.store.list_jobs(status="queued", limit=50, offset=0)
            if not queued:
                return
            job = queued[-1]
            env = os.environ.copy()
            env.update(job.get("env_overrides") or {})
            try:
                self.runner.start(job["id"], job["cmd"], job["workdir"], env, job.get("max_runtime_sec"))
            except Exception as exc:
                self.store.update_job(job["id"], status="failed", ended_at=_now_ts(), error=str(exc))

    def _public_job(self, job):
        if not job:
            return None
        return {
            "id": job.get("id"),
            "request_id": job.get("request_id"),
            "status": job.get("status"),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "ended_at": job.get("ended_at"),
            "exit_code": job.get("exit_code"),
            "error": job.get("error"),
            "queued_at": job.get("queued_at"),
            "queued_reason": job.get("queued_reason"),
        }

    def _send(self, payload):
        with self._send_lock:
            sys.stdout.write(_json_line(payload) + "\n")
            sys.stdout.flush()

    def _send_error(self, msg_id, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"jsonrpc": "2.0", "id": msg_id, "error": err})

    def _tool_text(self, payload, is_error=False):
        result = {
            "content": [{"type": "text", "text": _json_line(payload)}],
            "isError": bool(is_error),
        }
        if self._protocol_version in STRUCTURED_TOOL_PROTOCOLS:
            result["structuredContent"] = payload
        return result

    def _handle_initialize(self, msg_id, params):
        if self._protocol_version is not None:
            self._send_error(msg_id, -32600, "initialize has already been completed")
            return
        if not isinstance(params, dict) or not isinstance(params.get("protocolVersion"), str):
            self._send_error(msg_id, -32602, "initialize requires a string protocolVersion")
            return
        requested = params["protocolVersion"]
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        self._protocol_version = version
        capabilities = {"tools": {"listChanged": False}}
        if version == "2025-11-25":
            capabilities["tasks"] = {
                "cancel": {},
                "requests": {"tools": {"call": {}}},
            }
        self._send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": version,
                    "serverInfo": SERVER_INFO,
                    "capabilities": capabilities,
                },
            }
        )

    def _handle_tools_list(self, msg_id, params=None):
        if params is None:
            params = {}
        if not isinstance(params, dict):
            self._send_error(msg_id, -32602, "Invalid params")
            return
        try:
            offset = _decode_cursor(params.get("cursor"), "tools")
        except ValueError as exc:
            self._send_error(msg_id, -32602, str(exc))
            return
        page = TOOLS[offset:offset + 100]
        if self._protocol_version != "2025-11-25":
            page = [
                {key: value for key, value in tool.items() if key != "execution"}
                for tool in page
            ]
        if self._protocol_version not in STRUCTURED_TOOL_PROTOCOLS:
            page = [
                {key: value for key, value in tool.items() if key != "outputSchema"}
                for tool in page
            ]
        result = {"tools": page}
        next_offset = offset + len(page)
        if next_offset < len(TOOLS):
            result["nextCursor"] = _encode_cursor("tools", next_offset)
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _handle_tool_call(self, msg_id, params):
        if not isinstance(params, dict):
            self._send_error(msg_id, -32602, "Invalid params")
            return
        now = time.monotonic()
        with self._tool_rate_lock:
            while self._tool_call_times and now - self._tool_call_times[0] >= 60.0:
                self._tool_call_times.popleft()
            if len(self._tool_call_times) >= MAX_TOOL_CALLS_PER_MINUTE:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": self._tool_text(
                            {"error": "MCP tool call rate limit exceeded; retry shortly."},
                            is_error=True,
                        ),
                    }
                )
                return
            self._tool_call_times.append(now)
        name = params.get("name")
        args = params.get("arguments", {})
        task_meta = params.get("task")
        latest_tasks = self._protocol_version == "2025-11-25"
        if "task" in params and latest_tasks:
            if name != "backtest_run":
                self._send_error(msg_id, -32601, "This tool does not support task execution")
                return
            if not isinstance(task_meta, dict):
                self._send_error(msg_id, -32602, "task must be an object")
                return
            try:
                task_ttl_ms = _normalize_task_ttl(task_meta.get("ttl"))
            except ValueError as exc:
                self._send_error(msg_id, -32602, str(exc))
                return
        else:
            task_ttl_ms = None
        if name == "backtest_run":
            result = self._tool_backtest_run(args, task_ttl_ms=task_ttl_ms)
            if isinstance(result, dict) and "_created_task" in result:
                task = result["_created_task"]
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "task": task,
                            "_meta": {
                                "io.modelcontextprotocol/related-task": {
                                    "taskId": task["taskId"],
                                    "jobId": task["jobId"],
                                }
                            },
                        },
                    }
                )
            else:
                self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
            return
        if name == "backtest_status":
            result = self._tool_backtest_status(args)
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
            return
        if name == "backtest_result":
            result = self._tool_backtest_result(args)
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
            return
        if name == "backtest_list":
            result = self._tool_backtest_list(args)
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
            return
        if name == "backtest_cancel":
            result = self._tool_backtest_cancel(args)
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
            return
        self._send_error(msg_id, -32602, f"Unknown tool: {name}")

    def _task_object(self, task):
        if not task:
            return None
        return {
            "taskId": task["task_id"],
            "jobId": task["job_id"],
            "status": task["status"],
            "statusMessage": task.get("status_message"),
            "createdAt": _utc_iso(task.get("created_at")),
            "lastUpdatedAt": _utc_iso(task.get("last_updated_at")),
            "ttl": int(task.get("ttl_ms") or DEFAULT_TASK_TTL_MS),
            "pollInterval": TASK_POLL_INTERVAL_MS,
        }

    def _tool_backtest_run(self, args, task_ttl_ms=None):
        if not isinstance(args, dict):
            return self._tool_text({"error": "args must be object"}, is_error=True)
        raw_args = args.get("args")
        if (
            not isinstance(raw_args, list)
            or not raw_args
            or any(not isinstance(item, str) for item in raw_args)
        ):
            return self._tool_text({"error": "args must be a non-empty array of strings"}, is_error=True)
        clean_args = list(raw_args)
        for a in clean_args:
            if a == "--connect" or a.startswith("--connect="):
                return self._tool_text({"error": "live trading (--connect) is not allowed"}, is_error=True)

        allow_plot = args.get("allow_plot", False)
        if not isinstance(allow_plot, bool):
            return self._tool_text({"error": "allow_plot must be a boolean"}, is_error=True)
        if not allow_plot and "--no_plot" not in clean_args:
            clean_args.append("--no_plot")

        allowlist = _allowed_flags()
        err = _validate_args(clean_args, allowlist)
        if err:
            return self._tool_text({"error": err}, is_error=True)

        env_overrides = args.get("env", {})
        if not isinstance(env_overrides, dict):
            return self._tool_text({"error": "env must be an object"}, is_error=True)
        invalid_env = [
            name
            for name, value in env_overrides.items()
            if not isinstance(name, str)
            or name not in ALLOWED_ENV_OVERRIDES
            or not isinstance(value, str)
            or (
                name in {"OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"}
                and (not value.isdecimal() or len(value) > 3 or not 1 <= int(value) <= 256)
            )
            or (name == "PYTHONUTF8" and value not in {"0", "1"})
            or (
                name == "PYTHONIOENCODING"
                and value.lower() not in {"utf-8", "utf8", "utf-8:strict", "utf8:strict"}
            )
        ]
        if invalid_env:
            return self._tool_text(
                {
                    "error": "environment override is not allowed",
                    "invalid_keys": [str(name) for name in invalid_env],
                    "allowed_keys": sorted(ALLOWED_ENV_OVERRIDES),
                },
                is_error=True,
            )

        request_id = args.get("request_id")
        if request_id is not None and not isinstance(request_id, str):
            return self._tool_text({"error": "request_id must be a string"}, is_error=True)
        if not request_id and _auto_dedupe_enabled():
            request_id = _auto_request_id(self.base_cmd, self.workdir, clean_args, env_overrides)
        if request_id:
            existing = self.store.find_by_request_id(request_id)
            if existing:
                if task_ttl_ms is not None:
                    task = self.store.create_task(existing["id"], task_ttl_ms)
                    return {"_created_task": self._task_object(task)}
                return self._tool_text({"job": self._public_job(existing), "deduped": True})

        max_runtime = args.get("max_runtime_sec")
        if max_runtime is not None:
            if isinstance(max_runtime, bool) or not isinstance(max_runtime, int) or max_runtime <= 0:
                return self._tool_text({"error": "max_runtime_sec must be positive int"}, is_error=True)

        with self._schedule_lock:
            queue_jobs = self.store.list_jobs(status="queued", limit=MAX_QUEUED_JOBS + 1)
            should_queue = bool(
                self.max_concurrent > 0
                and (self._running_count() >= self.max_concurrent or queue_jobs)
            )
            if should_queue and len(queue_jobs) >= MAX_QUEUED_JOBS:
                return self._tool_text({"error": "job queue is full"}, is_error=True)

            cmd = list(self.base_cmd) + clean_args
            job = self.store.create_job(
                request_id,
                cmd,
                self.workdir,
                clean_args,
                max_runtime,
                env_overrides=env_overrides,
            )
            env = os.environ.copy()
            env.update(env_overrides)

            if should_queue:
                self.store.update_job(job["id"], queued_reason="max_concurrent")
                if task_ttl_ms is not None:
                    task = self.store.create_task(job["id"], task_ttl_ms)
                    return {"_created_task": self._task_object(task)}
                return self._tool_text({"job": self._public_job(job), "deduped": False, "queued": True})

            try:
                self.runner.start(job["id"], cmd, self.workdir, env, max_runtime)
            except Exception as exc:
                self.store.update_job(job["id"], status="failed", ended_at=_now_ts(), error=str(exc))
                return self._tool_text({"error": f"runner_start_failed: {exc}", "job": self._public_job(job)}, is_error=True)

            if task_ttl_ms is not None:
                task = self.store.create_task(job["id"], task_ttl_ms)
                return {"_created_task": self._task_object(task)}
            return self._tool_text({"job": self._public_job(job), "deduped": False})

    def _tool_backtest_status(self, args):
        if not isinstance(args, dict):
            return self._tool_text({"error": "args must be object"}, is_error=True)
        job_id = args.get("job_id")
        if not job_id:
            return self._tool_text({"error": "job_id required"}, is_error=True)
        job = self.store.get_job(job_id)
        if not job:
            return self._tool_text({"error": "job not found"}, is_error=True)
        return self._tool_text({"job": self._public_job(job)})

    def _tool_backtest_result(self, args):
        if not isinstance(args, dict):
            return self._tool_text({"error": "args must be object"}, is_error=True)
        job_id = args.get("job_id")
        if not job_id:
            return self._tool_text({"error": "job_id required"}, is_error=True)
        job = self.store.get_job(job_id)
        if not job:
            return self._tool_text({"error": "job not found"}, is_error=True)
        tail_lines = _safe_int(args.get("tail_lines", 200), 200)
        if tail_lines is None or tail_lines < 0:
            tail_lines = 200
        tail_lines = min(tail_lines, MAX_RESULT_TAIL_LINES)
        include_stdout = args.get("include_stdout", True)
        include_stderr = args.get("include_stderr", True)
        if not isinstance(include_stdout, bool) or not isinstance(include_stderr, bool):
            return self._tool_text(
                {"error": "include_stdout and include_stderr must be booleans"},
                is_error=True,
            )
        payload = {"job": self._public_job(job)}
        try:
            result_path = job.get("result_path")
            if result_path:
                p = Path(result_path)
                if p.exists():
                    payload["result_summary"] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        if include_stdout:
            payload["stdout_tail"] = _tail_text(job.get("stdout_path"), tail_lines)
        if include_stderr:
            payload["stderr_tail"] = _tail_text(job.get("stderr_path"), tail_lines)
        return self._tool_text(
            payload,
            is_error=job.get("status") in {"failed", "canceled"},
        )

    def _tool_backtest_list(self, args):
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return self._tool_text({"error": "args must be object"}, is_error=True)
        status = args.get("status")
        limit = _safe_int(args.get("limit", 20), 20)
        offset = _safe_int(args.get("offset", 0), 0)
        if limit is None or limit <= 0:
            limit = 20
        if limit > 200:
            limit = 200
        if offset is None or offset < 0:
            offset = 0
        jobs = self.store.list_jobs(status=status, limit=limit, offset=offset)
        return self._tool_text({"jobs": [self._public_job(j) for j in jobs]})

    def _tool_backtest_cancel(self, args):
        if not isinstance(args, dict):
            return self._tool_text({"error": "args must be object"}, is_error=True)
        job_id = args.get("job_id")
        if not job_id:
            return self._tool_text({"error": "job_id required"}, is_error=True)
        with self._schedule_lock:
            job = self.store.get_job(job_id)
            if not job:
                return self._tool_text({"error": "job not found"}, is_error=True)
            if job.get("status") == "queued":
                self.store.update_job(
                    job_id,
                    status="canceled",
                    cancel_requested_at=_now_ts(),
                    ended_at=_now_ts(),
                    error="canceled",
                )
                job = self.store.get_job(job_id)
                return self._tool_text({"job": self._public_job(job), "canceled": True})
            if job.get("status") not in ("running",):
                return self._tool_text({"job": self._public_job(job), "canceled": False, "reason": "not_running"})
            grace = _safe_int(args.get("grace_sec", 8), 8)
            grace = min(30, max(1, grace if grace is not None else 8))
            ok = self.runner.cancel(job_id, grace_sec=grace)
            job = self.store.get_job(job_id)
            return self._tool_text({"job": self._public_job(job), "canceled": bool(ok)})

    def _task_get(self, msg_id, params):
        if self._protocol_version != "2025-11-25":
            self._send_error(msg_id, -32601, "Method not found: tasks/get")
            return
        if not isinstance(params, dict) or not isinstance(params.get("taskId"), str):
            self._send_error(msg_id, -32602, "taskId must be a string")
            return
        task = self.store.get_task(params["taskId"])
        if not task:
            self._send_error(msg_id, -32602, "task not found or expired")
            return
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": self._task_object(task)})

    def _task_cancel(self, msg_id, params):
        if not isinstance(params, dict) or not isinstance(params.get("taskId"), str):
            self._send_error(msg_id, -32602, "taskId must be a string")
            return
        task = self.store.get_task(params["taskId"])
        if not task:
            self._send_error(msg_id, -32602, "task not found or expired")
            return
        if task["status"] in {"completed", "failed", "cancelled"}:
            self._send_error(msg_id, -32602, "task is already in a terminal status")
            return

        with self._schedule_lock:
            job = self.store.get_job(task["job_id"])
            if not job:
                self.store.update_task(task["task_id"], "failed", "Backtest job record is unavailable.")
            elif job.get("status") == "queued":
                self.store.update_job(
                    job["id"],
                    status="canceled",
                    cancel_requested_at=_now_ts(),
                    ended_at=_now_ts(),
                    error="canceled",
                )
            elif job.get("status") == "running":
                try:
                    self.runner.cancel(job["id"], grace_sec=8)
                except Exception as exc:
                    _eprint(f"[mcp] task cancellation error for {task['task_id']}: {exc}")
                current = self.store.get_job(job["id"])
                if current and current.get("status") == "running":
                    self.store.update_job(
                        job["id"],
                        status="canceled",
                        ended_at=_now_ts(),
                        error="canceled",
                    )
                self.store.update_task(task["task_id"], "cancelled", "The task was cancelled by request.")
            else:
                self.store.update_task(task["task_id"], "cancelled", "The task was cancelled by request.")

        task = self.store.get_task(params["taskId"])
        if task is None:
            self._send_error(msg_id, -32602, "task not found or expired")
            return
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": self._task_object(task)})

    def _task_result_worker(self, msg_id, task_id):
        try:
            task = self.store.get_task(task_id)
            if task is None:
                self._send_error(msg_id, -32602, "task not found or expired")
                return
            ttl_seconds = int(task.get("ttl_ms") or DEFAULT_TASK_TTL_MS) / 1000.0
            task = self.store.wait_for_task_terminal(task_id, timeout=ttl_seconds)
            if task is None:
                self._send_error(msg_id, -32602, "task not found or expired")
                return
            job = self.store.get_job(task["job_id"])
            if not job:
                self._send_error(msg_id, -32602, "task result is unavailable")
                return
            result = self._tool_backtest_result(
                {
                    "job_id": task["job_id"],
                    "tail_lines": 200,
                    "include_stdout": True,
                    "include_stderr": True,
                }
            )
            if task["status"] == "cancelled":
                result["isError"] = True
                result["structuredContent"]["task_status"] = "cancelled"
                result["content"][0]["text"] = _json_line(result["structuredContent"])
            result["_meta"] = {
                "io.modelcontextprotocol/related-task": {
                    "taskId": task_id,
                    "jobId": task["job_id"],
                }
            }
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except Exception as exc:
            self._send_error(msg_id, -32603, f"task result failed: {exc}")
        finally:
            self._task_result_slots.release()

    def _start_task_result(self, msg_id, params):
        if not isinstance(params, dict) or not isinstance(params.get("taskId"), str):
            self._send_error(msg_id, -32602, "taskId must be a string")
            return
        if not self._task_result_slots.acquire(blocking=False):
            self._send_error(msg_id, -32000, "too many task result requests are waiting")
            return
        threading.Thread(
            target=self._task_result_worker,
            args=(msg_id, params["taskId"]),
            daemon=True,
        ).start()

    def handle_message(self, msg):
        if not isinstance(msg, dict):
            self._send_error(None, -32600, "Invalid Request")
            return
        if msg.get("jsonrpc") != "2.0":
            self._send_error(msg.get("id"), -32600, "Invalid Request: jsonrpc must be 2.0")
            return
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params")
        if not isinstance(method, str) or not method:
            self._send_error(msg_id, -32600, "Invalid Request: method must be a string")
            return
        if "id" in msg and msg_id is None:
            self._send_error(None, -32600, "Invalid Request: id must not be null")
            return
        if msg_id is not None and (
            isinstance(msg_id, bool) or not isinstance(msg_id, (str, int))
        ):
            self._send_error(None, -32600, "Invalid Request: id must be a string or integer")
            return

        if self._first_method is None:
            self._first_method = method
        if self._received_message and self._protocol_version is None and self._first_method != "initialize":
            if msg_id is not None:
                self._send_error(msg_id, -32600, "initialize must be the first request")
            return
        self._received_message = True

        if method == "initialize" and msg_id is not None:
            self._handle_initialize(msg_id, params)
            return
        if method == "notifications/initialized":
            if msg_id is not None:
                self._send_error(msg_id, -32600, "notifications/initialized must not include an id")
                return
            if self._protocol_version is not None:
                self._initialized = True
            return
        if msg_id is None:
            # 未消费的 MCP notification 不需要响应。
            return
        if self._protocol_version is None or not self._initialized:
            self._send_error(msg_id, -32002, "Server not initialized")
            return
        if method == "ping" and msg_id is not None:
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            return
        if method == "tools/list" and msg_id is not None:
            self._handle_tools_list(msg_id, params)
            return
        if method == "tools/call" and msg_id is not None:
            self._handle_tool_call(msg_id, params)
            return
        if method == "tasks/get":
            if self._protocol_version != "2025-11-25":
                self._send_error(msg_id, -32601, "Method not found: tasks/get")
                return
            self._task_get(msg_id, params)
            return
        if method == "tasks/list":
            self._send_error(msg_id, -32601, "Method not found: tasks/list")
            return
        if method == "tasks/cancel":
            if self._protocol_version != "2025-11-25":
                self._send_error(msg_id, -32601, "Method not found: tasks/cancel")
                return
            self._task_cancel(msg_id, params)
            return
        if method == "tasks/result":
            if self._protocol_version != "2025-11-25":
                self._send_error(msg_id, -32601, "Method not found: tasks/result")
                return
            self._start_task_result(msg_id, params)
            return
        self._send_error(msg_id, -32601, f"Method not found: {method}")


def main():
    server = MCPServer()
    _eprint("[mcp] backtest server started")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                server._send_error(None, -32700, "Parse error", {"detail": str(exc)})
                continue
            try:
                server.handle_message(msg)
            except Exception as exc:
                _eprint(f"[mcp] handler error: {exc}")
                if isinstance(msg, dict) and msg.get("id") is not None:
                    server._send_error(msg.get("id"), -32603, "Internal error")
    finally:
        server.runner.shutdown()


if __name__ == "__main__":
    main()
