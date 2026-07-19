from datetime import datetime
from types import SimpleNamespace

from backtest.backtester import Backtester


def test_backtester_flushes_deferred_plan_after_run(monkeypatch):
    calls = []

    monkeypatch.setattr("backtest.backtester.config.PRINT_PLAN", False)
    monkeypatch.setattr(
        "backtest.backtester.runtime_notifications.clear_deferred_plan",
        lambda: calls.append("clear"),
    )
    monkeypatch.setattr(
        "backtest.backtester.runtime_notifications.flush_deferred_plan",
        lambda: calls.append("flush"),
    )

    backtester = Backtester.__new__(Backtester)
    backtester.verbose = False
    backtester.cerebro = SimpleNamespace(
        broker=SimpleNamespace(getvalue=lambda: 100000.0),
        run=lambda: calls.append("run") or ["result"],
    )
    backtester._init_data_feeds = lambda: calls.append("init_data")
    backtester._init_strategy = lambda: calls.append("init_strategy")
    backtester._init_broker = lambda: calls.append("init_broker")
    backtester._process_recorder_hooks = lambda final_val: calls.append(("recorder", final_val))
    backtester._generate_report = lambda: calls.append("report")

    results = backtester.run()

    assert results == ["result"]
    assert calls == [
        "clear",
        "init_data",
        "init_strategy",
        "init_broker",
        "run",
        "flush",
        ("recorder", 100000.0),
        "report",
    ]


def test_backtester_pushes_final_performance_summary_after_plan_flush(monkeypatch):
    calls = []

    metrics = {
        "start_date": datetime(2024, 1, 1),
        "end_date": datetime(2024, 1, 31),
        "initial_portfolio": 100000.0,
        "final_portfolio": 112345.67,
        "total_return": 0.1234567,
        "annual_return": 1.234567,
        "sharpe_ratio": 2.0,
        "max_drawdown": -0.05,
        "calmar_ratio": 24.69,
        "total_trades": 8,
        "win_rate": 62.5,
        "profit_factor": 1.8,
        "pnl_ratio": 1.4,
    }

    monkeypatch.setattr("backtest.backtester.config.PRINT_PLAN", True)
    monkeypatch.setattr("backtest.backtester.config.is_alarms_enabled", lambda: True)
    monkeypatch.setattr(
        "backtest.backtester.runtime_notifications.clear_deferred_plan",
        lambda: calls.append("clear"),
    )
    monkeypatch.setattr(
        "backtest.backtester.runtime_notifications.flush_deferred_plan",
        lambda: calls.append("flush"),
    )
    monkeypatch.setattr(
        "backtest.backtester.runtime_notifications.push_plan",
        lambda content, level="INFO": calls.append(("performance", content, level)) or True,
    )
    monkeypatch.setattr(
        "backtest.backtester.runtime_command.get_current_command",
        lambda: "python run.py sample_strategy --no_plot --params \"{'x': 1}\"",
    )
    monkeypatch.setattr(Backtester, "get_performance_metrics", lambda self: metrics)
    monkeypatch.setattr(
        Backtester,
        "get_trade_micro_attribution_report",
        lambda self: (
            "\n==================================================\n"
            "          Trade Attribution by Symbol\n"
            "==================================================\n"
            " No closed trades available for attribution.\n"
        ),
    )

    backtester = Backtester.__new__(Backtester)
    backtester.verbose = False
    backtester.cerebro = SimpleNamespace(
        broker=SimpleNamespace(getvalue=lambda: 112345.67),
        run=lambda: calls.append("run") or ["result"],
    )
    backtester._init_data_feeds = lambda: calls.append("init_data")
    backtester._init_strategy = lambda: calls.append("init_strategy")
    backtester._init_broker = lambda: calls.append("init_broker")
    backtester._process_recorder_hooks = lambda final_val: calls.append(("recorder", final_val))
    backtester._generate_report = lambda: calls.append("report")

    results = backtester.run()

    assert results == ["result"]
    assert calls[5] == "flush"
    assert calls[6][0] == "performance"
    assert "### Backtest Command" in calls[6][1]
    assert "python run.py sample_strategy --no_plot --params \"{'x': 1}\"" in calls[6][1]
    assert "Trade Attribution by Symbol" in calls[6][1]
    assert "Backtest Performance Metrics" in calls[6][1]
    assert calls[6][1].index("Trade Attribution by Symbol") < calls[6][1].index("Backtest Performance Metrics")
    assert "Final Portfolio:      112,345.67" in calls[6][1]
    assert calls[7:] == [
        ("recorder", 112345.67),
        "report",
    ]


def test_backtester_skips_final_performance_summary_when_alarm_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr("backtest.backtester.config.PRINT_PLAN", True)
    monkeypatch.setattr("backtest.backtester.config.is_alarms_enabled", lambda: False)
    monkeypatch.setattr(
        "backtest.backtester.runtime_notifications.push_plan",
        lambda *args, **kwargs: calls.append("push"),
    )

    backtester = Backtester.__new__(Backtester)
    backtester.verbose = False

    assert backtester._push_backtest_performance_summary() is False
    assert calls == []
