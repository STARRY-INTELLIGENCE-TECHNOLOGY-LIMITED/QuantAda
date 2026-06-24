"""
Async terminal output logging utilities.

This module keeps stdout/stderr tee mechanics out of optimizer orchestration.
The optimizer decides the run name; this module only owns path construction,
environment propagation, and non-blocking terminal/file fan-out.
"""

import atexit
import datetime
import os
import queue
import re
import sys
import threading

import config


OPTIMIZER_TERMINAL_LOG_ENV = "QUANTADA_OPTIMIZER_TERMINAL_LOG"
_TERMINAL_LOG_TEE = None


def configure_text_stream_error_handling(streams=None, errors="backslashreplace"):
    """
    Keep console output from crashing on Windows legacy encodings.

    UTF-8 log files still receive the original text. This only changes how
    stdout/stderr degrade when the active terminal cannot encode a character.
    """
    target_streams = streams if streams is not None else (sys.stdout, sys.stderr)
    for stream in target_streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors=errors)
        except Exception:
            pass


def _safe_log_token(value, max_len=180):
    token = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "NA")).strip("_.-")
    if not token:
        token = "NA"
    return token[:max_len]


def build_optimizer_terminal_log_path(name_tag=None, run_dt=None, run_pid=None):
    log_dir = os.path.join(os.getcwd(), config.DATA_PATH, "optimizer")
    os.makedirs(log_dir, exist_ok=True)

    if name_tag:
        token = _safe_log_token(name_tag)
    else:
        run_dt = run_dt or datetime.datetime.now()
        run_pid = os.getpid() if run_pid is None else run_pid
        token = f"RUN{run_dt.strftime('%Y%m%d-%H%M%S')}_{run_pid}"

    return os.path.join(log_dir, f"optimizer_terminal_{token}.log")


def get_optimizer_terminal_log_path(default=None):
    return os.environ.get(OPTIMIZER_TERMINAL_LOG_ENV) or default


def set_optimizer_terminal_log_path(log_file):
    if log_file:
        os.environ[OPTIMIZER_TERMINAL_LOG_ENV] = str(log_file)


class _AsyncTerminalLogTee:
    """
    Non-blocking terminal tee.

    Writes continue to the original stdout/stderr immediately. File persistence
    happens on a bounded background queue so disk IO cannot throttle trials.
    """

    def __init__(self, log_file, queue_size=10000):
        self.log_file = str(log_file)
        self.pid = os.getpid()
        self._queue = queue.Queue(maxsize=max(100, int(queue_size)))
        self._dropped_chunks = 0
        self._closed = False
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._file = open(self.log_file, "a", encoding="utf-8", buffering=1)
        self._thread = threading.Thread(target=self._writer_loop, name="optimizer-terminal-log", daemon=True)
        self._thread.start()

    def install(self):
        sys.stdout = _TeeStream(self._original_stdout, self)
        sys.stderr = _TeeStream(self._original_stderr, self)

    def enqueue(self, text):
        if self._closed or not text:
            return
        if not isinstance(text, str):
            text = str(text)
        try:
            self._queue.put_nowait(text)
            return
        except queue.Full:
            pass

        try:
            self._queue.get_nowait()
            self._dropped_chunks += 1
        except queue.Empty:
            pass

        try:
            self._queue.put_nowait(text)
        except queue.Full:
            self._dropped_chunks += 1

    def _writer_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            try:
                self._file.write(item)
                if "\n" in item:
                    self._file.flush()
            except Exception:
                pass

        if self._dropped_chunks:
            try:
                self._file.write(
                    f"\n[Optimizer] Terminal log dropped {self._dropped_chunks} chunks because the async queue was full.\n"
                )
            except Exception:
                pass
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True

        if isinstance(sys.stdout, _TeeStream) and getattr(sys.stdout, "_owner", None) is self:
            sys.stdout = self._original_stdout
        if isinstance(sys.stderr, _TeeStream) and getattr(sys.stderr, "_owner", None) is self:
            sys.stderr = self._original_stderr

        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass

        try:
            self._thread.join(timeout=2.0)
        except Exception:
            pass


class _TeeStream:
    def __init__(self, original_stream, owner):
        self._original_stream = original_stream
        self._owner = owner

    def write(self, text):
        try:
            written = self._original_stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self._original_stream, "encoding", None) or sys.getdefaultencoding()
            fallback = str(text).encode(encoding, errors="backslashreplace").decode(encoding, errors="strict")
            written = self._original_stream.write(fallback)
        self._owner.enqueue(text)
        return written if written is not None else len(str(text))

    def flush(self):
        return self._original_stream.flush()

    def isatty(self):
        return self._original_stream.isatty()

    @property
    def encoding(self):
        return getattr(self._original_stream, "encoding", None)

    @property
    def errors(self):
        return getattr(self._original_stream, "errors", None)

    def __getattr__(self, name):
        return getattr(self._original_stream, name)


def install_optimizer_terminal_log(log_file=None, announce=True):
    global _TERMINAL_LOG_TEE

    configure_text_stream_error_handling()

    log_file = str(log_file or get_optimizer_terminal_log_path() or build_optimizer_terminal_log_path())
    set_optimizer_terminal_log_path(log_file)

    if (
        _TERMINAL_LOG_TEE is not None
        and not _TERMINAL_LOG_TEE._closed
        and getattr(_TERMINAL_LOG_TEE, "pid", None) == os.getpid()
    ):
        if os.path.abspath(_TERMINAL_LOG_TEE.log_file) == os.path.abspath(log_file):
            return _TERMINAL_LOG_TEE
        _TERMINAL_LOG_TEE.close()

    if _TERMINAL_LOG_TEE is not None and getattr(_TERMINAL_LOG_TEE, "pid", None) != os.getpid():
        try:
            sys.stdout = _TERMINAL_LOG_TEE._original_stdout
            sys.stderr = _TERMINAL_LOG_TEE._original_stderr
        except Exception:
            pass

    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    tee = _AsyncTerminalLogTee(log_file)
    tee.install()
    _TERMINAL_LOG_TEE = tee
    atexit.register(tee.close)

    if announce:
        print(f"[Optimizer] Terminal output tee: {log_file}")
    return tee
