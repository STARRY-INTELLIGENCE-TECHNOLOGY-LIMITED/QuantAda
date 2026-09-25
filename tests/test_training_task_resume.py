"""交互续传的任务目录、CLI 选择和工作台执行入口。"""

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from optimizer.journal_metadata import WORKER_CONFIG_VERSION, read_study_metadata
from common.terminal_log import OPTIMIZER_AI_ANALYSIS_END_MARKER, OPTIMIZER_AI_ANALYSIS_START_MARKER
from optimizer.training_tasks import _DISPLAY_TAIL_BYTES, build_resume_arguments, choose_training_task, list_training_tasks, read_terminal_log_page, training_task_commands


def test_training_resume_ui_async_consistency():
    node = shutil.which("node")
    if node is None:
        pytest.skip("需要 Node.js 执行页面异步交互断言")
    subprocess.run([node, "--test", "tests/test_training_resume_ui.js"], check=True, timeout=20)


def _recorded(**changes):
    values = dict(
        strategy="examples.Strategy", selection=None, symbols="AAA", data_source="csv", cash=100000.0,
        commission=0.0005, slippage=0.0001, timeframe="Days", compression=1, risk=None, params={"size": 1},
        opt_params={"x": {"type": "int", "low": 1, "high": 4}}, risk_params={}, config={"LOT_SIZE": 1},
        train_roll_period="2y", test_roll_period="12m", train_ratio=None, train_period=None, test_period=None,
        start_date="20230923", end_date="20260923", metric="return", n_trials=None, n_jobs=-1,
        no_plot=True, refresh=True, opt_schedule="1d:02:00", connect=None, desc="saved task", plot_scope="full",
    )
    values.update(changes)
    recorded = {key: str(value) for key, value in values.items()}
    recorded["_optimizer_data_snapshot"] = {"id": "f" * 32, "sha256": "a" * 64}
    return recorded


def _write_task(path, *, attributes=None, metrics=("return", "sharpe"), timestamp=1000, name="saved", config_version=WORKER_CONFIG_VERSION):
    path.parent.mkdir(parents=True, exist_ok=True)
    attrs = attributes or _recorded()
    records = [{"op_code": 0, "study_name": name, "directions": [2]}]
    records.extend({"op_code": 2, "study_id": 0, "user_attr": {key: value}} for key, value in attrs.items())
    if config_version is not None:
        records.append({"op_code": 2, "study_id": 0, "user_attr": {"_optimizer_worker_config_version": config_version}})
    if metrics is not None:
        records.append({"op_code": 2, "study_id": 0, "user_attr": {"_optimizer_metrics": list(metrics)}})
        records.append({"op_code": 2, "study_id": 0, "user_attr": {"_optimizer_target_trials": 4}})
    records.extend([
        {"op_code": 4, "study_id": 0, "datetime_start": "2026-09-23T09:00:00"},
        {"op_code": 6, "trial_id": 0, "state": 1, "values": [-43.0]},
        {"op_code": 4, "study_id": 0, "datetime_start": "2026-09-23T09:00:01"},
        {"op_code": 6, "trial_id": 1, "state": 3, "values": None},
    ])
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    os.utime(path, (timestamp, timestamp))
    return path


def test_task_browser_and_web_imports_do_not_load_scientific_runtime():
    code = (
        "import sys; import command_center.web; import optimizer.training_tasks; "
        "assert not set(sys.modules).intersection({'numpy','pandas','optuna','optimizer.runtime'})"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)


def test_task_list_is_newest_first_with_saved_metric_set_and_progress(tmp_path):
    old = _write_task(tmp_path / "optuna_old.log", timestamp=1000)
    new = _write_task(tmp_path / "optuna_new.log", timestamp=2000, attributes=_recorded(strategy="examples.NewStrategy"))
    tasks = list_training_tasks(tmp_path)
    assert [task["journal"] for task in tasks] == [str(new), str(old)]
    assert tasks[0]["metrics"] == ["return", "sharpe"]
    assert tasks[0]["n_trials"] == 4
    assert tasks[0]["trial_counts"] == {"RUNNING": 0, "COMPLETE": 1, "PRUNED": 0, "FAIL": 1, "WAITING": 0}
    assert tasks[0]["resumable"] is True


def test_task_versions_have_separate_progress_and_show_isolation_notice(tmp_path, monkeypatch, capsys):
    from command_center.generator import build_resume_command

    path = _write_task(tmp_path / "optuna_versions.log", config_version=None)
    with path.open("a", encoding="utf-8") as stream:
        events = [{"op_code": 0, "study_name": "current", "directions": [2]}]
        events.extend({"op_code": 2, "study_id": 1, "user_attr": {key: value}} for key, value in _recorded().items())
        events.extend([
            {"op_code": 2, "study_id": 1, "user_attr": {"_optimizer_worker_config_version": WORKER_CONFIG_VERSION}},
            {"op_code": 2, "study_id": 1, "user_attr": {"_optimizer_metrics": ["return", "sharpe"]}},
            {"op_code": 4, "study_id": 1, "state": 1, "datetime_start": None},
        ])
        stream.writelines(json.dumps(event) + "\n" for event in events)
    current, legacy = list_training_tasks(tmp_path)
    assert current["worker_config_version"] == WORKER_CONFIG_VERSION
    assert current["trial_counts"]["COMPLETE"] == 1
    assert current["trial_counts"]["FAIL"] == 0
    assert current["notice"] == ""
    assert legacy["trial_counts"]["COMPLETE"] == 1
    assert legacy["trial_counts"]["FAIL"] == 1
    assert "1 completed trials count toward budget 4" in legacy["notice"]
    assert "budget 4" in legacy["notice"]
    assert "instead of starting at 0" in legacy["notice"]
    assert "does not count" not in legacy["notice"]
    assert "trial_id" in legacy["notice"]
    assert current["task_id"] != legacy["task_id"]
    assert build_resume_command(legacy, {}, tmp_path).warnings == (legacy["notice"],)
    answers = iter(["2", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert choose_training_task([current, legacy]) is None
    assert legacy["notice"] in capsys.readouterr().out


def test_progress_counts_waiting_retries_and_ignores_invalid_terminal_updates(tmp_path):
    path = _write_task(tmp_path / "optuna_task.log")
    with path.open("a", encoding="utf-8") as stream:
        for event in (
            {"op_code": 6, "trial_id": 1, "state": 4},
            {"op_code": 4, "study_id": 0, "state": 4, "datetime_start": None},
            {"op_code": 6, "trial_id": 2, "state": 0},
            {"op_code": 6, "trial_id": 2, "state": 1},
            {"op_code": 4, "study_id": 0, "state": 0, "datetime_start": None},
            {"op_code": 6, "trial_id": 3, "state": 4},
        ):
            stream.write(json.dumps(event) + "\n")
    counts = read_study_metadata(path, with_trial_counts=True)[0]["trial_counts"]
    assert counts == {"RUNNING": 0, "COMPLETE": 2, "PRUNED": 0, "FAIL": 1, "WAITING": 1}
    assert "trial_counts" not in read_study_metadata(path)[0]


def test_task_cache_refreshes_after_append_but_identity_stays_stable(tmp_path):
    path = _write_task(tmp_path / "optuna_task.log")
    before = list_training_tasks(tmp_path)[0]
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"op_code": 4, "study_id": 0, "state": 1, "datetime_start": None}) + "\n")
    after = list_training_tasks(tmp_path)[0]
    assert before["task_id"] == after["task_id"]
    assert before["trial_counts"]["COMPLETE"] == 1
    assert after["trial_counts"]["COMPLETE"] == 2


def test_task_restores_most_recent_budget_and_workers_across_metrics(tmp_path):
    path = _write_task(tmp_path / "optuna_task.log")
    with path.open("a", encoding="utf-8") as stream:
        events = [{"op_code": 0, "study_name": "second_metric", "directions": [2]}]
        events.extend({"op_code": 2, "study_id": 1, "user_attr": {key: value}}
                      for key, value in _recorded(metric="sharpe", n_trials=4).items())
        events.extend([
            {"op_code": 2, "study_id": 1, "user_attr": {"_optimizer_worker_config_version": WORKER_CONFIG_VERSION}},
            {"op_code": 2, "study_id": 0, "user_attr": {"_optimizer_target_trials": 10}},
            {"op_code": 2, "study_id": 0, "user_attr": {"n_jobs": "2"}},
        ])
        stream.writelines(json.dumps(event) + "\n" for event in events)

    task = list_training_tasks(tmp_path)[0]
    argv = build_resume_arguments(task)
    assert task["n_trials"] == 10
    assert argv[argv.index("--n_jobs") + 1] == "2"


def test_legacy_task_recovers_unstarted_metrics_from_its_terminal_header(tmp_path):
    journal = _write_task(tmp_path / "optuna" / "optuna_old.log", metrics=None, config_version=None)
    terminal = tmp_path / "optimizer" / "optimizer_terminal_old.log"
    terminal.parent.mkdir()
    terminal.write_text(
        ">>> Mode: PARAMETER OPTIMIZATION (Target: return,sharpe,calmar) <<<\n"
        f"[Optimizer] Using JournalStorage: {journal}\n"
        "[Optimizer] Auto-inferred n_trials: 2160 (entropy-complexity model)\n"
        "--- Starting Optimization (2160 trials, 13 parallel jobs) ---\n",
        encoding="utf-8",
    )
    task = list_training_tasks(journal.parent)[0]
    assert task["metrics"] == ["return", "sharpe", "calmar"]
    assert task["n_trials"] == 2160


def test_resume_arguments_restore_training_context_and_skip_startup_schedule(tmp_path):
    path = _write_task(tmp_path / "optuna_task.log")
    task = list_training_tasks(tmp_path)[0]
    argv = build_resume_arguments(task)
    assert argv[0] == "examples.Strategy"
    for key, value in {"start_date": "20230923", "end_date": "20260923", "metric": "return,sharpe", "n_trials": "4", "n_jobs": "-1", "study_journal": str(path)}.items():
        assert argv[argv.index("--" + key) + 1] == value
    assert ast.literal_eval(argv[argv.index("--params") + 1]) == {"size": 1}
    assert ast.literal_eval(argv[argv.index("--config") + 1]) == {"LOT_SIZE": 1}
    assert "--connect" not in argv
    assert "--opt_schedule" not in argv
    assert "--refresh" not in argv
    assert argv[argv.index("--study_name") + 1] == "saved"


@pytest.mark.parametrize("changes", [{"connect": "gm_broker:real"}, {"start_date": None}, {"opt_params": {}}, {"params": "__import__('os').getcwd()"}])
def test_unrecoverable_or_live_records_are_disabled(tmp_path, changes):
    _write_task(tmp_path / "optuna_task.log", attributes=_recorded(**changes))
    task = list_training_tasks(tmp_path)[0]
    assert task["resumable"] is False
    assert task["reason"]


def test_picker_reprompts_for_invalid_number_and_returns_selected_snapshot(monkeypatch, tmp_path, capsys):
    _write_task(tmp_path / "optuna_task.log")
    tasks = list_training_tasks(tmp_path)
    answers = iter(["invalid", "99", "1", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert choose_training_task(tasks) is tasks[0]
    assert "valid task number" in capsys.readouterr().out


def test_picker_pages_ten_tasks_and_requires_detail_confirmation(monkeypatch, tmp_path, capsys):
    _write_task(tmp_path / "optuna_task.log")
    template = list_training_tasks(tmp_path)[0]
    tasks = [{**template, "strategy": f"demo.Task{i:02d}"} for i in range(1, 24)]
    answers = iter(["n", "g 3", "23", "b", "p", "11", "y"])
    screens = []

    def answer(prompt):
        screens.append(capsys.readouterr().out)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    assert choose_training_task(tasks) is tasks[10]
    assert "demo.Task10" in screens[0] and "demo.Task11" not in screens[0]
    assert "Total 23 tasks | page 1/3" in screens[0]
    assert "demo.Task11" in screens[1] and "demo.Task21" not in screens[1]
    assert "page 3/3" in screens[2]
    assert "Original launch command" in screens[3] and "Manually train with fresh market data" in screens[3]
    assert "confirm resume" in screens[3]
    assert "demo.Task11" in screens[-1]


@pytest.mark.parametrize("cancel", ["q", EOFError(), KeyboardInterrupt()])
def test_picker_cancels_from_command_detail_without_starting(monkeypatch, tmp_path, cancel):
    _write_task(tmp_path / "optuna_task.log")
    answers = iter(["1", cancel])

    def answer(_):
        value = next(answers)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", answer)
    assert choose_training_task(list_training_tasks(tmp_path)) is None


def test_recorded_original_command_preserves_omitted_dates_and_fresh_command_unbinds_resume(tmp_path):
    import shlex

    attrs = _recorded()
    original = ["examples.Strategy", "--params", "{'text': '$HOME `literal`'}", "--n_jobs", "-1",
                "--study_name=manual", "--study_journal", "old.log", "--opt_schedule", "1d:02:00"]
    attrs["_optimizer_original_argv"] = original
    _write_task(tmp_path / "optuna_task.log", attributes=attrs)
    commands = training_task_commands(list_training_tasks(tmp_path)[0], "bash")
    assert commands["original_exact"] is True
    assert shlex.split(commands["original_command"]) == ["python", "run.py", *original]
    fresh = shlex.split(commands["fresh_command"])
    assert "--refresh" in fresh
    assert not any(item.startswith(("--study_", "--start_date", "--end_date", "--opt_schedule", "--train_resume")) for item in fresh)


def test_resume_preview_merges_config_overrides_like_actual_cli(tmp_path):
    import ast

    _write_task(tmp_path / "optuna_task.log")
    argv = build_resume_arguments(list_training_tasks(tmp_path)[0], ["--config", "{'ANNUAL_FACTOR': 365}", "--n_jobs", "2"])
    last_config = max(index for index, item in enumerate(argv) if item == "--config")
    assert ast.literal_eval(argv[last_config + 1]) == {"LOT_SIZE": 1, "ANNUAL_FACTOR": 365}


def test_cli_resume_restores_selection_before_elevation_and_allows_worker_override(monkeypatch, tmp_path):
    import config
    import run

    directory = tmp_path / ".data" / "optuna"
    old = _write_task(directory / "optuna_old.log", timestamp=1000, attributes=_recorded(strategy="examples.Old"))
    _write_task(directory / "optuna_new.log", timestamp=2000, attributes=_recorded(strategy="examples.New"))
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path / ".data"))
    monkeypatch.setattr(run.sys, "argv", ["run.py", "--train_resume", "--n_jobs", "2"])
    monkeypatch.setenv("QUANTADA_STUDY_NAME", "unrelated-study")
    monkeypatch.setenv("QUANTADA_STUDY_JOURNAL", "unrelated.log")
    answers = iter(["2", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    captured = {}

    def optimize(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(run.optimizer, "run_optimizer_mode", optimize)
    with pytest.raises(SystemExit) as exit_result:
        run._run_main()
    assert exit_result.value.code == 0
    args = captured["args"]
    assert args.strategy == "examples.Old"
    assert args.study_journal == str(old)
    assert args.metric == "return,sharpe"
    assert args.n_jobs == 2
    assert args.n_trials == 4
    assert args.opt_schedule is None
    assert args.refresh is False
    assert args.start_date == "20230923"
    assert "--train_resume" not in run.sys.argv
    assert os.environ.get("QUANTADA_STUDY_NAME") is None
    assert captured["fixed_params"] == {"size": 1}


@pytest.mark.parametrize("cancel", ["q", EOFError(), KeyboardInterrupt()])
def test_cli_resume_cancel_never_starts_training(monkeypatch, tmp_path, cancel):
    import config
    import run

    _write_task(tmp_path / ".data" / "optuna" / "optuna_task.log")
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path / ".data"))
    monkeypatch.setattr(run.sys, "argv", ["run.py", "--train_resume"])

    def answer(_):
        if isinstance(cancel, BaseException):
            raise cancel
        return cancel

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(run.optimizer, "run_optimizer_mode", lambda **_: pytest.fail("cancel must not execute"))
    assert run._run_main() == 0


def test_web_resume_uses_saved_arguments_and_existing_execution_tracking(tmp_path, monkeypatch):
    import config
    from command_center.web import CommandCenterService

    monkeypatch.setattr(config, "DATA_PATH", ".data")
    _write_task(tmp_path / ".data" / "optuna" / "optuna_task.log")
    (tmp_path / "run.py").write_text("import sys\nprint('resume-ok')\nprint(sys.argv[1:])\n", encoding="utf-8")
    service = CommandCenterService(tmp_path)
    task = service.training_tasks()[0]
    assert "recorded" not in task
    payload = {"train_resume": task["task_id"], "strategy": "ignored", "params": {"size": 99}, "variables": {"PYTHON_EXECUTABLE": sys.executable}}
    generated = service.generate(payload)
    assert generated["argv"][3] == "examples.Strategy"
    assert generated["params"] == {"size": 1}
    assert generated["config"] == {"LOT_SIZE": 1}
    assert generated["variables"]["QUANTADA_DISABLE_AUTO_ELEVATE"] == "1"
    assert generated["variables"]["QUANTADA_STUDY_NAME"] == ""
    assert "--train_resume" not in generated["argv"]
    run_id = service.execute(payload)["run_id"]
    deadline = time.monotonic() + 10
    while service.run_snapshot(run_id)["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
    result = service.run_snapshot(run_id)
    assert result["return_code"] == 0
    assert "resume-ok" in "\n".join(result["output"])


def test_web_task_api_and_unknown_selection_validation(tmp_path, monkeypatch):
    import config
    from command_center.web import serve

    monkeypatch.setattr(config, "DATA_PATH", ".data")
    _write_task(tmp_path / ".data" / "optuna" / "optuna_task.log")
    server = serve(tmp_path, port=0, open_browser=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        tasks = json.loads(urllib.request.urlopen(url + "/api/training/tasks").read())["tasks"]
        assert tasks[0]["strategy"] == "examples.Strategy"
        assert tasks[0]["training_status"] == "Incomplete"
        request = urllib.request.Request(
            url + "/api/generate", data=json.dumps({"train_resume": tasks[0]["task_id"]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        generated = json.loads(urllib.request.urlopen(request).read())
        assert "--study_journal" in generated["argv"]
        request = urllib.request.Request(
            url + "/api/execute", data=json.dumps({"train_resume": "../../unknown"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(request)
        assert result.value.code == 400
        assert server.service.runs == {}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _write_terminal(journal, name, body, timestamp):
    path = journal.parent.parent / "optimizer" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.utime(path, (timestamp, timestamp))
    return path


def _terminal_body(journal, *, finished=False, started=False):
    lines = [
        f"[Optimizer] Training Journal: {journal}",
        "--- Starting Optimization (4 trials, 1 parallel jobs) ---",
        "trial output",
    ]
    if finished or started:
        lines.append(OPTIMIZER_AI_ANALYSIS_START_MARKER)
        lines.append("summary only")
    if finished:
        lines.extend([OPTIMIZER_AI_ANALYSIS_END_MARKER, "optuna-dashboard replay"])
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_training_status_uses_latest_display_section_and_keeps_resume(monkeypatch, tmp_path, capsys):
    journal = _write_task(tmp_path / "optuna" / "optuna_task.log")
    _write_terminal(journal, "optimizer_terminal_old.log", _terminal_body(journal, finished=True), 1000)
    _write_terminal(journal, "optimizer_terminal_new.log", _terminal_body(journal, started=True), 2000)
    task = list_training_tasks(journal.parent)[0]
    assert task["training_status"] == "Incomplete"
    assert task["resumable"] is True
    assert "summary only" not in task.values()

    _write_terminal(journal, "optimizer_terminal_new.log", _terminal_body(journal, finished=True), 3000)
    task = list_training_tasks(journal.parent)[0]
    assert task["training_status"] == "Finished"
    monkeypatch.setattr("builtins.input", lambda _: "q")
    assert choose_training_task([task]) is None
    assert "Finished" in capsys.readouterr().out


def test_training_status_ignores_markers_outside_the_ending_display_section(tmp_path):
    journal = _write_task(tmp_path / "optuna" / "optuna_task.log")
    # 新试验输出必须超出末尾展示窗口，旧的完成标记才不能再把任务标成已训练完毕。
    padding = b"later trial\n" * (_DISPLAY_TAIL_BYTES // len(b"later trial\n") + 8)
    buried = _terminal_body(journal, finished=True) + padding
    _write_terminal(journal, "optimizer_terminal_task.log", buried, 1000)
    assert list_training_tasks(journal.parent)[0]["training_status"] == "Incomplete"


def _empty_crash_body(journal, *, zeros_only=False):
    suffix = "Metrics completed:  0\nCompleted trials:   0\n"
    if not zeros_only:
        suffix = '训练指标未返回结果' + "\n" + suffix
    return _terminal_body(journal, finished=True) + suffix.encode("utf-8")


def test_empty_crash_summary_does_not_finish_an_open_training(tmp_path):
    journal = _write_task(tmp_path / "optuna" / "optuna_task.log")
    _write_terminal(journal, "optimizer_terminal_old.log", _terminal_body(journal, started=True), 1000)
    _write_terminal(journal, "optimizer_terminal_crash.log", _empty_crash_body(journal), 2000)
    assert list_training_tasks(journal.parent)[0]["training_status"] == 'Incomplete'


def test_english_empty_summary_is_not_finished(tmp_path):
    journal = _write_task(tmp_path / "optuna" / "optuna_task.log")
    body = _terminal_body(journal, finished=True) + b"Training metrics returned no results\nAll metrics returned no results\n"
    _write_terminal(journal, "optimizer_terminal_crash.log", body, 2000)
    assert list_training_tasks(journal.parent)[0]["training_status"] == "Incomplete"


def test_zero_summary_without_warning_does_not_finish_an_open_training(tmp_path):
    journal = _write_task(tmp_path / "optuna" / "optuna_task.log")
    _write_terminal(journal, "optimizer_terminal_old.log", _terminal_body(journal, started=True), 1000)
    _write_terminal(journal, "optimizer_terminal_crash.log", _empty_crash_body(journal, zeros_only=True), 2000)
    assert list_training_tasks(journal.parent)[0]["training_status"] == 'Incomplete'


def test_empty_crash_summary_does_not_hide_an_earlier_real_finish(tmp_path):
    journal = _write_task(tmp_path / "optuna" / "optuna_task.log")
    _write_terminal(journal, "optimizer_terminal_done.log", _terminal_body(journal, finished=True), 1000)
    _write_terminal(journal, "optimizer_terminal_crash.log", _empty_crash_body(journal), 2000)
    assert list_training_tasks(journal.parent)[0]["training_status"] == 'Finished'


def test_terminal_log_pages_default_to_the_end_and_can_jump(tmp_path):
    path = tmp_path / "optimizer_terminal_pages.log"
    path.write_text("".join(f"line-{index:04d}\n" for index in range(200)), encoding="utf-8")
    end = read_terminal_log_page(path, where="end")
    start = read_terminal_log_page(path, where="start")
    middle = read_terminal_log_page(path, where="middle")
    assert end["pages"] == 5
    assert end["page"] == 5
    assert "line-0199" in end["text"]
    assert "line-0000" not in end["text"]
    assert len(end["text"].splitlines()) <= 40
    assert start["page"] == 1 and "line-0000" in start["text"]
    assert middle["page"] == 3
    assert read_terminal_log_page(path, page=2)["page"] == 2


def test_selected_task_log_menu_opens_at_end_and_jumps_without_starting(monkeypatch, tmp_path, capsys):
    journal = _write_task(tmp_path / "optuna" / "optuna_task.log")
    lines = [f"line-{index:03d}" for index in range(1, 101)]
    body = (
        f"[Optimizer] Training Journal: {journal}\n"
        "--- Starting Optimization (4 trials, 1 parallel jobs) ---\n"
        + "\n".join(lines) + "\n"
        + OPTIMIZER_AI_ANALYSIS_START_MARKER + "\nsummary section\n"
        + OPTIMIZER_AI_ANALYSIS_END_MARKER + "\n"
        + "".join(f"tail-{index:03d}\n" for index in range(50))
    ).encode("utf-8")
    _write_terminal(journal, "optimizer_terminal_task.log", body, 1000)
    answers = iter(["1", "l", "h", "m", "s", "g 2", "b", "q"])
    screens = []

    def answer(_prompt):
        screens.append(capsys.readouterr().out)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    assert choose_training_task(list_training_tasks(journal.parent)) is None
    assert "l: view log" in screens[1]
    assert "tail-049" in screens[2] and OPTIMIZER_AI_ANALYSIS_START_MARKER not in screens[2]
    assert "line-001" in screens[3] and "tail-049" not in screens[3]
    assert "line-050" in screens[4] and "tail-049" not in screens[4]
    assert OPTIMIZER_AI_ANALYSIS_START_MARKER in screens[5]
    assert "page 2/" in screens[6]
    assert "confirm resume" in screens[7]


def test_missing_terminal_log_stays_on_the_detail_menu(monkeypatch, tmp_path, capsys):
    _write_task(tmp_path / "optuna_task.log")
    answers = iter(["1", "l", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert choose_training_task(list_training_tasks(tmp_path)) is None
    assert "No terminal log found for this task." in capsys.readouterr().out


def test_web_task_log_pages_the_selected_journal_and_rejects_unknown_tasks(tmp_path, monkeypatch):
    import config
    from command_center.web import CommandCenterService

    monkeypatch.setattr(config, "DATA_PATH", ".data")
    journal = _write_task(tmp_path / ".data" / "optuna" / "optuna_task.log")
    body = (
        f"[Optimizer] Using JournalStorage: {journal.name}\n"
        "--- Starting Optimization (4 trials, 1 parallel jobs) ---\n"
        + "".join(f"row-{index:03d}\n" for index in range(90))
        + OPTIMIZER_AI_ANALYSIS_START_MARKER + "\nvisible summary\n"
        + OPTIMIZER_AI_ANALYSIS_END_MARKER + "\n"
    ).encode("utf-8")
    _write_terminal(journal, "optimizer_terminal_web.log", body, 1000)
    service = CommandCenterService(tmp_path)
    task_id = service.training_tasks()[0]["task_id"]
    end = service.training_task_log({"train_resume": task_id, "path": str(tmp_path / "secret.log")})
    assert end["page"] == end["pages"]
    assert "row-089" in end["text"]
    assert "secret" not in end["text"]
    home = service.training_task_log({"train_resume": task_id, "where": "start"})
    assert home["page"] == 1 and "row-000" in home["text"]
    summary = service.training_task_log({"train_resume": task_id, "where": "display"})
    assert "visible summary" in summary["text"]
    with pytest.raises(ValueError):
        service.training_task_log({"train_resume": "missing"})


def test_thinner_fork_resume_points_at_the_richer_study(tmp_path):
    path = _write_task(tmp_path / "optuna_versions.log", config_version=None, name="old")
    with path.open("a", encoding="utf-8") as stream:
        events = [{"op_code": 0, "study_name": "thin", "directions": [2]}]
        events.extend({"op_code": 2, "study_id": 1, "user_attr": {key: value}} for key, value in _recorded().items())
        events.extend([
            {"op_code": 2, "study_id": 1, "user_attr": {"_optimizer_worker_config_version": WORKER_CONFIG_VERSION}},
            {"op_code": 2, "study_id": 1, "user_attr": {"_optimizer_metrics": ["return", "sharpe"]}},
            {"op_code": 4, "study_id": 1, "state": 1, "datetime_start": None},
            {"op_code": 4, "study_id": 0, "state": 1, "datetime_start": None},
        ])
        stream.writelines(json.dumps(event) + "\n" for event in events)
    tasks = list_training_tasks(tmp_path)
    thin = next(task for task in tasks if "thin" in task["study_names"])
    argv = build_resume_arguments(thin)
    assert argv[argv.index("--study_name") + 1] == "old"
    assert "more completed trials" in thin["notice"]



def _append_window(path, *, name, start_date, end_date, snapshot_id):
    attrs = _recorded(start_date=start_date, end_date=end_date)
    attrs["_optimizer_data_snapshot"] = {"id": snapshot_id, "sha256": "b" * 64}
    records = [{"op_code": 0, "study_name": name, "directions": [2]}]
    records.extend({"op_code": 2, "study_id": 1, "user_attr": {key: value}} for key, value in attrs.items())
    records.extend([
        {"op_code": 2, "study_id": 1, "user_attr": {"_optimizer_worker_config_version": WORKER_CONFIG_VERSION}},
        {"op_code": 2, "study_id": 1, "user_attr": {"_optimizer_metrics": ["return", "sharpe"]}},
        {"op_code": 2, "study_id": 1, "user_attr": {"_optimizer_target_trials": 4}},
    ])
    with path.open("a", encoding="utf-8") as stream:
        stream.writelines(json.dumps(record) + "\n" for record in records)


def _scope_body(journal, *, start, end, snapshot, finished=True):
    lines = [
        f"[Optimizer] Training Journal: {journal}",
        f"[Optimizer] Reusing training window: {start} to {end}",
        f"[Optimizer] Training scope: window={start}..{end} snapshot={snapshot}",
        "--- Starting Optimization (4 trials, 1 parallel jobs) ---",
        "trial output",
    ]
    if finished:
        lines.extend([OPTIMIZER_AI_ANALYSIS_START_MARKER, "summary only", OPTIMIZER_AI_ANALYSIS_END_MARKER, "done"])
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_scope_sidecar_records_basename_without_a_client_log_path(monkeypatch, tmp_path):
    from optimizer.training_tasks import note_terminal_scope

    log = tmp_path / "optimizer_terminal_scope.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("QUANTADA_OPTIMIZER_TERMINAL_LOG", str(log))
    line = note_terminal_scope(
        r"E:\data\optuna\optuna_return.log", window=("20230923", "20260923"), snapshot_id="a" * 32,
    )
    assert line == f"[Optimizer] Training scope: window=20230923..20260923 snapshot={'a' * 32}"
    recorded = json.loads(Path(str(log) + ".scope").read_text(encoding="utf-8"))
    assert recorded == {
        "journal": "optuna_return.log", "window": ["20230923", "20260923"], "snapshot": "a" * 32,
    }


def test_one_terminal_log_finishes_every_journal_opened_in_the_run(tmp_path):
    from optimizer.training_tasks import task_terminal_log

    journal_a = _write_task(tmp_path / "optuna" / "optuna_a.log", timestamp=1000)
    journal_b = _write_task(
        tmp_path / "optuna" / "optuna_b.log", timestamp=2000, attributes=_recorded(strategy="examples.Other"),
    )
    path = _write_terminal(journal_a, "optimizer_terminal_both.log", _terminal_body(journal_a, finished=True), 3000)
    scope = Path(str(path) + ".scope")
    scope.write_text("".join(
        json.dumps({
            "journal": journal.name, "window": ["20230923", "20260923"], "snapshot": "f" * 32,
        }) + "\n"
        for journal in (journal_a, journal_b)
    ), encoding="utf-8")
    tasks = list_training_tasks(journal_a.parent)
    by_name = {Path(task["journal"]).name: task for task in tasks}
    assert by_name["optuna_a.log"]["training_status"] == "Finished"
    assert by_name["optuna_b.log"]["training_status"] == "Finished"
    assert task_terminal_log(by_name["optuna_a.log"], tasks) == path
    assert task_terminal_log(by_name["optuna_b.log"], tasks) == path


def test_newer_window_log_does_not_finish_or_open_for_another_task(tmp_path, monkeypatch):
    from optimizer.training_tasks import task_terminal_log
    import config
    from command_center.web import CommandCenterService

    journal = _write_task(tmp_path / ".data" / "optuna" / "optuna_task.log")
    _append_window(journal, name="other-window", start_date="20200101", end_date="20220101", snapshot_id="a" * 32)
    other = _write_terminal(
        journal, "optimizer_terminal_other.log",
        _scope_body(journal, start="20200101", end="20220101", snapshot="a" * 32), 2000,
    )
    ambiguous = _write_terminal(journal, "optimizer_terminal_ambiguous.log", _terminal_body(journal, finished=True), 3000)
    tasks = list_training_tasks(journal.parent)
    by_end = {task["end_date"]: task for task in tasks}
    assert set(by_end) == {"20260923", "20220101"}
    assert by_end["20260923"]["training_status"] == "Incomplete"
    assert by_end["20220101"]["training_status"] == "Finished"
    assert task_terminal_log(by_end["20260923"], tasks) is None
    assert task_terminal_log(by_end["20220101"], tasks) == other
    assert task_terminal_log(by_end["20220101"], tasks) != ambiguous
    monkeypatch.setattr(config, "DATA_PATH", ".data")
    service = CommandCenterService(tmp_path)
    with pytest.raises(ValueError, match="No terminal log"):
        service.training_task_log({"train_resume": by_end["20260923"]["task_id"], "path": str(other)})
    opened = service.training_task_log({"train_resume": by_end["20220101"]["task_id"], "path": str(ambiguous)})
    assert opened["name"] == other.name
    assert "secret" not in opened["text"]
