import pytest

from backtest.option_stats import summarize_option_trades


def test_summarize_option_trades_uses_average_risk_capital_and_short_mae():
    result = summarize_option_trades([
        {
            "side": "short",
            "pnl": 100.0,
            "entry_price": 2.0,
            "highest_price_during_trade": 2.5,
            "risk_capital": 1000.0,
            "premium_credit": 200.0,
        },
        {
            "side": "short",
            "pnl": -50.0,
            "entry_price": 2.0,
            "highest_price_during_trade": 3.0,
            "risk_capital": 1000.0,
            "premium_credit": 200.0,
        },
    ], years=1.0)

    assert result["option_trade_count"] == 2
    assert result["option_total_pnl"] == pytest.approx(50.0)
    assert result["option_risk_capital_sum"] == pytest.approx(2000.0)
    assert result["option_annual_risk_return_pct"] == pytest.approx(5.0)
    assert result["option_premium_yield_pct"] == pytest.approx(40.0)
    assert result["option_worst_mae_pct"] == pytest.approx(50.0)
    assert result["option_risk_data_coverage"] == pytest.approx(1.0)


def test_summarize_option_trades_nets_protective_long_debit():
    result = summarize_option_trades([
        {
            "side": "short",
            "pnl": 80.0,
            "entry_price": 2.0,
            "highest_price_during_trade": 2.2,
            "risk_capital": 1000.0,
            "premium_credit": 200.0,
        },
        {
            "side": "long",
            "pnl": -30.0,
            "entry_price": 1.0,
            "contract_multiplier": 100.0,
            "premium_credit": -100.0,
        },
    ], years=1.0)

    assert result["option_trade_count"] == 1
    assert result["option_total_pnl"] == pytest.approx(50.0)
    assert result["option_gross_credit"] == pytest.approx(100.0)
    assert result["option_annual_risk_return_pct"] == pytest.approx(5.0)
    assert result["option_premium_yield_pct"] == pytest.approx(10.0)


def test_summarize_option_trades_annualizes_premium_yield():
    result = summarize_option_trades([
        {
            "side": "short",
            "pnl": 100.0,
            "entry_price": 2.0,
            "highest_price_during_trade": 2.5,
            "risk_capital": 1000.0,
            "premium_credit": 200.0,
        },
        {
            "side": "short",
            "pnl": -50.0,
            "entry_price": 2.0,
            "highest_price_during_trade": 3.0,
            "risk_capital": 1000.0,
            "premium_credit": 200.0,
        },
    ], years=2.0)

    assert result["option_annual_risk_return_pct"] == pytest.approx(2.5)
    assert result["option_premium_yield_pct"] == pytest.approx(20.0)
