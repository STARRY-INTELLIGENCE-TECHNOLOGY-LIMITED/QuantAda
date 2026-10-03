"""保存训练 AI 分析结果，并在多组合训练完成后打开结果文件。"""

import datetime
import os
import subprocess
import sys

import config


def build_optimizer_result_file_path(run_dt=None, run_pid=None):
    """构造当前训练批次的 UTF-8 文本结果文件路径。"""
    directory = os.path.join(os.getcwd(), config.DATA_PATH, "optimizer")
    os.makedirs(directory, exist_ok=True)
    run_dt = run_dt or datetime.datetime.now()
    run_pid = os.getpid() if run_pid is None else run_pid
    name = f"optimizer_result_{run_dt.strftime('%Y%m%d-%H%M%S-%f')}_{run_pid}.txt"
    return os.path.join(directory, name)


def append_optimizer_result(path, text):
    """以统一的 LF 换行追加 UTF-8 训练结果。"""
    if not path or not text:
        return
    payload = str(text).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n\n"
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)


def open_optimizer_result_file(path):
    """使用固定的系统文本编辑器打开结果，避免出现打开方式选择窗口。"""
    if not path:
        return False
    try:
        if os.name == "nt":
            subprocess.Popen(["notepad.exe", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except (OSError, subprocess.SubprocessError):
        return False
    return True
