"""
Process elevation helpers for optimizer runtime.

The optimizer provides the worker-count policy; this module owns platform
checks, relaunch commands, and environment propagation.
"""

import base64
import os
import sys

from common.terminal_log import OPTIMIZER_TERMINAL_LOG_ENV, get_optimizer_terminal_log_path


def _escape_cmd_value(value):
    safe_chars = []
    for ch in str(value).replace('"', ""):
        if ch in {"^", "&", "|", "<", ">"}:
            safe_chars.append("^" + ch)
        elif ch == "%":
            safe_chars.append("%%")
        else:
            safe_chars.append(ch)
    return "".join(safe_chars)


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _build_windows_powershell_start_info():
    script_argv = [os.path.abspath(sys.argv[0])] + list(sys.argv[1:])
    python_args = ["-X", "utf8"] + script_argv
    python_cmd = "& " + _ps_quote(sys.executable) + " " + " ".join(_ps_quote(arg) for arg in python_args)
    terminal_log_path = get_optimizer_terminal_log_path()

    env_cmds = [
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
        f"Set-Location -LiteralPath {_ps_quote(os.getcwd())}",
        "chcp 65001 >$null",
        "$env:PYTHONIOENCODING='utf-8'",
        "$env:PYTHONUTF8='1'",
        "$env:QUANTADA_DISABLE_AUTO_ELEVATE='1'",
    ]
    if terminal_log_path:
        safe_log_path = str(terminal_log_path).replace("'", "''")
        env_cmds.append(f"$env:{OPTIMIZER_TERMINAL_LOG_ENV}='{safe_log_path}'")

    inner = "; ".join(env_cmds + [python_cmd, "Write-Host ''; Write-Host '[Optimizer] Elevated run finished. This window is kept open. Close it manually when done.'"])
    encoded = inner.encode("utf-16le")
    b64 = base64.b64encode(encoded).decode("ascii")
    return b64


def is_process_elevated():
    """
    Cross-platform administrator/root check.
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        try:
            return int(geteuid()) == 0
        except Exception:
            return False
    return False


def should_try_auto_elevation(args, resolve_worker_count):
    disable_flag = str(os.environ.get("QUANTADA_DISABLE_AUTO_ELEVATE", "")).strip().lower()
    if disable_flag in {"1", "true", "yes", "on"}:
        return False

    requested_jobs = getattr(args, "n_jobs", 1)
    try:
        workers = resolve_worker_count(requested_jobs)
    except Exception:
        workers = 1

    return workers > 1 and (not is_process_elevated())


def print_elevation_banner(args, resolve_worker_count):
    requested_jobs = getattr(args, "n_jobs", 1)
    try:
        workers = resolve_worker_count(requested_jobs)
    except Exception:
        workers = 1

    print("\n" + "=" * 72)
    print("[Optimizer] Authorization Request for Maximum Training Performance")
    print(
        f"[Optimizer] Target parallel workers: {workers}. "
        "Requesting administrator privileges before optimization."
    )
    print(
        "[Optimizer] The command will relaunch with elevated rights, "
        "then continue with the same CLI arguments."
    )
    print("=" * 72)


def build_windows_elevated_console_command():
    powershell_exe = os.environ.get("POWERSHELL") or "powershell.exe"
    script_b64 = _build_windows_powershell_start_info()
    params = f"-NoExit -NoProfile -ExecutionPolicy Bypass -EncodedCommand {script_b64}"
    return powershell_exe, params


def relaunch_windows_as_admin():
    try:
        import ctypes
    except Exception as e:
        print(f"[Optimizer] Warning: unable to load Windows elevation API: {e}")
        return False

    try:
        target_exe, params = build_windows_elevated_console_command()
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            target_exe,
            params,
            os.getcwd(),
            1,
        )
        if int(result) <= 32:
            print(f"[Optimizer] Warning: elevation request was not granted (ShellExecute code={result}).")
            return False
        return True
    except Exception as e:
        print(f"[Optimizer] Warning: failed to request administrator relaunch on Windows: {e}")
        return False


def relaunch_unix_with_sudo():
    stdin_obj = getattr(sys, "stdin", None)
    if stdin_obj is None or not getattr(stdin_obj, "isatty", lambda: False)():
        print("[Optimizer] Warning: non-interactive terminal detected, skip sudo elevation request.")
        return False

    cmd = [
        "sudo",
        "-E",
        "env",
        "PYTHONIOENCODING=utf-8",
        "PYTHONUTF8=1",
        "QUANTADA_DISABLE_AUTO_ELEVATE=1",
        sys.executable,
        "-X",
        "utf8",
    ] + list(sys.argv)
    print(f"[Optimizer] Elevation command: {' '.join(cmd)}")
    try:
        os.execvp("sudo", cmd)
    except FileNotFoundError:
        print("[Optimizer] Warning: 'sudo' not found, skip automatic elevation.")
        return False
    except Exception as e:
        print(f"[Optimizer] Warning: failed to request sudo relaunch: {e}")
        return False
    return True


def request_optimizer_elevation_if_needed(args, resolve_worker_count):
    """
    Return True when an elevated relaunch was triggered and the current process
    should stop.
    """
    if not should_try_auto_elevation(args, resolve_worker_count):
        return False

    print_elevation_banner(args, resolve_worker_count)

    if sys.platform.startswith("win"):
        if relaunch_windows_as_admin():
            print("[Optimizer] Elevation request accepted. Relaunching elevated optimizer process...")
            return True
        print("[Optimizer] Continuing without elevation.")
        return False

    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        return relaunch_unix_with_sudo()

    print(f"[Optimizer] Warning: unsupported platform for auto-elevation: {sys.platform}")
    return False
