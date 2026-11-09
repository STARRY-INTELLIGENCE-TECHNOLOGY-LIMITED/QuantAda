from types import SimpleNamespace

import optimizer.runtime as runtime
from optimizer.reporting import classify_sample_window, print_optimizer_ai_summary

TRAIN = ("20230101", "20241231")


def test_classify_sample_window_in_partial_out():
    assert classify_sample_window("20230601", "20231231", TRAIN)[0] == "in"
    kind, text = classify_sample_window("20241001", "20250331", TRAIN)
    assert kind == "partial" and "样本内" in text and "%" in text
    assert classify_sample_window("20250101", "20250331", TRAIN) == ("out", "样本外")
    assert classify_sample_window("20250101", "20250331", None)[0] == "unknown"


def _metrics(start, end, annual=0.2):
    return {
        "start_date": start, "end_date": end, "annual_return": annual, "max_drawdown": 0.1,
        "sharpe_ratio": 1.0, "total_trades": 10,
    }


def test_summary_labels_rows_and_counts_out_of_sample(capsys):
    report = {
        "metric_name": "sharpe", "best_score": "1.0",
        "main_eval_backtest": _metrics("20230101", "20250331"),
        "test_backtest": _metrics("20250101", "20250331"),
        "yearly_backtests": [_metrics("20230101", "20231231"), _metrics("20250101", "20250331")],
    }
    print_optimizer_ai_summary(
        [report], 1, ["x"], test_set_requested=True, train_range=TRAIN,
    )
    out = capsys.readouterr().out
    assert "MainEval] 窗口 20230101->20250331: 样本内(重叠" in out
    assert "TestSet] 窗口 20250101->20250331: 样本外" in out
    assert "样本内(完全)" in out
    # 去重后共 3 个窗口 (MainEval、测试集与年度 2025 同窗口、2023 年)，其中 1 个样本外
    assert "共 3 个报告窗口，其中 1 个完全位于训练期(20230101->20241231)结束之后" in out


def test_summary_without_train_range_does_not_crash(capsys):
    report = {"metric_name": "sharpe", "main_eval_backtest": _metrics("20230101", "20250331")}
    print_optimizer_ai_summary([report], 1, ["x"])
    assert "归属未知" in capsys.readouterr().out


def _stress_job(monkeypatch, commission, slippage, fail=False):
    seen = []

    class DummyBacktester:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def run(self):
            if fail:
                raise RuntimeError("boom")

        def get_performance_metrics(self):
            return {"annual_return": 0.1, "sharpe_ratio": 0.5, "max_drawdown": 0.2, "total_trades": 7}

    monkeypatch.setattr(runtime, "Backtester", DummyBacktester)
    job = runtime.OptimizationJob.__new__(runtime.OptimizationJob)
    job.strategy_class = object()
    job.risk_control_classes = []
    job.risk_params = {}
    job.args = SimpleNamespace(
        cash=1000.0, commission=commission, slippage=slippage, timeframe="Days", compression=1,
    )
    job._infer_main_eval_window = lambda: ("20230101", "20250331")
    job._fetch_datas_for_window = lambda s, e: {"AAA": object()}
    return job, seen


def test_cost_stress_doubles_costs(monkeypatch):
    job, seen = _stress_job(monkeypatch, 0.0003, 0.001)
    got = job._run_cost_stress_backtest({"p": 1})
    assert seen[0]["commission"] == 0.0006 and seen[0]["slippage"] == 0.002
    assert got["total_trades"] == 7 and got["base_commission"] == 0.0003


def test_cost_stress_zero_base_uses_floor(monkeypatch):
    job, seen = _stress_job(monkeypatch, 0, 0)
    job._run_cost_stress_backtest({})
    assert seen[0]["commission"] == runtime.COST_STRESS_COMMISSION_FLOOR
    assert seen[0]["slippage"] == runtime.COST_STRESS_SLIPPAGE_FLOOR


def test_cost_stress_failure_returns_error_and_report_survives(monkeypatch, capsys):
    job, _ = _stress_job(monkeypatch, 0.0003, 0.001, fail=True)
    stress = job._run_cost_stress_backtest({})
    assert stress == {"error": "boom"}
    report = {
        "metric_name": "sharpe", "main_eval_backtest": _metrics("20230101", "20250331"),
        "cost_stress_backtest": stress,
    }
    print_optimizer_ai_summary([report], 1, ["x"], train_range=TRAIN)
    assert "成本加压回测失败: boom" in capsys.readouterr().out


def test_summary_prints_cost_stress_comparison(capsys):
    stress = _metrics("20230101", "20250331", annual=0.1)
    stress.update(base_commission=0.0, commission=0.0005, base_slippage=0.001, slippage=0.002)
    report = {
        "metric_name": "sharpe", "main_eval_backtest": _metrics("20230101", "20250331"),
        "cost_stress_backtest": stress,
    }
    print_optimizer_ai_summary([report], 1, ["x"], train_range=TRAIN)
    out = capsys.readouterr().out
    assert "佣金 0->0.0005 滑点 0.001->0.002" in out
    assert "相对变化 -50%" in out
