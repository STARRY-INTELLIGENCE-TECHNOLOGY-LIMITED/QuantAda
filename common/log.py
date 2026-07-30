import datetime

import config
from common.order_quantity import format_quantity


def coerce_dt(dt):
    """
    归一化常见的时间类型，便于日志/报警统一使用真实执行时间。
    """
    if dt in (None, ""):
        return None

    if isinstance(dt, datetime.datetime):
        return dt

    if isinstance(dt, datetime.date):
        return datetime.datetime.combine(dt, datetime.time())

    if hasattr(dt, "to_pydatetime"):
        try:
            return dt.to_pydatetime()
        except Exception:
            pass

    if isinstance(dt, (int, float)):
        try:
            ts = float(dt)
            if ts <= 0:
                return None
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            if ts >= 1_000_000_000:
                return datetime.datetime.fromtimestamp(ts)
        except Exception:
            return None
        return None

    if isinstance(dt, str):
        raw = dt.strip()
        if not raw:
            return None

        if raw.isdigit():
            try:
                ts = float(raw)
                if len(raw) >= 13:
                    ts /= 1000.0
                if ts >= 1_000_000_000:
                    return datetime.datetime.fromtimestamp(ts)
            except Exception:
                pass

        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        parse_fmts = (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S.%f",
            "%Y/%m/%d %H:%M:%S",
            "%Y%m%d %H:%M:%S",
            "%Y%m%d %H%M%S",
        )

        try:
            return datetime.datetime.fromisoformat(normalized)
        except ValueError:
            pass

        for fmt in parse_fmts:
            try:
                return datetime.datetime.strptime(normalized, fmt)
            except ValueError:
                continue

    return None


def extract_order_execution_dt(order, fallback=None):
    """
    尽量从订单代理中提取“真实执行时间”。
    找不到时返回 fallback，由调用方决定是否回退到触发时间。
    """
    candidates = []

    executed = getattr(order, "executed", None)
    if executed is not None:
        candidates.extend([
            getattr(executed, "dt", None),
            getattr(executed, "datetime", None),
            getattr(executed, "time", None),
            getattr(executed, "timestamp", None),
        ])

    candidates.extend([
        getattr(order, "execution_dt", None),
        getattr(order, "execution_time", None),
        getattr(order, "filled_at", None),
        getattr(order, "filled_time", None),
        getattr(order, "updated_at", None),
        getattr(order, "updated_time", None),
    ])

    trade = getattr(order, "trade", None)
    fills = getattr(trade, "fills", None)
    if fills:
        try:
            last_fill = list(fills)[-1]
        except Exception:
            last_fill = None
        if last_fill is not None:
            candidates.extend([
                getattr(last_fill, "time", None),
                getattr(getattr(last_fill, "execution", None), "time", None),
            ])

    platform_order = getattr(order, "platform_order", None)
    if platform_order is not None:
        candidates.extend([
            getattr(platform_order, "filled_at", None),
            getattr(platform_order, "filled_time", None),
            getattr(platform_order, "updated_at", None),
            getattr(platform_order, "updated_time", None),
            getattr(platform_order, "transact_time", None),
            getattr(platform_order, "transaction_time", None),
        ])

    raw_order = getattr(order, "raw_order", None)
    if raw_order is not None:
        candidates.extend([
            getattr(raw_order, "filled_at", None),
            getattr(raw_order, "filled_time", None),
            getattr(raw_order, "updated_at", None),
            getattr(raw_order, "updated_time", None),
        ])

    for raw_dt in candidates:
        normalized = coerce_dt(raw_dt)
        if normalized is not None:
            return normalized

    return fallback


def _fmt_dt(dt=None):
    """
    内部助手：时间格式化
    - 如果传入 dt (回测时间/指定时间)，则格式化它
    - 如果未传入 (实盘)，则取当前系统时间
    """
    if dt:
        # 处理 backtrader 的 float 类型时间或其他类型
        if isinstance(dt, (float, int)):
            s = str(dt)  # 兜底
        elif hasattr(dt, 'isoformat'):
            s = dt.isoformat()
        else:
            s = str(dt)
    else:
        # 实盘/默认情况：取当前系统时间
        s = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 去除零点时间后缀，让日志更清爽
        # 同时处理 'T00:00:00' (isoformat) 和 ' 00:00:00' (str/strftime) 两种情况
    return s.replace('T00:00:00', '').replace(' 00:00:00', '')


def format_dt(dt=None):
    return _fmt_dt(dt)

def info(msg, dt=None):
    """普通日志"""
    if getattr(config, 'LOG', True):
        time_str = _fmt_dt(dt)
        print(f"[{time_str}] {msg}")


def warning(msg, dt=None):
    """警告日志"""
    time_str = _fmt_dt(dt)
    print(f"[WARN] [{time_str}] {msg}")


def error(msg, dt=None):
    """错误日志"""
    time_str = _fmt_dt(dt)
    print(f"[ERROR] [{time_str}] {msg}")


def signal(action, symbol, size, price, tag="信号触发", dt=None):
    """
    统一的交易信号日志
    :param action: 'BUY' 或 'SELL'
    :param symbol: 标的代码 string
    :param size: 数量
    :param price: 价格
    :param tag: 场景标签 (如: '实盘信号', '回测信号')
    :param dt: (可选) 显式指定时间，回测时传入回测时间，实盘时传 None 自动取当前
    """
    if getattr(config, 'LOG', True):
        # 增加防御性判断，确保 size 和 price 为数字
        safe_size = size if size is not None else 0
        safe_price = price if price is not None else 0.0

        action_tag = "BUY" if action == 'BUY' else "SELL"
        act_cn = "买入" if action == 'BUY' else "卖出"

        # est_val 必须确保是数字计算结果
        est_val = float(safe_size) * float(safe_price)

        # 修正格式化逻辑
        if est_val > 10000:
            val_str = f"{est_val / 10000:.2f}万"
        else:
            val_str = f"{est_val:.2f}元"

        time_str = _fmt_dt(dt)
        # 使用 safe_size 和 safe_price 打印
        size_text = format_quantity(safe_size)
        print(
            f"[{action_tag}] [{tag}] {time_str} {act_cn} {symbol:<12} 数量: {size_text:<12} 价格: {safe_price:.2f} (约 {val_str})")
