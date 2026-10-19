"""跨平台启动与当前终端脱离的长任务子进程。"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from common.data_paths import ensure_data_dir, resolve_data_path
import subprocess
from typing import Mapping, Sequence


def _default_log_path(cwd=None, data_path=".data"):
    """返回后台启动器自身的引导日志路径。数据目录不随 cwd 漂移。"""
    root = ensure_data_dir(resolve_data_path(data_path))
    directory = Path(ensure_data_dir(Path(root) / "optimizer"))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return directory / f"resume_launcher_{stamp}_{os.getpid()}.log"


def spawn_detached_python(
    argv: Sequence[str],
    *,
    cwd=None,
    env: Mapping[str, str] | None = None,
    log_file=None,
    data_path=".data",
):
    """启动不依赖当前终端的 Python 子进程，并返回 ``(pid, log_path)``。"""
    command = [str(item) for item in argv]
    if not command:
        raise ValueError("detached Python command cannot be empty")

    working_directory = str(Path(cwd or os.getcwd()).resolve())
    output_path = Path(log_file).expanduser() if log_file else _default_log_path(working_directory, data_path)
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process_env = os.environ.copy()
    if env is not None:
        process_env.update({str(key): str(value) for key, value in env.items()})
    process_env.setdefault("PYTHONUTF8", "1")
    process_env.setdefault("PYTHONIOENCODING", "utf-8")

    popen_options = {
        "cwd": working_directory,
        "env": process_env,
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0) or 0)
        if flags:
            popen_options["creationflags"] = flags
    else:
        popen_options["start_new_session"] = True

    with output_path.open("a", encoding="utf-8", buffering=1) as stream:
        process = subprocess.Popen(command, stdout=stream, **popen_options)
    return int(process.pid), str(output_path)


__all__ = ["spawn_detached_python"]
