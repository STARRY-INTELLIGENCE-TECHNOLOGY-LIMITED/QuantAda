import json
import os
import subprocess
import sys
import threading
import time

import pytest

from mcp_backtest_server.server import (
    ALLOWED_ENV_OVERRIDES,
    DEFAULT_ALLOWED_FLAGS,
    MAX_TOOL_CALLS_PER_MINUTE,
    MAX_RESULT_TAIL_BYTES,
    MCPServer,
    SUPPORTED_PROTOCOLS,
    _tail_text,
    _allowed_flags,
    _validate_args,
)


@pytest.fixture
def mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_BACKTEST_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv(
        "MCP_BACKTEST_CMD_JSON",
        json.dumps([sys.executable, "-c", "pass"]),
    )
    monkeypatch.delenv("MCP_BACKTEST_ALLOWED_FLAGS", raising=False)
    monkeypatch.setenv("MCP_BACKTEST_MAX_CONCURRENT", "0")
    server = MCPServer()
    sent = []
    sent_event = threading.Event()

    def send(payload):
        sent.append(payload)
        sent_event.set()

    server._send = send
    return server, sent, sent_event


def _initialize(server, version="2025-11-25"):
    server.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": version, "capabilities": {}},
    })
    server.handle_message({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })


def _tool_payload(result):
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def _start_task(server, sent, monkeypatch, *, args=None, ttl=60000, request_id=None):
    def start(job_id, cmd, workdir, env, max_runtime_sec):
        server.store.update_job(
            job_id,
            status="running",
            pid=4321,
            started_at=time.time(),
        )
        return 4321

    monkeypatch.setattr(server.runner, "start", start)
    tool_args = {
        "args": args or ["sample_strategy", "--symbols=US.AAPL"],
        "max_runtime_sec": 60,
    }
    if request_id:
        tool_args["request_id"] = request_id
    server.handle_message({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "backtest_run",
            "arguments": tool_args,
            "task": {"ttl": ttl},
        },
    })
    response = sent[-1]
    assert "result" in response
    task = response["result"]["task"]
    return task["taskId"], task


def test_latest_protocol_negotiates_and_declares_task_capabilities(mcp_server):
    server, sent, _ = mcp_server

    _initialize(server, "2025-11-25")

    response = sent[0]["result"]
    assert response["protocolVersion"] == "2025-11-25"
    assert response["capabilities"]["tools"] == {"listChanged": False}
    assert response["capabilities"]["tasks"] == {
        "cancel": {},
        "requests": {"tools": {"call": {}}},
    }


def test_unknown_protocol_version_negotiates_latest_supported_version(mcp_server):
    server, sent, _ = mcp_server

    _initialize(server, "2099-01-01")

    assert sent[0]["result"]["protocolVersion"] == SUPPORTED_PROTOCOLS[0]


def test_legacy_protocol_omits_tasks_and_new_tool_fields(mcp_server):
    server, sent, _ = mcp_server
    _initialize(server, "2024-11-05")

    server.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/list",
        "params": {},
    })

    capabilities = sent[0]["result"]["capabilities"]
    assert "tasks" not in capabilities
    tools = sent[-1]["result"]["tools"]
    assert all("execution" not in tool and "outputSchema" not in tool for tool in tools)


def test_2025_06_protocol_exposes_structured_output_but_not_tasks(mcp_server):
    server, sent, _ = mcp_server
    _initialize(server, "2025-06-18")
    server.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
    })

    assert "tasks" not in sent[0]["result"]["capabilities"]
    tools = sent[-1]["result"]["tools"]
    assert all("outputSchema" in tool for tool in tools)
    assert all("execution" not in tool for tool in tools)


def test_current_protocol_tool_results_include_structured_content(mcp_server):
    server, sent, _ = mcp_server
    _initialize(server)
    server.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "backtest_status",
            "arguments": {"job_id": "missing"},
        },
    })

    result = sent[-1]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "job not found"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_tool_calls_are_rate_limited(mcp_server):
    server, sent, _ = mcp_server
    _initialize(server)
    now = time.monotonic()
    with server._tool_rate_lock:
        server._tool_call_times.extend([now - 0.1] * MAX_TOOL_CALLS_PER_MINUTE)

    server.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "backtest_status", "arguments": {"job_id": "missing"}},
    })

    assert sent[-1]["result"]["isError"] is True
    assert "rate limit" in sent[-1]["result"]["structuredContent"]["error"]


def test_legacy_protocol_ignores_task_metadata_and_uses_normal_tool_result(
    mcp_server, monkeypatch,
):
    server, sent, _ = mcp_server
    _initialize(server, "2024-11-05")

    def start(job_id, cmd, workdir, env, max_runtime_sec):
        server.store.update_job(job_id, status="running", pid=1, started_at=time.time())
        return 1

    monkeypatch.setattr(server.runner, "start", start)
    server.handle_message({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "backtest_run",
            "arguments": {"args": ["sample_strategy"]},
            "task": {"ttl": 60000},
        },
    })

    assert "content" in sent[-1]["result"]
    assert "task" not in sent[-1]["result"]
    assert server.store._tasks == {}


def test_task_augmented_backtest_creates_working_task_and_persists_it(
    mcp_server, monkeypatch, tmp_path,
):
    server, sent, _ = mcp_server
    _initialize(server)

    task_id, task = _start_task(server, sent, monkeypatch)

    assert task["status"] == "working"
    assert task["ttl"] == 60000
    assert task["pollInterval"] == 1000
    assert task["createdAt"].endswith("Z")
    assert task["lastUpdatedAt"].endswith("Z")
    stored = server.store.get_task(task_id)
    assert stored["status"] == "working"
    assert task["jobId"] == stored["job_id"]
    task_file = tmp_path / "jobs" / stored["job_id"] / "tasks" / f"{task_id}.json"
    assert task_file.is_file()
    related = sent[-1]["result"]["_meta"]["io.modelcontextprotocol/related-task"]
    assert related["taskId"] == task_id
    assert related["jobId"] == stored["job_id"]


def test_concurrency_limit_queues_jobs_without_starting_extra_processes(
    mcp_server, monkeypatch,
):
    server, sent, _ = mcp_server
    _initialize(server)
    server.max_concurrent = 1
    started = []

    def start(job_id, cmd, workdir, env, max_runtime_sec):
        started.append(job_id)
        server.store.update_job(job_id, status="running", pid=len(started), started_at=time.time())
        return len(started)

    monkeypatch.setattr(server.runner, "start", start)
    monkeypatch.setattr(server, "_running_count", lambda: len(started))
    for request_id in ("first", "second"):
        server.handle_message({
            "jsonrpc": "2.0",
            "id": len(started) + 2,
            "method": "tools/call",
            "params": {
                "name": "backtest_run",
                "arguments": {
                    "args": ["sample_strategy"],
                    "request_id": request_id,
                },
            },
        })

    jobs = server.store.list_jobs(limit=10)
    assert len(started) == 1
    assert len(jobs) == 2
    assert "queued" in {job["status"] for job in jobs}


def test_tasks_get_and_result_follow_job_lifecycle(mcp_server, monkeypatch):
    server, sent, event = mcp_server
    _initialize(server)
    task_id, _ = _start_task(server, sent, monkeypatch)
    stored_task = server.store.get_task(task_id)
    job = server.store.get_job(stored_task["job_id"])
    with open(job["result_path"], "w", encoding="utf-8") as handle:
        json.dump({"status": "done", "exit_code": 0}, handle)

    server.store.update_job(
        job["id"],
        status="done",
        exit_code=0,
        ended_at=time.time(),
    )
    server.handle_message({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tasks/get",
        "params": {"taskId": task_id},
    })
    assert sent[-1]["result"]["status"] == "completed"

    event.clear()
    server.handle_message({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tasks/result",
        "params": {"taskId": task_id},
    })
    assert event.wait(2)
    result = sent[-1]
    assert result["result"]["structuredContent"]["job"]["status"] == "done"
    related = result["result"]["_meta"]["io.modelcontextprotocol/related-task"]
    assert related["taskId"] == task_id
    assert related["jobId"] == job["id"]


def test_tasks_result_waits_for_a_terminal_state(mcp_server, monkeypatch):
    server, sent, event = mcp_server
    _initialize(server)
    task_id, _ = _start_task(server, sent, monkeypatch)
    job = server.store.get_job(server.store.get_task(task_id)["job_id"])
    job["result_path"] = str(job["result_path"])
    event.clear()

    server.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tasks/result",
        "params": {"taskId": task_id},
    })
    assert not event.wait(0.05)

    with open(job["result_path"], "w", encoding="utf-8") as handle:
        json.dump({"status": "done"}, handle)
    server.store.update_job(job["id"], status="done", exit_code=0, ended_at=time.time())

    assert event.wait(2)
    assert sent[-1]["id"] == 3
    assert "result" in sent[-1]


def test_cancel_uses_stored_pid_when_runner_handle_is_missing(mcp_server, monkeypatch):
    server, sent, _ = mcp_server
    _initialize(server)
    killed = []
    monkeypatch.setattr(
        server.runner,
        "_terminate_pid_tree",
        lambda pid, grace_sec=8: killed.append(int(pid)) or True,
    )
    task_id, task = _start_task(server, sent, monkeypatch)
    job_id = task["jobId"]
    assert job_id not in server.runner._procs

    server.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tasks/cancel",
        "params": {"taskId": task_id},
    })

    assert killed == [4321]
    assert sent[-1]["result"]["status"] == "cancelled"
    assert server.store.get_job(job_id)["status"] == "canceled"


def test_deduped_task_on_finished_job_returns_terminal_status(mcp_server, monkeypatch):
    server, sent, _ = mcp_server
    _initialize(server)
    task_id, task = _start_task(server, sent, monkeypatch, request_id="same-run")
    job_id = task["jobId"]
    server.store.update_job(job_id, status="done", exit_code=0, ended_at=time.time())

    server.handle_message({
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {
            "name": "backtest_run",
            "arguments": {
                "args": ["sample_strategy", "--symbols=US.AAPL"],
                "request_id": "same-run",
            },
            "task": {"ttl": 60000},
        },
    })

    created = sent[-1]["result"]["task"]
    assert created["jobId"] == job_id
    assert created["status"] == "completed"
    assert created["taskId"] != task_id


def test_task_cancellation_is_terminal_and_rejects_second_cancel(mcp_server, monkeypatch):
    server, sent, _ = mcp_server
    _initialize(server)
    task_id, _ = _start_task(server, sent, monkeypatch)
    job = server.store.get_job(server.store.get_task(task_id)["job_id"])

    def cancel(job_id, grace_sec=8):
        server.store.update_job(
            job_id, status="canceled", ended_at=time.time(), error="canceled",
        )
        return True

    monkeypatch.setattr(server.runner, "cancel", cancel)
    server.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tasks/cancel",
        "params": {"taskId": task_id},
    })
    assert sent[-1]["result"]["status"] == "cancelled"

    server.handle_message({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tasks/cancel",
        "params": {"taskId": task_id},
    })
    assert sent[-1]["error"]["code"] == -32602
    assert job["id"] == server.store.get_task(task_id)["job_id"]


def test_task_metadata_survives_server_restart(mcp_server, monkeypatch, tmp_path):
    server, sent, _ = mcp_server
    _initialize(server)
    task_id, _ = _start_task(server, sent, monkeypatch)

    restarted = MCPServer()
    task = restarted.store.get_task(task_id)

    assert task is not None
    assert task["status"] == "failed"
    assert restarted.store.get_job(task["job_id"])["status"] == "failed"


def test_restart_fails_queued_jobs(mcp_server):
    server, _, _ = mcp_server
    job = server.store.create_job(None, [sys.executable], os.getcwd(), ["sample_strategy"], 1)
    assert job["status"] == "queued"

    restarted = MCPServer()
    recovered = restarted.store.get_job(job["id"])

    assert recovered["status"] == "failed"
    assert "not recoverable" in recovered["error"]


def test_task_ttl_is_clamped_and_expired_tasks_are_removed(mcp_server):
    server, _, _ = mcp_server
    job = server.store.create_job(None, [sys.executable], os.getcwd(), [], 1)
    task = server.store.create_task(job["id"], 1)
    assert task["ttl_ms"] == 1
    time.sleep(0.01)
    assert server.store.get_task(task["task_id"]) is None


def test_allowlist_can_only_narrow_built_in_cli_flags(monkeypatch):
    monkeypatch.setenv("MCP_BACKTEST_ALLOWED_FLAGS", "--cash,--n_jobs,--connect,--new-unsafe")

    allowlist = _allowed_flags()

    assert "--cash" in allowlist
    assert "--n_jobs" in allowlist
    assert "--no_plot" in allowlist
    assert "--connect" not in allowlist
    assert "--new-unsafe" not in allowlist
    assert allowlist.issubset(DEFAULT_ALLOWED_FLAGS)
    assert _validate_args(["sample_strategy", "--n_jobs", "-1"], allowlist) is None
    assert _validate_args(["sample_strategy", "--cash", "-100"], allowlist) is None


@pytest.mark.parametrize(
    "args",
    [
        ["sample_strategy", "--connect=gm_broker:real"],
        ["sample_strategy", "--config", "{'DATA_PATH': 'elsewhere'}"],
        ["sample_strategy", "--train_kill"],
        ["sample_strategy", "--some-unreviewed-flag"],
    ],
)
def test_default_flag_policy_rejects_unsafe_or_unknown_flags(mcp_server, args):
    server, _, _ = mcp_server

    result = server._tool_backtest_run({"args": args})

    assert result["isError"] is True
    assert "error" in _tool_payload(result)


def test_value_flag_cannot_consume_another_flag_as_its_value(mcp_server):
    server, _, _ = mcp_server

    result = server._tool_backtest_run({
        "args": ["sample_strategy", "--cash", "--config", "{'DATA_PATH': 'elsewhere'}"],
    })

    assert result["isError"] is True
    assert "--config" in _tool_payload(result)["error"]


def test_per_request_environment_overrides_are_restricted(mcp_server):
    server, _, _ = mcp_server

    result = server._tool_backtest_run({
        "args": ["sample_strategy"],
        "env": {"GM_TOKEN": "secret"},
    })

    assert result["isError"] is True
    assert "GM_TOKEN" in _tool_payload(result)["invalid_keys"]
    assert "OMP_NUM_THREADS" in ALLOWED_ENV_OVERRIDES


@pytest.mark.parametrize(
    "env",
    [
        {"OMP_NUM_THREADS": "0"},
        {"OMP_NUM_THREADS": "999999"},
        {"PYTHONUTF8": "yes"},
        {"PYTHONIOENCODING": "latin-1"},
    ],
)
def test_per_request_environment_override_values_are_validated(mcp_server, env):
    server, _, _ = mcp_server

    result = server._tool_backtest_run({"args": ["sample_strategy"], "env": env})

    assert result["isError"] is True


def test_job_lookup_rejects_path_like_identifiers(mcp_server):
    server, _, _ = mcp_server

    assert server.store.get_job("../../README.md") is None
    assert server.store.get_job("not-a-job-id") is None


def test_result_log_tail_has_byte_and_line_bounds(tmp_path):
    log = tmp_path / "large.log"
    log.write_bytes(b"x" * (MAX_RESULT_TAIL_BYTES + 32) + b"\nlast line\n")

    tail = _tail_text(log, 5000)

    assert tail == "last line"


def test_public_job_metadata_does_not_return_local_paths(mcp_server):
    server, _, _ = mcp_server
    job = server.store.create_job(
        None, [sys.executable, "run.py"], os.getcwd(), ["strategy"], 10,
    )

    public = server._public_job(job)

    assert "cmd" not in public
    assert "workdir" not in public
    assert "stdout_path" not in public
    assert "stderr_path" not in public
    assert "result_path" not in public


def test_invalid_cursor_and_uninitialized_requests_use_protocol_errors(mcp_server):
    server, sent, _ = mcp_server

    server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    assert sent[-1]["error"]["code"] == -32002

    fresh = MCPServer()
    fresh_sent = []
    fresh._send = fresh_sent.append
    _initialize(fresh)
    fresh.handle_message({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {"cursor": "not-a-valid-cursor"},
    })
    assert fresh_sent[-1]["error"]["code"] == -32602


def test_initialize_must_be_the_first_request(mcp_server):
    server, sent, _ = mcp_server

    server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
    })
    assert sent[-1]["error"]["code"] == -32002

    server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25"},
    })
    assert sent[-1]["error"]["code"] == -32600


def test_task_listing_is_not_exposed_without_requestor_identity(mcp_server):
    server, sent, _ = mcp_server
    _initialize(server)
    server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tasks/list", "params": {},
    })

    assert sent[-1]["error"]["code"] == -32601


def test_stdio_reports_parse_errors_on_protocol_stdout(tmp_path):
    env = os.environ.copy()
    env["MCP_BACKTEST_DATA_DIR"] = str(tmp_path / "jobs")
    env["MCP_BACKTEST_CMD_JSON"] = json.dumps([sys.executable, "-c", "pass"])
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_backtest_server"],
        input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\nnot-json\n',
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )

    messages = [json.loads(line) for line in proc.stdout.splitlines()]
    assert proc.returncode == 0
    assert messages[0]["result"]["protocolVersion"] == "2025-11-25"
    assert messages[1]["error"]["code"] == -32700
