from common import runtime_command
from common.live_runtime import dependency_install_hint


def test_get_current_command_uses_python_and_sys_argv(monkeypatch):
    monkeypatch.setattr(
        runtime_command.sys,
        "argv",
        ["run.py", "sample_strategy", "--params", "{'x': 1}"],
    )

    assert runtime_command.get_current_command() == 'python run.py sample_strategy --params "{\'x\': 1}"'


def test_dependency_install_hint_lives_with_live_runtime_helpers():
    message = dependency_install_hint('futu-api', RuntimeError('missing'))

    assert 'futu-api' in message
    assert 'pip install -r requirements.txt' in message
    assert 'missing' in message
