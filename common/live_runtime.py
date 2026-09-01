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


def dependency_install_hint(package_name: str, error=None) -> str:
    """生成可选第三方依赖缺失时的统一安装提示。"""
    message = (
        f"缺少可选依赖 {package_name!r}。请编辑 requirements.txt，解除 {package_name} 对应行的注释，"
        "然后执行 python -m pip install -r requirements.txt。"
    )
    if error is not None:
        message += f" 原始错误: {error}"
    return message
