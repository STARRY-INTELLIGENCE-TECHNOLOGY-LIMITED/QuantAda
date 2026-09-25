import subprocess
import sys
import os
import shlex


def get_current_command():
    return subprocess.list2cmdline(["python"] + list(sys.argv))


def format_cli_command(arguments, shell=None):
    """把参数数组渲染为本机可复制命令，防止引号、美元符号和反引号触发展开。"""
    shell = shell or ("powershell" if os.name == "nt" else "bash")
    if shell.lower() in {"powershell", "pwsh", "windows", "ps"}:
        return "& " + " ".join("'" + str(item).replace("'", "''") + "'" for item in arguments)
    return shlex.join(str(item) for item in arguments)
