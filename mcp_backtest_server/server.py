import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

SUPPORTED_PROTOCOLS = ("2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "quantada-backtest-mcp", "version": "0.1.0"}


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
        data = p.read_bytes()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:])
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
        return None
    items = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            items.add(part)
    return items


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
    if not allowlist:
        return None
    # 这些内部 flag 始终允许通过。
    allowlist = set(allowlist)
    allowlist.update({"--no_plot"})

    for arg in clean_args:
        if not arg.startswith("-"):
            continue
        flag = arg.split("=", 1)[0]
        if flag not in allowlist:
            return f"flag not allowed: {flag}"
    return None


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
        self._lock = threading.Lock()
        self._cache = {}
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

    def create_job(self, request_id, cmd, workdir, args, max_runtime_sec):
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
            "stdout_path": str(job_dir / "stdout.log"),
            "stderr_path": str(job_dir / "stderr.log"),
            "result_path": str(job_dir / "result.json"),
            "max_runtime_sec": max_runtime_sec,
        }
        with self._lock:
            self._cache[job_id] = job
            self._write_job(job)
        return job

    def update_job(self, job_id, **updates):
        with self._lock:
            job = self._cache.get(job_id)
            if not job:
                job = self.get_job(job_id)
                if not job:
                    return None
            job.update(updates)
            self._cache[job_id] = job
            self._write_job(job)
            return job

    def get_job(self, job_id):
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
        jobs = []
        for job in list(self._cache.values()):
            jobs.append(dict(job))
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
        for job in list(self._cache.values()):
            if job.get("request_id") == request_id:
                return dict(job)
        return None


class BacktestRunner:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._procs = {}

    def start(self, job_id, cmd, workdir, env, max_runtime_sec):
        job = self.store.get_job(job_id)
        if not job:
            raise RuntimeError("job not found")
        stdout_path = Path(job["stdout_path"])
        stderr_path = Path(job["stderr_path"])
        stdout_f = stdout_path.open("wb")
        stderr_f = stderr_path.open("wb")
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            env=env,
            stdout=stdout_f,
            stderr=stderr_f,
        )
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
        if not proc:
            return False
        try:
            grace = max(1, int(grace_sec or 8))
        except Exception:
            grace = 8
        self.store.update_job(job_id, cancel_requested_at=_now_ts())
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=grace)
        except Exception:
            try:
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
                    try:
                        proc.terminate()
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


TOOLS = [
    {
        "name": "backtest_run",
        "description": "Start a backtest/optimizer run via configured CLI runner. Live trading is blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CLI args passed to the runner (strategy name and flags).",
                },
                "request_id": {
                    "type": "string",
                    "description": "Optional client id for dedupe.",
                },
                "allow_plot": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow matplotlib plotting. Default is false (headless).",
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Optional environment overrides.",
                },
                "max_runtime_sec": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional timeout in seconds.",
                },
            },
            "required": ["args"],
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
        self.max_concurrent = _read_int_env("MCP_BACKTEST_MAX_CONCURRENT", 0)
        self._scheduler_interval = max(1, _read_int_env("MCP_BACKTEST_SCHED_INTERVAL_SEC", 1))
        self._scheduler_thread = None
        if self.max_concurrent and self.max_concurrent > 0:
            self._start_scheduler()
        self._initialized = False

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
            except Exception:
                pass
            time.sleep(self._scheduler_interval)

    def _running_count(self):
        with self.runner._lock:
            return len(self.runner._procs)

    def _try_schedule(self):
        if not self.max_concurrent or self.max_concurrent <= 0:
            return
        if self._running_count() >= self.max_concurrent:
            return
        queued = self.store.list_jobs(status="queued", limit=50, offset=0)
        if not queued:
            return
        job = queued[-1]
        env = os.environ.copy()
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
            "cmd": job.get("cmd"),
            "workdir": job.get("workdir"),
            "stdout_path": job.get("stdout_path"),
            "stderr_path": job.get("stderr_path"),
            "result_path": job.get("result_path"),
            "queued_at": job.get("queued_at"),
            "queued_reason": job.get("queued_reason"),
        }

    def _send(self, payload):
        sys.stdout.write(_json_line(payload) + "\n")
        sys.stdout.flush()

    def _send_error(self, msg_id, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"jsonrpc": "2.0", "id": msg_id, "error": err})

    def _tool_text(self, payload, is_error=False):
        return {
            "content": [{"type": "text", "text": _json_line(payload)}],
            "isError": bool(is_error),
        }

    def _handle_initialize(self, msg_id, params):
        version = (params or {}).get("protocolVersion")
        if version and version not in SUPPORTED_PROTOCOLS:
            self._send_error(
                msg_id,
                -32602,
                "Unsupported protocol version",
                {"supported": list(SUPPORTED_PROTOCOLS)},
            )
            return
        self._initialized = True
        self._send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": version or SUPPORTED_PROTOCOLS[0],
                    "serverInfo": SERVER_INFO,
                    "capabilities": {"tools": {"listChanged": False}},
                },
            }
        )

    def _handle_tools_list(self, msg_id):
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})

    def _handle_tool_call(self, msg_id, params):
        if not isinstance(params, dict):
            self._send_error(msg_id, -32602, "Invalid params")
            return
        name = params.get("name")
        args = params.get("arguments", {})
        if name == "backtest_run":
            result = self._tool_backtest_run(args)
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

    def _tool_backtest_run(self, args):
        if not isinstance(args, dict):
            return self._tool_text({"error": "args must be object"}, is_error=True)
        raw_args = args.get("args")
        if not isinstance(raw_args, list) or not raw_args:
            return self._tool_text({"error": "args must be non-empty list"}, is_error=True)
        clean_args = [str(a) for a in raw_args]
        for a in clean_args:
            if a == "--connect" or a.startswith("--connect="):
                return self._tool_text({"error": "live trading (--connect) is not allowed"}, is_error=True)

        allow_plot = bool(args.get("allow_plot", False))
        if not allow_plot and "--no_plot" not in clean_args:
            clean_args.append("--no_plot")

        allowlist = _allowed_flags()
        err = _validate_args(clean_args, allowlist)
        if err:
            return self._tool_text({"error": err}, is_error=True)

        env_overrides = args.get("env") or {}
        if env_overrides and not isinstance(env_overrides, dict):
            return self._tool_text({"error": "env must be object of string values"}, is_error=True)

        request_id = args.get("request_id")
        if not request_id and _auto_dedupe_enabled():
            request_id = _auto_request_id(self.base_cmd, self.workdir, clean_args, env_overrides)
        if request_id:
            existing = self.store.find_by_request_id(request_id)
            if existing:
                return self._tool_text({"job": self._public_job(existing), "deduped": True})

        max_runtime = args.get("max_runtime_sec")
        if max_runtime is not None:
            max_runtime = _safe_int(max_runtime)
            if max_runtime is None or max_runtime <= 0:
                return self._tool_text({"error": "max_runtime_sec must be positive int"}, is_error=True)

        cmd = list(self.base_cmd) + clean_args
        job = self.store.create_job(request_id, cmd, self.workdir, clean_args, max_runtime)

        env = os.environ.copy()
        for k, v in (env_overrides or {}).items():
            env[str(k)] = str(v)

        if self.max_concurrent and self.max_concurrent > 0 and self._running_count() >= self.max_concurrent:
            self.store.update_job(job["id"], queued_reason="max_concurrent")
            return self._tool_text({"job": self._public_job(job), "deduped": False, "queued": True})

        try:
            self.runner.start(job["id"], cmd, self.workdir, env, max_runtime)
        except Exception as exc:
            self.store.update_job(job["id"], status="failed", ended_at=_now_ts(), error=str(exc))
            return self._tool_text({"error": f"runner_start_failed: {exc}", "job": self._public_job(job)}, is_error=True)

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
        include_stdout = bool(args.get("include_stdout", True))
        include_stderr = bool(args.get("include_stderr", True))
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
        return self._tool_text(payload)

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
        ok = self.runner.cancel(job_id, grace_sec=grace)
        job = self.store.get_job(job_id)
        return self._tool_text({"job": self._public_job(job), "canceled": bool(ok)})

    def handle_message(self, msg):
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params")

        if method == "initialize" and msg_id is not None:
            self._handle_initialize(msg_id, params)
            return
        if method == "notifications/initialized":
            self._initialized = True
            return
        if method == "ping" and msg_id is not None:
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            return
        if method == "tools/list" and msg_id is not None:
            self._handle_tools_list(msg_id)
            return
        if method == "tools/call" and msg_id is not None:
            self._handle_tool_call(msg_id, params)
            return
        if msg_id is not None and method:
            self._send_error(msg_id, -32601, f"Method not found: {method}")


def main():
    server = MCPServer()
    _eprint("[mcp] backtest server started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            server.handle_message(msg)
        except Exception as exc:
            _eprint(f"[mcp] handler error: {exc}")


if __name__ == "__main__":
    main()
