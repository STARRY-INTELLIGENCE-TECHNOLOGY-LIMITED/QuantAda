import os
import subprocess

from common import detached_process


def test_spawn_detached_python_uses_current_platform_detach_flags(tmp_path, monkeypatch):
    calls = {}

    class DummyProcess:
        pid = 2468

    def popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return DummyProcess()

    monkeypatch.setattr(detached_process.subprocess, "Popen", popen)
    log_file = tmp_path / "resume.log"

    pid, path = detached_process.spawn_detached_python(
        ["python", "run.py", "--n_jobs", "31"],
        cwd=tmp_path,
        log_file=log_file,
    )

    assert pid == 2468
    assert path == str(log_file.resolve())
    assert calls["command"] == ["python", "run.py", "--n_jobs", "31"]
    assert calls["kwargs"]["stdin"] is subprocess.DEVNULL
    assert calls["kwargs"]["stderr"] is subprocess.STDOUT
    assert calls["kwargs"]["cwd"] == str(tmp_path.resolve())
    if os.name == "nt":
        assert "creationflags" in calls["kwargs"]
    else:
        assert calls["kwargs"]["start_new_session"] is True
