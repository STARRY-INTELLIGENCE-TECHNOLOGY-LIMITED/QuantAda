import subprocess
import sys


def get_current_command():
    return subprocess.list2cmdline(["python"] + list(sys.argv))
