import math


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def format_with_spec(value, spec=".2f", default="N/A"):
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    try:
        return f"{value:{spec}}"
    except (TypeError, ValueError):
        return default


def format_percent(value, default="N/A"):
    if not _is_number(value):
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    return f"{float(value):.2%}"


def format_float(value, digits=2, default="N/A"):
    if not _is_number(value):
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    return f"{float(value):.{digits}f}"


def format_rate(value, default="N/A"):
    if not _is_number(value):
        return default
    rate = float(value)
    if math.isnan(rate):
        return default
    # 兼容 [0,1] 或 [0,100] 两种胜率表示
    rate = rate * 100.0 if 0 <= rate <= 1 else rate
    return f"{rate:.2f}%"


def format_int(value, default="N/A"):
    if not _is_number(value):
        return default
    intval = float(value)
    if math.isnan(intval):
        return default
    return str(int(intval))


def format_recent_backtest_metrics(metrics):
    metrics = metrics or {}
    return {
        "annual_return": format_percent(metrics.get("annual_return")),
        "max_drawdown": format_percent(metrics.get("max_drawdown")),
        "calmar_ratio": format_float(metrics.get("calmar_ratio")),
        "sharpe_ratio": format_float(metrics.get("sharpe_ratio")),
        "total_trades": format_int(metrics.get("total_trades")),
        "win_rate": format_rate(metrics.get("win_rate")),
        "profit_factor": format_float(metrics.get("profit_factor")),
    }


def format_ranked_candidates_markdown(
    ranked_candidates,
    title="ranked_symbols",
    dt=None,
    score_digits=6,
):
    lines = [f"### {title}"]
    if dt is not None:
        dt_text = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        lines.append(f"- **时间**: `{dt_text.replace('T00:00:00', '').replace(' 00:00:00', '')}`")

    rows = list(ranked_candidates or [])
    if not rows:
        lines.append("1. (empty)")
        return "\n".join(lines)

    for idx, item in enumerate(rows, start=1):
        try:
            symbol, score = item
        except (TypeError, ValueError):
            symbol, score = item, None

        symbol_name = getattr(symbol, "_name", str(symbol))
        if score is None:
            lines.append(f"{idx}. {symbol_name}")
            continue

        try:
            score_value = float(score)
            score_text = str(score) if math.isnan(score_value) else f"{score_value:.{int(score_digits)}f}"
        except (TypeError, ValueError):
            score_text = str(score)
        lines.append(f"{idx}. {symbol_name} `{score_text}`")

    return "\n".join(lines)
