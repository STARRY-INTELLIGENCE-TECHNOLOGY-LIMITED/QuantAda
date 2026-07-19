from common import runtime_command


def test_get_current_command_uses_python_and_sys_argv(monkeypatch):
    monkeypatch.setattr(
        runtime_command.sys,
        "argv",
        ["run.py", "sample_strategy", "--params", "{'x': 1}"],
    )

    assert runtime_command.get_current_command() == 'python run.py sample_strategy --params "{\'x\': 1}"'
