import os
import sys
import textwrap

import pytest

import common.live_process_supervisor as supervisor


def _worker_script(body):
    preamble = textwrap.dedent(
        f"""
        import os
        import sys
        import time
        from pathlib import Path
        sys.path.insert(0, {os.getcwd()!r})
        from common.live_process_supervisor import (
            LiveWorkerFailureKind,
            get_previous_live_worker_failure_kind,
            mark_live_worker_expected_exit,
            report_live_worker_state,
            request_live_worker_restart,
            start_live_worker_heartbeat,
            stop_live_worker_heartbeat,
        )
        start_live_worker_heartbeat()
        """
    )
    return preamble + "\n" + textwrap.dedent(body)


def test_evaluate_live_worker_health_detects_stale_and_deadline():
    stale = {
        "pid": 999,
        "worker_id": "worker-1",
        "state": "running",
        "updated_monotonic": 0.0,
        "state_since_monotonic": 0.0,
        "unhealthy_after_seconds": None,
    }
    assert "heartbeat stale" in supervisor.evaluate_live_worker_health(
        stale,
        now_monotonic=supervisor.HEARTBEAT_STALE_SECONDS + 1,
        started_monotonic=0.0,
        worker_pid=10,
        worker_id="worker-1",
    )

    unhealthy = {
        "pid": 10,
        "worker_id": "worker-1",
        "state": "sdk_initializing",
        "detail": "gmi_init has not completed",
        "updated_monotonic": 10.0,
        "state_since_monotonic": 0.0,
        "unhealthy_after_seconds": 30.0,
    }
    reason = supervisor.evaluate_live_worker_health(
        unhealthy,
        now_monotonic=31.0,
        started_monotonic=0.0,
        worker_pid=10,
        worker_id="worker-1",
    )
    assert "health deadline exceeded" in reason
    assert "gmi_init has not completed" in reason

    expected_but_stale = {
        "pid": 10,
        "worker_id": "worker-1",
        "state": "worker_expected_exit",
        "expected_exit": True,
        "updated_monotonic": 0.0,
        "state_since_monotonic": 0.0,
        "unhealthy_after_seconds": None,
    }
    assert "heartbeat stale" in supervisor.evaluate_live_worker_health(
        expected_but_stale,
        now_monotonic=supervisor.HEARTBEAT_STALE_SECONDS + 1,
        started_monotonic=0.0,
        worker_pid=10,
        worker_id="worker-1",
    )

    starting = {
        "pid": 10,
        "worker_id": "worker-1",
        "state": "worker_starting",
        "updated_monotonic": supervisor.WORKER_STARTUP_DEADLINE_SECONDS,
        "state_since_monotonic": 0.0,
        "unhealthy_after_seconds": supervisor.WORKER_STARTUP_DEADLINE_SECONDS,
    }
    assert "health deadline exceeded" in supervisor.evaluate_live_worker_health(
        starting,
        now_monotonic=supervisor.WORKER_STARTUP_DEADLINE_SECONDS + 1,
        started_monotonic=0.0,
        worker_pid=10,
        worker_id="worker-1",
    )


def test_previous_worker_failure_kind_is_structured(monkeypatch):
    monkeypatch.setenv(
        supervisor.PREVIOUS_FAILURE_KIND_ENV,
        supervisor.LiveWorkerFailureKind.CONNECTIVITY.value,
    )
    assert (
        supervisor.get_previous_live_worker_failure_kind()
        is supervisor.LiveWorkerFailureKind.CONNECTIVITY
    )

    monkeypatch.setenv(supervisor.PREVIOUS_FAILURE_KIND_ENV, "gm_sdk_initializing")
    assert supervisor.get_previous_live_worker_failure_kind() is None


def test_worker_heartbeat_publishes_state_and_expected_exit(monkeypatch, tmp_path):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setenv(supervisor.WORKER_ENV, "1")
    monkeypatch.setenv(supervisor.HEARTBEAT_ENV, str(path))
    supervisor.stop_live_worker_heartbeat()

    try:
        assert supervisor.start_live_worker_heartbeat() is True
        initial = supervisor.read_live_worker_heartbeat(str(path))
        assert initial["state"] == "worker_starting"
        assert initial["unhealthy_after_seconds"] == pytest.approx(
            supervisor.WORKER_STARTUP_DEADLINE_SECONDS
        )
        assert supervisor.report_live_worker_state(
            "sdk_initializing",
            unhealthy_after_seconds=12,
            detail="initializing",
            failure_kind=supervisor.LiveWorkerFailureKind.CONNECTIVITY,
        )
        snapshot = supervisor.read_live_worker_heartbeat(str(path))
        assert snapshot["state"] == "sdk_initializing"
        assert snapshot["worker_id"] == ""
        assert snapshot["unhealthy_after_seconds"] == pytest.approx(12.0)
        assert snapshot["expected_exit"] is False
        assert snapshot["failure_kind"] == "connectivity"

        assert supervisor.mark_live_worker_expected_exit("test complete")
        snapshot = supervisor.read_live_worker_heartbeat(str(path))
        assert snapshot["expected_exit"] is True
        assert snapshot["state"] == "worker_expected_exit"
        assert snapshot["failure_kind"] == ""
    finally:
        supervisor.stop_live_worker_heartbeat()


def test_worker_health_report_refreshes_deadline_only_when_requested(monkeypatch, tmp_path):
    now = {"value": 10.0}
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now["value"])
    heartbeat = supervisor._WorkerHeartbeat(str(tmp_path / "heartbeat.json"))

    now["value"] = 20.0
    heartbeat.update("worker_starting", unhealthy_after_seconds=30)
    assert heartbeat._snapshot["state_since_monotonic"] == pytest.approx(10.0)

    now["value"] = 30.0
    heartbeat.update(
        "worker_starting",
        unhealthy_after_seconds=30,
        refresh_deadline=True,
    )
    assert heartbeat._snapshot["state_since_monotonic"] == pytest.approx(30.0)


def test_operator_stop_handler_is_installed_only_for_supervised_worker(monkeypatch):
    registered = []
    monkeypatch.delenv(supervisor.WORKER_ENV, raising=False)
    monkeypatch.setattr(
        supervisor.signal,
        "signal",
        lambda sig, handler: registered.append((sig, handler)),
    )

    assert supervisor.install_live_worker_operator_stop_handler() is False
    assert registered == []

    monkeypatch.setenv(supervisor.WORKER_ENV, "1")
    assert supervisor.install_live_worker_operator_stop_handler() is True
    assert len(registered) == 1
    sig, handler = registered[0]
    assert sig == supervisor.signal.SIGINT

    with pytest.raises(supervisor.LiveWorkerOperatorStop):
        handler(sig, None)
    handler(sig, None)


def test_operator_stop_signals_child_with_sigint(monkeypatch):
    calls = []

    class Process:
        def send_signal(self, sig):
            calls.append(("signal", sig))

        def terminate(self):
            calls.append(("terminate", None))

        def poll(self):
            return 0

    supervisor._stop_child(Process(), "operator stop", operator_stop=True)

    assert calls == [("signal", supervisor.signal.SIGINT)]


def test_health_stop_uses_terminate_instead_of_operator_signal():
    calls = []

    class Process:
        def send_signal(self, sig):
            calls.append(("signal", sig))

        def terminate(self):
            calls.append(("terminate", None))

        def poll(self):
            return 0

    supervisor._stop_child(Process(), "health deadline")

    assert calls == [("terminate", None)]


def test_operator_stop_allows_longer_grace_for_im_delivery(monkeypatch):
    elapsed = {"value": 0.0}

    class Process:
        def __init__(self):
            self.calls = []

        def send_signal(self, sig):
            self.calls.append(("signal", sig))

        def terminate(self):
            self.calls.append(("terminate", None))

        def poll(self):
            return None

        def kill(self):
            self.calls.append(("kill", None))

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(supervisor, "OPERATOR_STOP_GRACE_SECONDS", 3.0)
    monkeypatch.setattr(supervisor, "HEALTH_KILL_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: elapsed["value"])
    monkeypatch.setattr(
        supervisor.time,
        "sleep",
        lambda seconds: elapsed.__setitem__("value", elapsed["value"] + seconds),
    )

    operator_process = Process()
    supervisor._stop_child(operator_process, "operator stop", operator_stop=True)
    operator_elapsed = elapsed["value"]

    elapsed["value"] = 0.0
    health_process = Process()
    supervisor._stop_child(health_process, "health deadline")

    assert operator_elapsed == pytest.approx(3.0)
    assert elapsed["value"] == pytest.approx(1.0)
    assert operator_process.calls[0] == ("signal", supervisor.signal.SIGINT)
    assert health_process.calls[0] == ("terminate", None)


def test_supervisor_restarts_unexpected_worker_and_accepts_expected_exit(monkeypatch, tmp_path):
    counter = tmp_path / "starts.txt"
    script = _worker_script(
        f"""
        counter = Path({str(counter)!r})
        count = int(counter.read_text() or "0") if counter.exists() else 0
        counter.write_text(str(count + 1))
        if count == 0:
            report_live_worker_state(
                "opaque_failure_state",
                failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
            )
            os._exit(23)
        assert get_previous_live_worker_failure_kind() is LiveWorkerFailureKind.CONNECTIVITY
        mark_live_worker_expected_exit("test worker finished")
        time.sleep(0.1)
        """
    )
    monkeypatch.setattr(supervisor, "RESTART_BACKOFF_SECONDS", (0.0,))
    monkeypatch.setattr(supervisor, "HEALTH_MONITOR_INTERVAL_SECONDS", 0.02)

    result = supervisor.supervise_live_process(
        [sys.executable, "-u", "-c", script]
    )

    assert result == 0
    assert counter.read_text() == "2"


def test_supervisor_propagates_requested_failure_kind(monkeypatch, tmp_path):
    counter = tmp_path / "requested-restart-starts.txt"
    script = _worker_script(
        f"""
        counter = Path({str(counter)!r})
        count = int(counter.read_text() or "0") if counter.exists() else 0
        counter.write_text(str(count + 1))
        if count == 0:
            request_live_worker_restart(
                "opaque connectivity detail",
                failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
            )
        assert get_previous_live_worker_failure_kind() is LiveWorkerFailureKind.CONNECTIVITY
        mark_live_worker_expected_exit("requested restart recovery finished")
        time.sleep(0.1)
        """
    )
    monkeypatch.setattr(supervisor, "RESTART_BACKOFF_SECONDS", (0.0,))
    monkeypatch.setattr(supervisor, "HEALTH_MONITOR_INTERVAL_SECONDS", 0.02)

    result = supervisor.supervise_live_process(
        [sys.executable, "-u", "-c", script]
    )

    assert result == 0
    assert counter.read_text() == "2"


def test_supervisor_restarts_worker_after_health_deadline(monkeypatch, tmp_path):
    counter = tmp_path / "health-starts.txt"
    script = _worker_script(
        f"""
        counter = Path({str(counter)!r})
        count = int(counter.read_text() or "0") if counter.exists() else 0
        counter.write_text(str(count + 1))
        if count == 0:
            report_live_worker_state(
                "sdk_initializing",
                unhealthy_after_seconds=0.2,
                detail="test timeout",
                failure_kind=LiveWorkerFailureKind.CONNECTIVITY,
            )
            time.sleep(30)
        assert get_previous_live_worker_failure_kind() is LiveWorkerFailureKind.CONNECTIVITY
        mark_live_worker_expected_exit("test recovery worker finished")
        time.sleep(0.1)
        """
    )
    monkeypatch.setattr(supervisor, "RESTART_BACKOFF_SECONDS", (0.0,))
    monkeypatch.setattr(supervisor, "HEALTH_MONITOR_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(supervisor, "HEALTH_KILL_GRACE_SECONDS", 0.05)

    result = supervisor.supervise_live_process(
        [sys.executable, "-u", "-c", script]
    )

    assert result == 0
    assert counter.read_text() == "2"


def test_supervisor_restarts_worker_after_stale_heartbeat(monkeypatch, tmp_path):
    counter = tmp_path / "stale-starts.txt"
    script = _worker_script(
        f"""
        counter = Path({str(counter)!r})
        count = int(counter.read_text() or "0") if counter.exists() else 0
        counter.write_text(str(count + 1))
        if count == 0:
            stop_live_worker_heartbeat()
            time.sleep(30)
        mark_live_worker_expected_exit("test stale-heartbeat recovery finished")
        time.sleep(0.1)
        """
    )
    monkeypatch.setattr(supervisor, "RESTART_BACKOFF_SECONDS", (0.0,))
    monkeypatch.setattr(supervisor, "HEARTBEAT_STALE_SECONDS", 0.2)
    monkeypatch.setattr(supervisor, "HEALTH_MONITOR_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(supervisor, "HEALTH_KILL_GRACE_SECONDS", 0.05)

    result = supervisor.supervise_live_process(
        [sys.executable, "-u", "-c", script]
    )

    assert result == 0
    assert counter.read_text() == "2"


def test_run_main_prints_config_override_values_directly(monkeypatch, capsys):
    import run

    monkeypatch.setattr(sys, "argv", [
        "run.py",
        "dummy_strategy",
        "--config",
        "{'GM_TOKEN':'secret','BROKER_ENVIRONMENTS': {'gm_broker': {'real': {'token': 'secret'}}}}",
    ])
    monkeypatch.setattr(run.config, "GM_TOKEN", "original-token", raising=False)
    monkeypatch.setattr(run.config, "BROKER_ENVIRONMENTS", {}, raising=False)
    monkeypatch.setattr(run.config, "DB_ENABLED", False, raising=False)
    monkeypatch.setattr(run.config, "HTTP_LOG_URL", None, raising=False)
    monkeypatch.setattr(run, "run_backtest", lambda **kwargs: None)

    run._run_main()

    output = capsys.readouterr().out
    assert "  [Config] Overriding GM_TOKEN = secret" in output
    assert (
        "  [Config] Overriding BROKER_ENVIRONMENTS = "
        "{'gm_broker': {'real': {'token': 'secret'}}}"
    ) in output


def test_run_main_reports_unknown_config_override(monkeypatch, capsys):
    """拼写错误/局部配置名不能再静默造成“配置不响应”。"""
    import run

    monkeypatch.setattr(sys, "argv", [
        "run.py",
        "dummy_strategy",
        "--config",
        (
            "{'BROKER_LOT_LIMIT': 1000000, "
            "'IBKR_ALLOW_FRACTIONAL_SELL': True}"
        ),
    ])
    monkeypatch.setattr(run.config, "DB_ENABLED", False, raising=False)
    monkeypatch.setattr(run, "run_backtest", lambda **kwargs: None)

    run._run_main()

    output = capsys.readouterr().out
    assert "未知配置覆盖项，已忽略：'BROKER_LOT_LIMIT'" in output
    assert "未知配置覆盖项，已忽略：'IBKR_ALLOW_FRACTIONAL_SELL'" in output


def test_run_main_rejects_non_mapping_config_override(monkeypatch):
    import run

    monkeypatch.setattr(sys, "argv", [
        "run.py",
        "dummy_strategy",
        "--config",
        "['LOT_SIZE']",
    ])

    with pytest.raises(ValueError, match="--config 必须是 Python 字典字符串"):
        run._run_main()


def test_run_main_centralizes_supervised_worker_operator_stop(monkeypatch):
    import alarms.manager as manager_module
    import run

    calls = []

    class DummyAlarm:
        def push_status(self, status, detail=""):
            calls.append(("status", status, detail))

    monkeypatch.setattr(run, "is_live_worker_process", lambda: True)
    monkeypatch.setattr(run, "start_live_worker_heartbeat", lambda: calls.append(("heartbeat",)))
    monkeypatch.setattr(
        run,
        "install_live_worker_operator_stop_handler",
        lambda: calls.append(("handler",)),
    )
    monkeypatch.setattr(
        run,
        "_run_main",
        lambda: (_ for _ in ()).throw(run.LiveWorkerOperatorStop()),
    )
    monkeypatch.setattr(
        run,
        "mark_live_worker_expected_exit",
        lambda detail: calls.append(("expected_exit", detail)),
    )
    monkeypatch.setattr(manager_module, "AlarmManager", lambda: DummyAlarm())

    assert run.main() == 0
    assert calls == [
        ("heartbeat",),
        ("handler",),
        ("status", "STOPPED", "Operator requested graceful shutdown."),
        ("expected_exit", "operator requested graceful shutdown"),
    ]
