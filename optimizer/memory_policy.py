"""优化器的内存观测与 worker 预算。

worker 配额以物理可用内存为主；swap/pagefile 只按有界比例计入，避免把虚拟内存当成可并行容量。
Windows 的 ullAvailPageFile 已是剩余 commit，不得再与 avail_phys 相加。
"""

import ctypes
import os
from pathlib import Path
import re
import subprocess
import sys


_GB = 1024 ** 3
# 每个 worker 的解释器、NumPy/Pandas、Optuna 与 Backtrader 底噪。
_PROCESS_OVERHEAD_BYTES = 512 * 1024 ** 2
# 行情需按进程复制时，为 cerebro 再拷一份预留余量。
_COPY_PAYLOAD_FACTOR = 1.20
# fork COW 或共享内存映射时，worker 仍会把行情写入本地 feed。
_SHARED_PAYLOAD_FACTOR = 0.30
# 只把一小部分 swap 当作缓冲，防止 16C32G Windows 把 pagefile 当成 31 路并发容量。
_SWAP_USE_RATIO = 0.10


def _reserve_ratio():
    """读取优化器内存余量配置，并限制到安全数值范围。"""
    try:
        import config

        value = float(getattr(config, "OPTIMIZER_MEMORY_RESERVE_RATIO", 0.05))
    except (ImportError, TypeError, ValueError, OverflowError):
        value = 0.05
    if not 0.0 <= value < 0.95:
        return 0.05
    return value


def windows_physical_and_swap(avail_phys, avail_page_file):
    """把 Windows 内存计数拆成物理空闲和 pagefile 空闲。

    ullAvailPageFile 是剩余 commit（已含未用物理内存）。pagefile 空闲取其与物理空闲之差。
    """
    physical = max(0, int(avail_phys))
    remaining_commit = max(0, int(avail_page_file))
    swap = max(0, remaining_commit - physical)
    return physical, swap


def _psutil_memory():
    """优先用 psutil 读取物理可用与 swap 空闲。"""
    try:
        import psutil

        physical = int(psutil.virtual_memory().available)
        swap = int(psutil.swap_memory().free)
        return max(0, physical), max(0, swap)
    except Exception:
        return None


def _macos_memory():
    """回退读取 macOS vm_stat 与 vm.swapusage。"""
    try:
        raw = subprocess.check_output(["vm_stat"], text=True, stderr=subprocess.DEVNULL)
        page_match = re.search(r"page size of (\d+) bytes", raw)
        page_size = int(page_match.group(1)) if page_match else 4096
        pages = 0
        for label in ("Pages free", "Pages inactive", "Pages speculative"):
            match = re.search(rf"{re.escape(label)}:\s+(\d+)", raw)
            if match:
                pages += int(match.group(1))
        physical = pages * page_size
        swap_raw = subprocess.check_output(
            ["sysctl", "-n", "vm.swapusage"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        free_match = re.search(r"free\s*=\s*([0-9.]+)([KMGTP])", swap_raw, re.IGNORECASE)
        swap = 0
        if free_match:
            units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
            swap = int(float(free_match.group(1)) * units[free_match.group(2).upper()])
        return max(0, physical), max(0, swap)
    except Exception:
        return None


def _windows_memory():
    """读取 Windows 物理空闲与 pagefile 空闲，不把剩余 commit 与物理空闲相加。"""
    try:
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint32),
                ("memory_load", ctypes.c_uint32),
                ("total_phys", ctypes.c_uint64),
                ("avail_phys", ctypes.c_uint64),
                ("total_page_file", ctypes.c_uint64),
                ("avail_page_file", ctypes.c_uint64),
                ("total_virtual", ctypes.c_uint64),
                ("avail_virtual", ctypes.c_uint64),
                ("avail_extended_virtual", ctypes.c_uint64),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return windows_physical_and_swap(status.avail_phys, status.avail_page_file)
    except Exception:
        pass
    return None


def _linux_memory():
    """读取 /proc/meminfo 的 MemAvailable 与 SwapFree。"""
    try:
        meminfo = Path("/proc/meminfo")
        if not meminfo.exists():
            return None
        lines = meminfo.read_text(encoding="ascii", errors="ignore").splitlines()
        physical = None
        swap = 0
        for line in lines:
            if line.startswith("MemAvailable:"):
                physical = int(line.split()[1]) * 1024
            elif line.startswith("SwapFree:"):
                swap = int(line.split()[1]) * 1024
        if physical is None:
            return None
        return max(0, physical), max(0, swap)
    except Exception:
        return None


def read_available_memory():
    """返回 (物理可用字节, swap 空闲字节)；无法读取时对应项为 None。

    Windows 必须先拆剩余 commit，不能把 psutil.swap_memory().free 当成 pagefile。
    Linux 用 MemAvailable/SwapFree；macOS 优先 psutil。
    """
    if os.name == "nt":
        windows_value = _windows_memory()
        if windows_value is not None:
            return windows_value
        psutil_value = _psutil_memory()
        if psutil_value is not None:
            return windows_physical_and_swap(psutil_value[0], psutil_value[1])
        return None, None

    if sys.platform == "darwin":
        psutil_value = _psutil_memory()
        if psutil_value is not None:
            return psutil_value
        mac_value = _macos_memory()
        if mac_value is not None:
            return mac_value
        return None, None

    psutil_value = _psutil_memory()
    if psutil_value is not None:
        return psutil_value
    linux_value = _linux_memory()
    if linux_value is not None:
        return linux_value

    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size), 0
    except (AttributeError, OSError, ValueError):
        return None, None


def usable_memory_bytes(physical_bytes, swap_bytes=0, reserve_ratio=None):
    """计算 worker 可用池：物理内存扣余量后，再加上有界比例的 swap。"""
    physical = max(0, int(physical_bytes or 0))
    swap = max(0, int(swap_bytes or 0))
    ratio = _reserve_ratio() if reserve_ratio is None else float(reserve_ratio)
    if not 0.0 <= ratio < 0.95:
        ratio = 0.05
    reserve = int(physical * ratio)
    return max(0, physical - reserve) + int(swap * _SWAP_USE_RATIO)


def available_physical_bytes():
    """返回物理可用内存；读不到时返回 None。"""
    physical, _swap = read_available_memory()
    return physical


def available_swap_bytes():
    """返回 swap/pagefile 空闲；读不到时返回 None。"""
    _physical, swap = read_available_memory()
    return swap


def available_memory_bytes():
    """返回 worker 可用池；物理与 swap 都读不到时返回 None。"""
    physical, swap = read_available_memory()
    if physical is None and swap is None:
        return None
    return usable_memory_bytes(physical or 0, swap or 0)


def format_bytes(value):
    """把字节数格式化为简短日志文本。"""
    if value is None:
        return "unknown"
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(number) < 1024.0 or unit == "TB":
            return f"{number:.1f}{unit}"
        number /= 1024.0
    return f"{number:.1f}TB"


def estimate_payload_bytes(payload):
    """估算 worker 训练行情 payload 的内存量，不序列化完整对象。"""
    total = 0
    train_datas = (payload or {}).get("train_datas") or {}
    for frame in train_datas.values():
        try:
            total += int(frame.memory_usage(index=True, deep=True).sum())
        except Exception:
            total += 0
    return max(0, total)


def per_worker_bytes(payload_bytes, shared_memory=False):
    """单个 worker 的内存成本：进程底噪加上行情副本或映射开销。"""
    payload = max(0, int(payload_bytes or 0))
    factor = _SHARED_PAYLOAD_FACTOR if shared_memory else _COPY_PAYLOAD_FACTOR
    return _PROCESS_OVERHEAD_BYTES + int(payload * factor)


def resolve_memory_worker_count(
    requested_workers,
    payload_bytes,
    *,
    shared_memory=False,
    available_bytes=None,
    swap_bytes=None,
):
    """按物理可用内存限制 worker 数；显式高并行在内存足够时必须发满。"""
    requested = max(1, int(requested_workers or 1))
    if requested <= 1:
        return requested

    if available_bytes is None:
        physical, swap = read_available_memory()
        if swap_bytes is not None:
            swap = swap_bytes
        if physical is None:
            return requested
    else:
        physical = available_bytes
        swap = 0 if swap_bytes is None else swap_bytes

    usable = usable_memory_bytes(physical, swap or 0)
    cost = max(1, per_worker_bytes(payload_bytes, shared_memory=shared_memory))
    memory_workers = max(1, usable // cost)
    return max(1, min(requested, int(memory_workers)))
