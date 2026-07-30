import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _find_bash():
    candidates = []
    git = shutil.which("git")
    if git:
        git_bin = Path(git).resolve().parent
        candidates.extend((git_bin / "bash.exe", git_bin.parent / "bin" / "bash.exe"))
    bash = shutil.which("bash")
    if bash:
        candidates.append(Path(bash))

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            result = subprocess.run(
                [str(candidate), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and "GNU bash" in result.stdout:
            return str(candidate)
    return None


@pytest.fixture(scope="module")
def bash():
    executable = _find_bash()
    if executable is None:
        pytest.skip("bash is required for safe process script tests")
    return executable


def _run_bash(bash, body, input_text=""):
    script = f"""
set -euo pipefail
source script/safe_process_common.sh
exec 3<&0 4>&1
{body}
"""
    result = subprocess.run(
        [bash, "-c", script],
        cwd=ROOT,
        input=input_text.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=10,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def test_command_filter_is_case_insensitive_substring(bash):
    result = _run_bash(
        bash,
        """
qada_root_pids=(101 202 303)
qada_root_cmdline[101]='python run.py Alpha --connect=gm'
qada_root_cmdline[202]='python run.py Beta --connect=ib'
qada_root_cmdline[303]='python run.py Gamma --connect=gm'
qada_filter_processes 'ALPHA'
printf '%s\\n' "${qada_visible_pids[*]}"
""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "101"


def test_live_command_key_ignores_interpreter_worker_flags(bash):
    result = _run_bash(
        bash,
        """
supervisor_key=$(qada_live_command_key /opt/venv/python /srv/run.py strategy --connect=gm:real)
worker_key=$(qada_live_command_key /opt/venv/python -u /srv/run.py strategy --connect=gm:real)
[[ "$supervisor_key" == "$worker_key" ]]
printf '%s\n' "$worker_key"
""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("/srv/run.py strategy")


def test_operation_lock_rejects_concurrent_restart(bash):
    flock_probe = subprocess.run(
        [bash, "-lc", "command -v flock"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    if flock_probe.returncode != 0:
        pytest.skip("flock is required for the Linux safe-restart lock test")

    lock_name = f"safe-restart-test-{uuid.uuid4().hex}"
    holder_script = f"""
set -euo pipefail
source script/safe_process_common.sh
qada_acquire_operation_lock {lock_name}
printf 'locked\\n'
sleep 5
"""
    holder = subprocess.Popen(
        [bash, "-c", holder_script],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline().decode("utf-8", errors="replace").strip() == "locked"
        contender = _run_bash(
            bash,
            f"qada_acquire_operation_lock {lock_name}",
        )
        assert contender.returncode != 0
        assert "already running" in contender.stderr
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_stop_refuses_success_while_original_worker_is_alive(bash):
    result = _run_bash(
        bash,
        """
qada_children_by_root[101]='202'
kill() {
  if [[ $1 == '-0' && $2 == '202' ]]; then
    return 0
  fi
  return 1
}
qada_read_cmdline() {
  local -n output=$2
  output=(/opt/venv/python -u /srv/run.py strategy --connect=gm:real)
  return 0
}
if qada_stop_supervisor 101 0; then
  printf 'unexpected-success\n'
else
  printf 'blocked\n'
fi
""",
    )
    assert result.returncode == 0, result.stderr
    assert "live worker 202 is still running" in result.stdout
    assert result.stdout.strip().endswith("blocked")


def test_search_then_enter_selects_one_matching_supervisor(bash):
    result = _run_bash(
        bash,
        """
qada_root_pids=(101 202 303)
qada_root_cmdline[101]='python run.py Alpha --connect=gm'
qada_root_cmdline[202]='python run.py Beta --connect=ib'
qada_root_cmdline[303]='python run.py Gamma --connect=gm'
selected=()
qada_select_processes selected 'QuantAda live processes' 'stop'
printf 'selected=%s\\n' "${selected[*]}"
""",
        input_text="/gAmMa\n\n",
    )
    assert result.returncode == 0, result.stderr
    assert "selected=303" in result.stdout


def test_search_then_number_selects_from_filtered_results(bash):
    result = _run_bash(
        bash,
        """
qada_root_pids=(101 202 303)
qada_root_cmdline[101]='python run.py Alpha --connect=gm'
qada_root_cmdline[202]='python run.py Beta --connect=ib'
qada_root_cmdline[303]='python run.py Gamma --connect=gm'
selected=()
qada_select_processes selected 'QuantAda live processes' 'stop'
printf 'selected=%s\\n' "${selected[*]}"
""",
        input_text="/connect=gm\n2\n",
    )
    assert result.returncode == 0, result.stderr
    assert "selected=303" in result.stdout


def test_empty_search_result_cannot_select_a_process(bash):
    result = _run_bash(
        bash,
        """
qada_root_pids=(101)
qada_root_cmdline[101]='python run.py Alpha --connect=gm'
selected=()
qada_select_processes selected 'QuantAda live processes' 'stop'
printf 'selected=%s\\n' "${selected[*]}"
""",
        input_text="/missing\n\nq",
    )
    assert result.returncode == 0, result.stderr
    assert "No commands match the current filter." in result.stdout
    assert "Invalid selection: 1" in result.stdout
    assert "selected=" not in result.stdout


@pytest.mark.parametrize("all_key", ["a", "A"])
def test_all_selection_requires_confirmation_and_uses_search_results(bash, all_key):
    result = _run_bash(
        bash,
        """
qada_root_pids=(101 202 303)
qada_root_cmdline[101]='python run.py Alpha --connect=gm'
qada_root_cmdline[202]='python run.py Beta --connect=ib'
qada_root_cmdline[303]='python run.py Gamma --connect=gm'
selected=()
qada_select_processes selected 'QuantAda live processes' 'restart'
printf 'selected=%s\\n' "${selected[*]}"
""",
        input_text=f"/connect=gm\n{all_key}\ny\n",
    )
    assert result.returncode == 0, result.stderr
    assert "selected=101 303" in result.stdout
    assert "Confirm restart all listed supervisors?" in result.stdout


def test_declined_all_selection_does_not_execute_all(bash):
    result = _run_bash(
        bash,
        """
qada_root_pids=(101 202)
qada_root_cmdline[101]='python run.py Alpha --connect=gm'
qada_root_cmdline[202]='python run.py Beta --connect=ib'
selected=()
qada_select_processes selected 'QuantAda live processes' 'stop'
printf 'selected=%s\\n' "${selected[*]}"
""",
        input_text="/connect\na\nn\n\n",
    )
    assert result.returncode == 0, result.stderr
    assert "selected=101" in result.stdout
    assert "selected=101 202" not in result.stdout
    assert "All selection cancelled." in result.stdout
