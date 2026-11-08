"""run.py Web 工作台入口测试。"""

from __future__ import annotations

import command_center.web as web


def test_run_without_arguments_prints_help_then_launches_ui(monkeypatch, capsys):
    import run

    called: dict[str, list[str] | None] = {}
    monkeypatch.setattr(run.sys, "argv", ["run.py"])
    monkeypatch.setattr(run, "_ui_browser_available", lambda: False)
    monkeypatch.setattr(web, "main", lambda argv=None: called.setdefault("argv", argv))

    assert run._run_main() == 0
    assert called["argv"] == ["--no-browser"]
    assert "usage:" in capsys.readouterr().out


def test_run_ui_forwards_web_arguments_without_browser_on_headless_host(monkeypatch):
    import run

    called: dict[str, list[str] | None] = {}
    monkeypatch.setattr(run.sys, "argv", ["run.py", "--ui", "--port", "9001"])
    monkeypatch.setattr(run, "_ui_browser_available", lambda: False)
    monkeypatch.setattr(web, "main", lambda argv=None: called.setdefault("argv", argv))

    assert run._run_main() == 0
    assert called["argv"] == ["--port", "9001", "--no-browser"]


def test_run_ui_ip_alone_enters_ui_and_forwards_explicit_bind_address(monkeypatch):
    import run

    called: dict[str, list[str] | None] = {}
    monkeypatch.setattr(run.sys, "argv", ["run.py", "--ui_ip", "0.0.0.0", "--no-browser"])
    monkeypatch.setattr(web, "main", lambda argv=None: called.setdefault("argv", argv))

    assert run._run_main() == 0
    assert called["argv"] == ["--ui_ip", "0.0.0.0", "--no-browser"]


def test_run_dangerous_ui_flag_enters_ui_and_reaches_web_parser(monkeypatch):
    import run

    called: dict[str, object] = {}
    monkeypatch.setattr(run.sys, "argv", ["run.py", "--dangerously-ui-executable", "--no-browser"])
    monkeypatch.setattr(web, "serve", lambda *a, **kw: called.update(kw) or (_ for _ in ()).throw(OSError("stop")))

    assert run._run_main() == 1
    assert called["dangerously_ui_executable"] is True


def test_run_ui_ip_equals_form_enters_ui(monkeypatch):
    import run

    called: dict[str, list[str] | None] = {}
    monkeypatch.setattr(run.sys, "argv", ["run.py", "--ui_ip=0.0.0.0", "--no-browser"])
    monkeypatch.setattr(web, "main", lambda argv=None: called.setdefault("argv", argv))

    assert run._run_main() == 0
    assert called["argv"] == ["--ui_ip=0.0.0.0", "--no-browser"]


def test_run_ui_propagates_web_start_failure(monkeypatch):
    import run

    monkeypatch.setattr(run.sys, "argv", ["run.py", "--ui", "--no-browser"])
    monkeypatch.setattr(web, "main", lambda argv=None: 1)

    assert run._run_main() == 1


def test_run_normalizes_common_timeframe_aliases():
    import run

    assert run._normalize_timeframe("1d") == "Days"
    assert run._normalize_timeframe("m") == "Minutes"
    assert run._normalize_timeframe("1mo") == "Months"
    assert run._normalize_timeframe("day:14:45:00") == "Day:14:45:00"
