"""跨平台识别并停止指定进程及其后代。"""

from __future__ import annotations

import ctypes
import datetime
import os
import re
import signal
import subprocess
import sys
import time


def parse_optimizer_owner(owner):
    """解析优化器保存的运行时间与主进程号。"""
    match = re.search(r"_RUN(\d{8}-\d{6})_(\d+)$", str(owner or ""))
    if not match:
        return None
    try:
        stamp = datetime.datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").timestamp()
        pid = int(match.group(2))
    except (OverflowError, OSError, ValueError):
        return None
    return {"pid": pid, "owner_time": stamp, "owner": str(owner)}


def _linux_process_table():
    """读取 Linux 的进程父子关系和内核启动标识。"""
    table = {}
    try:
        with open("/proc/stat", encoding="ascii") as stream:
            boot_line = next(line for line in stream if line.startswith("btime "))
        boot_time = float(boot_line.split()[1])
        clock_ticks = float(os.sysconf("SC_CLK_TCK"))
        names = os.listdir("/proc")
    except (OSError, StopIteration, ValueError):
        return table

    for name in names:
        if not name.isdecimal():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{name}/stat", encoding="ascii") as stream:
                stat = stream.read()
            end = stat.rfind(")")
            fields = stat[end + 1:].split()
            ppid = int(fields[1])
            start_ticks = int(fields[19])
            with open(f"/proc/{name}/cmdline", "rb") as stream:
                command = stream.read().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            table[pid] = {
                "pid": pid,
                "ppid": ppid,
                "identity": start_ticks,
                "started": boot_time + start_ticks / clock_ticks,
                "command": command,
            }
        except (OSError, IndexError, ValueError):
            continue
    return table


def _windows_process_table():
    """通过 Toolhelp 和进程句柄读取 Windows 进程树。"""
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    table = {}
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    invalid = ctypes.c_void_p(-1).value
    if snapshot == invalid:
        return table
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            pid = int(entry.pid)
            record = {"pid": pid, "ppid": int(entry.parent), "identity": None, "started": None,
                      "command": str(entry.exe)}
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    stamps = [wintypes.FILETIME() for _ in range(4)]
                    if kernel.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in stamps)):
                        identity = (stamps[0].dwHighDateTime << 32) + stamps[0].dwLowDateTime
                        record["identity"] = identity
                        record["started"] = identity / 10**7 - 11644473600
                finally:
                    kernel.CloseHandle(handle)
            table[pid] = record
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return table


def _ps_process_table():
    """读取 macOS 进程树；启动时间用于排除已复用的进程号。"""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,lstart=,command="],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\w+\s+\w+\s+\d+\s+\d+:\d+:\d+\s+\d+)\s+(.*)$", line)
        if not match:
            continue
        try:
            started = datetime.datetime.strptime(match.group(3), "%a %b %d %H:%M:%S %Y").timestamp()
            pid, ppid = int(match.group(1)), int(match.group(2))
        except (OverflowError, OSError, ValueError):
            continue
        table[pid] = {"pid": pid, "ppid": ppid, "identity": started,
                      "started": started, "command": match.group(4)}
    return table


def _process_table():
    if os.name == "nt":
        return _windows_process_table()
    if sys.platform.startswith("linux"):
        return _linux_process_table()
    return _ps_process_table()


def _is_training_entry(record):
    command = str(record.get("command") or "").lower()
    if os.name == "nt":
        return "python" in command or "pypy" in command
    return "run.py" in command and "python" in command


def optimizer_owner_process(owner):
    """验证优化器主进程仍是记录该运行标识的原进程。"""
    parsed = parse_optimizer_owner(owner)
    if parsed is None:
        return None
    record = _process_table().get(parsed["pid"])
    if record is None or record.get("started") is None:
        return None
    if float(record["started"]) > parsed["owner_time"] + 3.0:
        return None
    if not _is_training_entry(record):
        return None
    return {**record, **parsed}


def optimizer_process_tree(owner):
    """返回已验证优化器主进程及其当前全部后代。"""
    root = optimizer_owner_process(owner)
    if root is None:
        return []
    table = _process_table()
    current_root = table.get(root["pid"])
    if current_root is None or current_root.get("identity") != root.get("identity"):
        return []
    descendants = {root["pid"]: {**current_root, "depth": 0}}
    changed = True
    while changed:
        changed = False
        parents = set(descendants)
        for pid, record in table.items():
            if pid in descendants or record.get("ppid") not in parents:
                continue
            descendants[pid] = {**record, "depth": descendants[record["ppid"]]["depth"] + 1}
            changed = True
    return list(descendants.values())


def _same_process(record):
    identity = record.get("identity")
    if identity is None:
        return False
    current = _process_table().get(int(record["pid"]))
    return bool(current and current.get("identity") is not None and current.get("identity") == identity)


def _signal_process(record, sig):
    if not _same_process(record):
        return False
    if os.name == "nt":
        return False
    try:
        os.kill(int(record["pid"]), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def stop_optimizer_owners(owners, *, grace_seconds=15.0):
    """先中断优化器主进程，再有界清理仍存活的训练后代。"""
    unique = {}
    for owner in owners or ():
        root = optimizer_owner_process(owner)
        if root is not None:
            unique[(root["pid"], root["identity"])] = (str(owner), root)

    trees = {}
    for key, (owner, _root) in unique.items():
        tree = optimizer_process_tree(owner)
        if tree:
            trees[key] = tree

    if os.name == "nt":
        outcomes = {}
        for (_pid, _identity), (owner, root) in unique.items():
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(root["pid"]), "/T", "/F"],
                    capture_output=True, text=True, check=False, timeout=30,
                )
                if result.returncode == 0:
                    outcomes[owner] = "stopped"
                elif optimizer_owner_process(owner) is None:
                    outcomes[owner] = "already_stopped"
                else:
                    outcomes[owner] = "stop_failed"
            except (OSError, subprocess.SubprocessError):
                outcomes[owner] = "stop_failed"
        return outcomes

    roots = [tree[0] for tree in trees.values()]
    for root in roots:
        _signal_process(root, signal.SIGINT)

    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while time.monotonic() < deadline:
        alive = [record for tree in trees.values() for record in tree if _same_process(record)]
        if not alive:
            break
        time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))

    survivors = [
        record for tree in trees.values() for record in reversed(tree)
        if _same_process(record)
    ]
    for record in survivors:
        _signal_process(record, signal.SIGTERM)

    force_deadline = time.monotonic() + 2.0
    while time.monotonic() < force_deadline:
        survivors = [record for record in survivors if _same_process(record)]
        if not survivors:
            break
        time.sleep(0.05)
    for record in survivors:
        _signal_process(record, signal.SIGKILL)

    outcomes = {}
    for (_pid, _identity), (owner, _root) in unique.items():
        tree = trees.get((_pid, _identity), [])
        outcomes[owner] = "stopped" if not any(_same_process(record) for record in tree) else "stop_incomplete"
    return outcomes


__all__ = [
    "optimizer_owner_process", "optimizer_process_tree", "parse_optimizer_owner",
    "stop_optimizer_owners",
]
