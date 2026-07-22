import shutil
import subprocess
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
