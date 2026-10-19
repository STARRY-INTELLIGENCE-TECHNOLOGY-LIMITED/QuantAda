"""运行时数据目录解析与共享权限。

相对 DATA_PATH 固定到 run.py 同级，不随启动目录变化。绝对路径才是强指定。
创建目录时尽量设为 0o777，新文件设为 0o666，让其他用户也能读写；失败不中断任务。
"""

from __future__ import annotations

import os
from pathlib import Path

import config


_DIR_MODE = 0o777
_FILE_MODE = 0o666


def project_directory() -> Path:
    """run.py 所在目录。config.py 与它同级。"""
    return Path(config.__file__).resolve().parent


def resolve_data_path(value=None) -> str:
    """解析数据根。相对路径锚定项目目录，绝对路径原样使用。"""
    if value is None:
        value = getattr(config, "DATA_PATH", ".data")
    text = str(value or ".data").strip() or ".data"
    path = Path(text).expanduser()
    if path.is_absolute():
        return os.path.normpath(str(path))
    return os.path.normpath(str((project_directory() / path).resolve()))


def _chmod_wide(path, mode) -> None:
    """尽量放开权限。Windows 或不支持的文件系统失败时继续。"""
    try:
        os.chmod(path, mode)
    except OSError:
        return


def ensure_data_dir(path) -> str:
    """创建数据目录，并把新建目录和目标目录尽量设为 0o777。

    只放宽本次新建的父目录和目标目录，不改已经存在的更上层系统目录。
    自定义绝对路径与默认 .data 使用同一规则。
    """
    target = Path(resolve_data_path(path))
    missing = []
    cursor = target
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    for directory in reversed(missing):
        directory.mkdir(mode=_DIR_MODE, exist_ok=True)
        _chmod_wide(directory, _DIR_MODE)
    if target.is_dir():
        _chmod_wide(target, _DIR_MODE)
    return str(target)


def share_data_file(path) -> None:
    """新写入的数据文件尽量设为 0o666，便于其他用户读写。"""
    if path and os.path.isfile(path):
        _chmod_wide(path, _FILE_MODE)
