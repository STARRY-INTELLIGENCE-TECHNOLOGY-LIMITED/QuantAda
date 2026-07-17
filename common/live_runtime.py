import datetime


def runtime_print(message, now=None):
    """
    Print long-running live runtime messages with a local timestamp.

    This is intentionally tiny and side-effect-free so broker launch loops can
    share timestamped diagnostics without moving broker-specific recovery logic
    into BaseLiveBroker.
    """
    ts = now or datetime.datetime.now()
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if hasattr(ts, "strftime"):
        ts_text = ts.strftime("%Y-%m-%d %H:%M:%S")
    else:
        ts_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts_text}] {message}")
