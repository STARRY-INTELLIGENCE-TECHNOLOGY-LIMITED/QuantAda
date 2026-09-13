"""命令工作台的跨平台子进程执行器。"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Sequence



def _decode_output_line(raw: bytes) -> str:
    """解码子进程一行输出；UTF-8 优先，Windows 本地编码回退。"""

    payload = raw.rstrip(b"\r\n")
    for encoding in ("utf-8", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


class CommandExecutor:
    """以 shell=False 执行当前工作台选中的命令。"""

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self.process: subprocess.Popen[bytes] | None = None
        self.started_at: float | None = None
        self._queue: Queue[tuple[str, str | int | float]] = Queue(maxsize=10_000)
        self._reader: threading.Thread | None = None
        self._state_lock = threading.RLock()

    @property
    def running(self) -> bool:
        """返回当前是否仍有命令运行。"""

        return self.process is not None and self.process.poll() is None

    def start(
        self,
        argv: Sequence[str],
        *,
        environ: dict[str, str] | None = None,
        on_line: Callable[[str], None] | None = None,
        on_done: Callable[[int, float], None] | None = None,
    ) -> None:
        """启动命令，并在后台线程读取合并后的输出。"""

        process_env = dict(os.environ)
        if environ:
            process_env.update({str(key): str(value) for key, value in environ.items()})
        # 强制子进程 UTF-8，避免 Windows 默认 GBK 把中文打成问号。
        process_env["PYTHONUTF8"] = "1"
        process_env["PYTHONIOENCODING"] = "utf-8"
        popen_options: dict[str, object] = {
            "cwd": self.project_root,
            "env": process_env,
            "shell": False,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "bufsize": 0,
        }
        if os.name == "nt":
            popen_options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_options["start_new_session"] = True
        with self._state_lock:
            if self.running:
                raise RuntimeError("已有命令正在运行")
            # ``Popen.poll()`` 先于 reader 线程结束；旧 reader 尚未完成时若
            # 立即复用 self.process，会把两次运行的输出、耗时和回调串接起来。
            if self._reader is not None and self._reader.is_alive():
                raise RuntimeError("上一条命令仍在收尾，请稍后重试")
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break
            process = subprocess.Popen([str(item) for item in argv], **popen_options)
            started_at = time.monotonic()
            self.process = process
            self.started_at = started_at
            self._reader = threading.Thread(
                target=self._read_output,
                args=(process, started_at, on_line, on_done),
                daemon=True,
            )
            self._reader.start()

    def _read_output(
        self,
        process: subprocess.Popen[bytes],
        started_at: float,
        on_line: Callable[[str], None] | None,
        on_done: Callable[[int, float], None] | None,
    ) -> None:
        try:
            if process.stdout is not None:
                for raw in iter(process.stdout.readline, b""):
                    text = _decode_output_line(raw)
                    self._put_event(("line", text))
                    if on_line:
                        try:
                            on_line(text)
                        except Exception:
                            # UI 回调不是进程生命周期的一部分；回调异常不能遗留
                            # 未关闭的管道或阻止完成事件发送。
                            pass
        finally:
            if process.stdout is not None:
                process.stdout.close()
        return_code = process.wait()
        duration = time.monotonic() - started_at
        self._put_event(("done", return_code))
        self._put_event(("duration", duration))
        if on_done:
            on_done(return_code, duration)

    def _put_event(self, event: tuple[str, str | int | float]) -> None:
        """有界写入事件队列；输出消费较慢时丢弃最旧行但保留进程状态。"""
        try:
            self._queue.put_nowait(event)
            return
        except Exception:
            pass
        try:
            self._queue.get_nowait()
        except Empty:
            return
        try:
            self._queue.put_nowait(event)
        except Exception:
            pass

    def drain(self) -> list[tuple[str, str | int | float]]:
        """取出尚未消费的输出事件。"""

        events: list[tuple[str, str | int | float]] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except Empty:
                return events

    def stop(self) -> bool:
        """停止当前子进程，返回是否确实发送了停止请求。"""

        process = self.process
        if process is None or process.poll() is not None:
            return False
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                return False
        return True
