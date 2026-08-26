"""将 ib_insync 异步 API 调度到其所属事件循环的辅助函数。"""

import asyncio
import concurrent.futures
import inspect


def call_async_on_owner_loop(
    ib,
    method_name: str,
    event_loop,
    args=(),
    kwargs=None,
    timeout: float = 3.0,
):
    """在 SDK 所属事件循环中调用 ``IB.<method_name>Async``。

    ``ib_insync`` 的 ``*Async`` 方法有两种形式：一种是真正的协程函数，
    另一种是普通函数，但会同步创建绑定当前线程的 ``asyncio.Future``（例如
    ``reqPositionsAsync`` 和 ``reqAllOpenOrdersAsync``）。后一种方法如果先在
    工作线程调用，会在投递到 ``run_coroutine_threadsafe`` 之前因找不到事件
    循环而失败。本函数先把方法调用投递到所属事件循环，因此两种 API 都会
    使用接收 IB socket 回调的正确事件循环。
    """

    method = getattr(ib, f"{method_name}Async", None)
    if not callable(method):
        raise RuntimeError(f"IB.{method_name}Async is unavailable for cross-thread call")
    if event_loop is None:
        raise RuntimeError(f"IB event loop is unavailable; cannot run {method_name}")
    if event_loop.is_closed():
        raise RuntimeError(f"IB event loop is closed; cannot run {method_name}")

    try:
        wait_timeout = max(0.01, float(timeout))
    except (TypeError, ValueError, OverflowError):
        wait_timeout = 3.0

    result_future = concurrent.futures.Future()
    task_holder = {}
    call_kwargs = dict(kwargs or {})

    def _finish(task):
        if result_future.done():
            return
        try:
            result_future.set_result(task.result())
        except BaseException as exc:
            result_future.set_exception(exc)

    def _invoke():
        if result_future.cancelled():
            return
        try:
            awaitable = method(*args, **call_kwargs)
            if not inspect.isawaitable(awaitable):
                result_future.set_result(awaitable)
                return
            task = asyncio.ensure_future(awaitable, loop=event_loop)
            task_holder["task"] = task
            task.add_done_callback(_finish)
        except BaseException as exc:
            result_future.set_exception(exc)

    try:
        event_loop.call_soon_threadsafe(_invoke)
    except BaseException:
        result_future.cancel()
        raise

    try:
        return result_future.result(timeout=wait_timeout)
    except concurrent.futures.TimeoutError as exc:
        result_future.cancel()

        def _cancel_task():
            task = task_holder.get("task")
            if task is not None and not task.done():
                task.cancel()

        try:
            event_loop.call_soon_threadsafe(_cancel_task)
        except Exception:
            pass
        raise TimeoutError(f"IB.{method_name}Async timed out after {wait_timeout:.2f}s") from exc
