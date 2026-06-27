import math
from types import SimpleNamespace

import pytest

from backtest.trade_attribution import (
    calculate_trade_attribution,
    calculate_winning_trade_mae,
    format_trade_micro_attribution_report,
)


def test_calculate_trade_attribution_groups_and_sorts_by_pnl_contribution():
    trades = [
        {
            "symbol": "AAA",
            "entry_price": 10.0,
            "exit_price": 12.0,
            "pnl": 100.0,
            "pnl_pct": 0.20,
            "lowest_price_during_trade": 9.0,
        },
        {
            "symbol": "AAA",
            "entry_price": 12.0,
            "exit_price": 11.0,
            "pnl": -20.0,
            "pnl_pct": -0.083,
            "lowest_price_during_trade": 10.0,
        },
        SimpleNamespace(
            symbol="BBB",
            entry_price=20.0,
            exit_price=19.0,
            pnl=-30.0,
            pnl_pct=-0.05,
            lowest_price_during_trade=18.0,
        ),
        {
            "symbol": "CCC",
            "entry_price": 30.0,
            "exit_price": 33.0,
            "pnl": 50.0,
            "pnl_pct": 0.10,
            "lowest_price_during_trade": 29.0,
        },
    ]

    result = calculate_trade_attribution(trades)

    assert result["total_net_pnl"] == 100.0
    assert result["valid_trade_count"] == 4
    assert [row["symbol"] for row in result["rows"]] == ["AAA", "CCC", "BBB"]

    aaa = result["rows"][0]
    assert aaa["trade_count"] == 2
    assert aaa["win_rate_pct"] == 50.0
    assert aaa["profit_factor"] == 5.0
    assert aaa["total_pnl"] == 80.0
    assert aaa["pnl_contribution_pct"] == 80.0


def test_calculate_trade_attribution_handles_zero_and_negative_total_pnl():
    zero_result = calculate_trade_attribution([
        {"symbol": "AAA", "pnl": 100.0},
        {"symbol": "BBB", "pnl": -100.0},
    ])

    assert zero_result["total_net_pnl"] == 0.0
    assert all(math.isnan(row["pnl_contribution_pct"]) for row in zero_result["rows"])

    negative_result = calculate_trade_attribution([
        {"symbol": "AAA", "pnl": 40.0},
        {"symbol": "BBB", "pnl": -100.0},
    ])

    by_symbol = {row["symbol"]: row for row in negative_result["rows"]}
    assert negative_result["total_net_pnl"] == -60.0
    assert by_symbol["AAA"]["pnl_contribution_pct"] == 40.0 / 60.0 * 100.0
    assert by_symbol["BBB"]["pnl_contribution_pct"] == -100.0 / 60.0 * 100.0


def test_calculate_trade_attribution_profit_factor_edge_cases():
    result = calculate_trade_attribution([
        {"symbol": "WIN_ONLY", "pnl": 10.0},
        {"symbol": "FLAT_ONLY", "pnl": 0.0},
        {"symbol": "LOSS_ONLY", "pnl": -5.0},
    ])

    by_symbol = {row["symbol"]: row for row in result["rows"]}
    assert math.isinf(by_symbol["WIN_ONLY"]["profit_factor"])
    assert math.isnan(by_symbol["FLAT_ONLY"]["profit_factor"])
    assert by_symbol["LOSS_ONLY"]["profit_factor"] == 0.0


def test_calculate_winning_trade_mae_uses_winning_trades_with_valid_prices():
    result = calculate_winning_trade_mae([
        {"symbol": "AAA", "pnl": 10.0, "entry_price": 100.0, "lowest_price_during_trade": 95.0},
        {"symbol": "BBB", "pnl": 20.0, "entry_price": 50.0, "lowest_price_during_trade": 45.0},
        {"symbol": "CCC", "pnl": -5.0, "entry_price": 20.0, "lowest_price_during_trade": 10.0},
        {"symbol": "DDD", "pnl": 1.0, "entry_price": 0.0, "lowest_price_during_trade": 1.0},
    ])

    assert result["winning_trade_count"] == 3
    assert result["mae_sample_count"] == 2
    assert result["average_mae"] == pytest.approx((-0.05 + -0.10) / 2.0)
    assert result["worst_mae"] == pytest.approx(-0.10)


def test_format_trade_micro_attribution_report_prints_expected_ascii_sections():
    report = format_trade_micro_attribution_report([
        {"symbol": "AAA", "pnl": 10.0, "entry_price": 100.0, "lowest_price_during_trade": 90.0},
    ])

    assert "==================================================" in report
    assert "Trade Attribution by Symbol" in report
    assert "Winning Trade MAE Statistics" in report
    assert "| Symbol | Trades | Win Rate |  PF | Net PnL | PnL Contrib |" in report
    assert "| AAA    |      1 |  100.00% | Inf |   10.00 |     100.00% |" in report
    assert "| Winning Trades | MAE Samples | Average MAE | Worst MAE |" in report
    assert "|              1 |           1 |     -10.00% |   -10.00% |" in report
