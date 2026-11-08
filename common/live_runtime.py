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


def order_created_before(created_at, before):
    """判断在途委托是否创建于 before 之前（隔夜清理边界）。

    返回 True/False；创建时间缺失、无法解析或无法比较时返回 None，由调用方按“未知”处理。
    带时区与无时区时间比较时，无时区一侧按带时区一侧的时区解释：适配器给出的时间戳和调度 now 都是市场时钟，
    不能用运行机器的本地时区，否则非市场时区的主机会把当日委托误判为隔夜单。
    """
    if created_at is None or before is None:
        return None
    import pandas as pd

    try:
        created = pd.Timestamp(created_at)
        boundary = pd.Timestamp(before)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(created) or pd.isna(boundary):
        return None
    try:
        if (created.tzinfo is None) != (boundary.tzinfo is None):
            if created.tzinfo is None:
                created = created.tz_localize(boundary.tzinfo)
            else:
                boundary = boundary.tz_localize(created.tzinfo)
        return bool(created < boundary)
    except Exception:
        return None
