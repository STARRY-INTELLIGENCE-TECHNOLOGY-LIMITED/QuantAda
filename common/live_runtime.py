import datetime


def runtime_print(message, now=None):
    """
    为长时间运行的实盘消息添加本地时间戳后输出。

    本函数保持轻量且无副作用，供券商启动循环共享带时间戳的诊断能力，
    不把券商专属的恢复逻辑移动到 BaseLiveBroker。
    """
    ts = now or datetime.datetime.now()
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if hasattr(ts, "strftime"):
        ts_text = ts.strftime("%Y-%m-%d %H:%M:%S")
    else:
        ts_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts_text}] {message}")
