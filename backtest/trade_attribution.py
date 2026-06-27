import math
from collections import defaultdict
from collections.abc import Mapping


def _get_trade_field(trade, field_name, default=None):
    if isinstance(trade, Mapping):
        return trade.get(field_name, default)
    return getattr(trade, field_name, default)


def _to_finite_float(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _clean_symbol(value):
    if value in (None, ""):
        return "UNKNOWN"
    text = str(value).strip()
    return text or "UNKNOWN"


def calculate_trade_attribution(closed_trades):
    """
    Aggregate closed trades by symbol.

    PnL contribution uses abs(total_net_pnl) as denominator when the whole
    account is net losing, so profitable symbols stay positive and losing
    symbols stay negative in attribution tables.
    """
    grouped = defaultdict(lambda: {
        "symbol": "UNKNOWN",
        "trade_count": 0,
        "win_count": 0,
        "total_pnl": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
    })

    total_net_pnl = 0.0
    valid_trade_count = 0

    for trade in closed_trades or []:
        pnl = _to_finite_float(_get_trade_field(trade, "pnl"))
        if pnl is None:
            continue

        symbol = _clean_symbol(_get_trade_field(trade, "symbol"))
        bucket = grouped[symbol]
        bucket["symbol"] = symbol
        bucket["trade_count"] += 1
        bucket["total_pnl"] += pnl
        total_net_pnl += pnl
        valid_trade_count += 1

        if pnl > 0:
            bucket["win_count"] += 1
            bucket["gross_profit"] += pnl
        elif pnl < 0:
            bucket["gross_loss"] += pnl

    denominator = abs(total_net_pnl) if total_net_pnl != 0 else None
    rows = []
    for bucket in grouped.values():
        trade_count = bucket["trade_count"]
        gross_profit = bucket["gross_profit"]
        gross_loss_abs = abs(bucket["gross_loss"])

        if gross_loss_abs > 0:
            profit_factor = gross_profit / gross_loss_abs
        elif gross_profit > 0:
            profit_factor = float("inf")
        else:
            profit_factor = float("nan")

        rows.append({
            "symbol": bucket["symbol"],
            "trade_count": trade_count,
            "win_rate_pct": (bucket["win_count"] / trade_count * 100.0) if trade_count else float("nan"),
            "profit_factor": profit_factor,
            "total_pnl": bucket["total_pnl"],
            "pnl_contribution_pct": (
                bucket["total_pnl"] / denominator * 100.0
                if denominator
                else float("nan")
            ),
        })

    rows.sort(key=lambda item: _sort_number(item["pnl_contribution_pct"]), reverse=True)
    return {
        "total_net_pnl": total_net_pnl,
        "valid_trade_count": valid_trade_count,
        "rows": rows,
    }


def calculate_winning_trade_mae(closed_trades):
    """
    Calculate MAE for trades that closed with positive PnL.

    MAE follows the requested formula:
    lowest_price_during_trade / entry_price - 1
    """
    maes = []
    winning_trade_count = 0

    for trade in closed_trades or []:
        pnl = _to_finite_float(_get_trade_field(trade, "pnl"))
        if pnl is None or pnl <= 0:
            continue

        winning_trade_count += 1
        entry_price = _to_finite_float(_get_trade_field(trade, "entry_price"))
        lowest_price = _to_finite_float(_get_trade_field(trade, "lowest_price_during_trade"))
        if entry_price is None or entry_price <= 0 or lowest_price is None:
            continue

        maes.append(lowest_price / entry_price - 1.0)

    return {
        "winning_trade_count": winning_trade_count,
        "mae_sample_count": len(maes),
        "average_mae": (sum(maes) / len(maes)) if maes else float("nan"),
        "worst_mae": min(maes) if maes else float("nan"),
    }


def format_trade_micro_attribution_report(closed_trades):
    attribution = calculate_trade_attribution(closed_trades)
    mae = calculate_winning_trade_mae(closed_trades)

    lines = [
        "",
        "=" * 50,
        "          Trade Attribution by Symbol",
        "=" * 50,
    ]

    attribution_rows = [
        [
            row["symbol"],
            str(row["trade_count"]),
            _format_pct_value(row["win_rate_pct"]),
            _format_float(row["profit_factor"]),
            _format_money(row["total_pnl"]),
            _format_pct_value(row["pnl_contribution_pct"]),
        ]
        for row in attribution["rows"]
    ]

    if attribution_rows:
        lines.append(_format_ascii_table(
            ["Symbol", "Trades", "Win Rate", "PF", "Net PnL", "PnL Contrib"],
            attribution_rows,
            aligns=["left", "right", "right", "right", "right", "right"],
        ))
    else:
        lines.append(" No closed trades available for attribution.")

    lines.extend([
        "-" * 50,
        f" Total Net PnL:       {_format_money(attribution['total_net_pnl'])}",
        f" Valid Closed Trades: {attribution['valid_trade_count']}",
        "=" * 50,
        "          Winning Trade MAE Statistics",
        "=" * 50,
        _format_ascii_table(
            ["Winning Trades", "MAE Samples", "Average MAE", "Worst MAE"],
            [[
                str(mae["winning_trade_count"]),
                str(mae["mae_sample_count"]),
                _format_ratio_as_pct(mae["average_mae"]),
                _format_ratio_as_pct(mae["worst_mae"]),
            ]],
            aligns=["right", "right", "right", "right"],
        ),
        "=" * 50,
        "",
    ])

    return "\n".join(lines)


def print_trade_micro_attribution_report(closed_trades, logger=None):
    report = format_trade_micro_attribution_report(closed_trades)
    emit = logger.info if hasattr(logger, "info") else logger
    if emit is None:
        print(report)
        return
    for line in report.splitlines():
        emit(line)


def _sort_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return float("-inf")
    if math.isnan(value):
        return float("-inf")
    return float(value)


def _format_float(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "N/A"
    if math.isnan(value):
        return "N/A"
    if math.isinf(value):
        return "Inf" if value > 0 else "-Inf"
    return f"{float(value):.2f}"


def _format_pct_value(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "N/A"
    if math.isnan(value):
        return "N/A"
    if math.isinf(value):
        return "Inf%" if value > 0 else "-Inf%"
    return f"{float(value):.2f}%"


def _format_ratio_as_pct(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "N/A"
    if math.isnan(value):
        return "N/A"
    if math.isinf(value):
        return "Inf%" if value > 0 else "-Inf%"
    return f"{float(value) * 100.0:.2f}%"


def _format_money(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "N/A"
    if math.isnan(value):
        return "N/A"
    if math.isinf(value):
        return "Inf" if value > 0 else "-Inf"
    return f"{float(value):,.2f}"


def _format_ascii_table(headers, rows, aligns=None):
    aligns = aligns or ["left"] * len(headers)
    normalized_rows = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(str(header)), *(len(row[idx]) for row in normalized_rows))
        for idx, header in enumerate(headers)
    ]

    def border():
        return "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def format_row(row):
        cells = []
        for idx, cell in enumerate(row):
            align = aligns[idx] if idx < len(aligns) else "left"
            text = str(cell)
            if align == "right":
                text = text.rjust(widths[idx])
            else:
                text = text.ljust(widths[idx])
            cells.append(f" {text} ")
        return "|" + "|".join(cells) + "|"

    output = [border(), format_row(headers), border()]
    output.extend(format_row(row) for row in normalized_rows)
    output.append(border())
    return "\n".join(output)
