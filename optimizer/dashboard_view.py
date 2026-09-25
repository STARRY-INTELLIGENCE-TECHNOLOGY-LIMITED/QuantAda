"""把多份训练 Journal 复制成 dashboard 只读视图。

训练 Journal 仍按 Study 拆分，本模块不写回、不合并这些文件。
Dashboard 启动时只过滤本线程的 Optuna 日志，不改共享 logger 级别。
"""

import logging
import os
import threading
from typing import NamedTuple

import optuna
from optuna.storages import InMemoryStorage, JournalStorage

try:
    from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock
except ImportError:
    from optuna.storages import JournalFileStorage as JournalFileBackend
    from optuna.storages import JournalFileOpenLock


class DashboardStorage(NamedTuple):
    """dashboard 使用的存储、可见 Study，以及跳过文件时的警告。"""

    storage: object
    study_names: tuple
    warnings: tuple
    aggregated: bool


def open_journal_storage(path):
    """打开已存在的 Journal。缺失文件不能打开，否则后端会创建空文件。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    return JournalStorage(JournalFileBackend(path, lock_obj=JournalFileOpenLock(path)))


def build_dashboard_storage(log_files):
    """单份 Journal 直接打开；多份复制到内存。打不开的跳过，全部失败则没有存储。"""
    existing, warnings = _existing_journals(log_files)
    if not existing:
        return DashboardStorage(None, (), tuple(warnings), False)
    if len(existing) == 1:
        return _open_direct(existing[0], warnings)

    opened = []
    for path in existing:
        try:
            storage = open_journal_storage(path)
            names = list(optuna.get_all_study_names(storage))
        except Exception as exc:
            warnings.append(f"Dashboard journal skipped: {path} ({_exception_text(exc)})")
            continue
        if not names:
            warnings.append(f"Dashboard journal has no study: {path}")
            continue
        opened.append((path, storage, names))

    if not opened:
        warnings.append(f"Dashboard aggregation failed. Opening the last journal only: {existing[-1]}")
        return _open_direct(existing[-1], warnings)
    if len(opened) == 1:
        path, storage, names = opened[0]
        return DashboardStorage(storage, tuple(names), tuple(warnings), False)

    memory = InMemoryStorage()
    copied = []
    used = set()
    for _path, storage, names in opened:
        for name in names:
            target = _unique_study_name(name, used)
            try:
                optuna.copy_study(
                    from_study_name=name,
                    from_storage=storage,
                    to_storage=memory,
                    to_study_name=target,
                )
            except Exception as exc:
                warnings.append(f"Dashboard study skipped: {name} ({_exception_text(exc)})")
                continue
            used.add(target)
            copied.append(target)
    if not copied:
        warnings.append(f"Dashboard aggregation failed. Opening the last journal only: {existing[-1]}")
        return _open_direct(existing[-1], warnings)
    return DashboardStorage(memory, tuple(copied), tuple(warnings), True)



_DASHBOARD_SIDE_LOGGERS = ("optuna_dashboard", "sqlalchemy", "bottle", "waitress", "werkzeug")
_DASHBOARD_LOG_FILTERS = {}
_DASHBOARD_LOG_FILTER_LOCK = threading.Lock()


class _DashboardThreadLogFilter(logging.Filter):
    """丢弃 Dashboard 线程上的 Optuna 日志，其它线程保持原样。"""

    def __init__(self, thread_id):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record):
        if getattr(record, "thread", None) != self.thread_id:
            return True
        name = getattr(record, "name", "") or ""
        return not (name == "optuna" or name.startswith("optuna."))


def _optuna_log_handlers():
    import optuna.logging as optuna_logging

    optuna_logging.get_logger("optuna")
    logger = logging.getLogger("optuna")
    handlers = list(logger.handlers)
    if logger.propagate:
        for handler in logging.getLogger().handlers:
            if handler not in handlers:
                handlers.append(handler)
    return handlers


def _silence_dashboard_side_loggers():
    for name in _DASHBOARD_SIDE_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def _detach_dashboard_filter(entry):
    filt, handlers = entry
    for handler in handlers:
        try:
            handler.removeFilter(filt)
        except ValueError:
            pass


def _install_dashboard_optuna_filter(thread_id):
    """同一 Dashboard 线程重复安装时替换旧过滤器，不叠加。"""
    handlers = _optuna_log_handlers()
    filt = _DashboardThreadLogFilter(thread_id)
    with _DASHBOARD_LOG_FILTER_LOCK:
        previous = _DASHBOARD_LOG_FILTERS.pop(thread_id, None)
        if previous is not None:
            _detach_dashboard_filter(previous)
        for handler in handlers:
            if filt in handler.filters:
                handler.filters.remove(filt)
            # 放在最前，避免后续 Trial 改写过滤器先计入 Dashboard 线程的噪声。
            handler.filters.insert(0, filt)
        _DASHBOARD_LOG_FILTERS[thread_id] = (filt, handlers)


def _remove_dashboard_optuna_filter(thread_id):
    with _DASHBOARD_LOG_FILTER_LOCK:
        previous = _DASHBOARD_LOG_FILTERS.pop(thread_id, None)
    if previous is not None:
        _detach_dashboard_filter(previous)


def begin_dashboard_log_scope():
    """静默 Dashboard 附属日志，并按线程过滤 Optuna。不改 optuna logger 级别。"""
    logger = logging.getLogger("optuna")
    saved_level = logger.level
    try:
        _silence_dashboard_side_loggers()
        _install_dashboard_optuna_filter(threading.get_ident())
    finally:
        if logger.level != saved_level:
            logger.setLevel(saved_level)
    return saved_level


def end_dashboard_log_scope(saved_level):
    """卸下当前线程的 Dashboard 日志过滤，并恢复进入前的 optuna logger 级别。"""
    logger = logging.getLogger("optuna")
    try:
        _remove_dashboard_optuna_filter(threading.get_ident())
    finally:
        if saved_level is not None and logger.level != saved_level:
            logger.setLevel(saved_level)


def launch_multi_metric_dashboard(job, log_files, *, port, port_in_use):
    """结束时把全部 metric Journal 交给同一个 dashboard，不合并训练文件。"""
    files = [str(path) for path in log_files if path]
    available = [path for path in files if os.path.isfile(path)]
    if job is None or not available:
        print("[Warning] Aggregated dashboard log file not found.")
        return False
    target_port = int(port)
    for _ in range(100):
        if not port_in_use(target_port):
            break
        target_port += 1
    else:
        print(f"[Warning] Could not find an available port starting from {port}.")
        target_port = int(port)
    print(f"[Info] Multi-metric training completed. Launching dashboard for {len(available)} journals.")
    print("[Info] Dashboard will run in foreground. Analyze results, then press Ctrl-C to exit.")
    job._launch_dashboard(available[-1], port=target_port, background=False, log_files=files)
    return True


def _existing_journals(log_files):
    seen = set()
    existing = []
    warnings = []
    for raw in log_files or []:
        text = str(raw or "").strip()
        if not text:
            continue
        path = os.path.abspath(text)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isfile(path):
            warnings.append(f"Dashboard journal not found: {path}")
            continue
        existing.append(path)
    return existing, warnings


def _open_direct(path, warnings):
    try:
        storage = open_journal_storage(path)
        names = tuple(optuna.get_all_study_names(storage))
    except Exception as exc:
        warnings.append(f"Dashboard journal skipped: {path} ({_exception_text(exc)})")
        return DashboardStorage(None, (), tuple(warnings), False)
    return DashboardStorage(storage, names, tuple(warnings), False)


def _unique_study_name(name, used):
    if name not in used:
        return name
    index = 2
    while True:
        candidate = f"{name}__{index}"
        if candidate not in used:
            return candidate
        index += 1


def _exception_text(exc):
    return f"{type(exc).__name__}: {exc}"
