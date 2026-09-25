import base64
from types import SimpleNamespace

import pandas as pd

import optimizer.runtime as optimizer
import common.process_elevation as process_elevation
import common.terminal_log as terminal_log
from common.terminal_log import configure_text_stream_error_handling


def _decode_encoded_command(params):
    marker = "-EncodedCommand "
    idx = params.index(marker) + len(marker)
    encoded = params[idx:].split()[0].rstrip('"')
    return base64.b64decode(encoded).decode("utf-16le")


def _build_args(**overrides):
    base = {
        "metric": "sharpe",
        "opt_params": "{}",
        "train_roll_period": "1Y",
        "test_roll_period": "3M",
        "test_period": None,
        "start_date": "20240101",
        "end_date": "20250331",
        "data_source": "ibkr",
        "selection": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _sample_metrics(start="20250101", end="20250331"):
    return {
        "start_date": start,
        "end_date": end,
        "total_return": 0.10,
        "annual_return": 0.12,
        "sharpe_ratio": 1.45,
        "max_drawdown": 0.08,
        "calmar_ratio": 1.50,
        "total_trades": 20,
        "win_rate": 55.0,
        "monthly_win_rate": 0.58,
        "profit_factor": 1.90,
        "final_portfolio": 110000.0,
    }


def _sample_trade_attribution_report():
    return (
        "\n==================================================\n"
        "          Trade Attribution by Symbol\n"
        "==================================================\n"
        "+--------+--------+----------+-----+---------+-------------+\n"
        "| Symbol | Trades | Win Rate |  PF | Net PnL | PnL Contrib |\n"
        "+--------+--------+----------+-----+---------+-------------+\n"
        "| AAPL   |      1 |  100.00% | Inf |   10.00 |     100.00% |\n"
        "+--------+--------+----------+-----+---------+-------------+\n"
        "==================================================\n"
    )


class _DummyOptimizationJob:
    def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
        self.args = args
        self.fixed_params = fixed_params
        self.train_range = ("20240101", "20241231")
        self.test_range = ("20250101", "20250331")
        self.target_symbols = ["AAPL"]
        self.yearly_backtests = [
            _sample_metrics(start="20240101", end="20241231"),
            _sample_metrics(start="20250101", end="20251231"),
        ]

    def export_shared_context(self):
        return {
            "strategy_class": object(),
            "risk_control_classes": [],
            "data_manager": object(),
            "target_symbols": ["AAPL"],
            "raw_datas": {},
            "train_datas": {"AAPL": object()},
            "test_datas": {"AAPL": object()},
            "train_range": self.train_range,
            "test_range": self.test_range,
            "window_data_cache": {},
        }

    def _run_main_eval_backtest(self, params):
        metrics = _sample_metrics(start="20230101", end="20251231")
        metrics["trade_micro_attribution_report"] = _sample_trade_attribution_report()
        return metrics

    def _run_recent_3y_backtest(self, params):
        return self._run_main_eval_backtest(params)

    def _run_test_set_backtest(self, params, verbose=False):
        metrics = _sample_metrics(start="20250101", end="20250331")
        metrics["trade_micro_attribution_report"] = _sample_trade_attribution_report()
        return metrics

    def _run_yearly_validation_backtests(self, params):
        return self.yearly_backtests

    def run(self):
        return {
            "best_score": "1.2345",
            "best_params": {"lookback": 20},
            "trials_completed": 5,
            "log_file": "dummy.log",
            "main_eval_backtest": self._run_main_eval_backtest({"lookback": 20}),
            "recent_backtest": self._run_main_eval_backtest({"lookback": 20}),
            "test_backtest": self._run_test_set_backtest({"lookback": 20}),
            "yearly_backtests": self.yearly_backtests,
        }

    @classmethod
    def build_optuna_name_tag(cls, **kwargs):
        return "dummy_name_tag"

    def _launch_dashboard(self, log_file, port=8090, background=False):
        return None


def test_run_optimizer_mode_prints_test_backtest_section(monkeypatch, capsys):
    monkeypatch.delenv(terminal_log.OPTIMIZER_TERMINAL_LOG_ENV, raising=False)
    monkeypatch.setattr(optimizer, "OptimizationJob", _DummyOptimizationJob)
    monkeypatch.setattr(optimizer.sys, "argv", ["run.py", "--params", "{\"lookback\": 20}"])

    args = _build_args(test_roll_period="3M")
    code = optimizer.run_optimizer_mode(
        args=args,
        fixed_params={"lookback": 20},
        risk_params={},
        symbol_list=["AAPL"],
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "测试集回测结果" in out
    assert "年度固定窗口回测结果" in out
    assert "TRADE MICRO ATTRIBUTION" in out
    assert "Trade Attribution by Symbol" in out
    assert out.index("TRADE MICRO ATTRIBUTION") < out.index("=== 请将上文提供给AI辅助分析 ===")
    assert "20240101->20241231" in out
    assert "20250101 -> 20250331" in out
    assert "当前基准" in out
    assert "运行概要 (RUN SUMMARY)" in out
    assert "Metrics requested:  1" in out
    assert "Metrics completed:  1" in out
    assert "Completed trials:   5" in out
    assert "MainEval reports:   2" in out
    assert "TestSet reports:    2" in out
    assert "Yearly reports:     4" in out
    assert "Optuna log files:   1" in out


def test_run_optimizer_mode_waits_after_elevation_for_opt_schedule(monkeypatch):
    monkeypatch.setattr(optimizer, "OptimizationJob", _DummyOptimizationJob)
    monkeypatch.setattr(
        optimizer.process_elevation,
        "request_optimizer_elevation_if_needed",
        lambda *_args: False,
    )
    waited = []
    monkeypatch.setattr(
        optimizer.SchedulePlanner,
        "wait_until_schedule",
        lambda schedule, **_kwargs: waited.append(schedule),
    )

    args = _build_args(opt_schedule="2d:02:00")
    code = optimizer.run_optimizer_mode(
        args=args,
        fixed_params={"lookback": 20},
        risk_params={},
        symbol_list=["AAPL"],
    )

    assert code == 0
    assert waited == ["2d:02:00"]


def test_run_optimizer_mode_skips_test_backtest_section_without_test_config(monkeypatch, capsys):
    monkeypatch.delenv(terminal_log.OPTIMIZER_TERMINAL_LOG_ENV, raising=False)
    monkeypatch.setattr(optimizer, "OptimizationJob", _DummyOptimizationJob)
    monkeypatch.setattr(optimizer.sys, "argv", ["run.py", "--params", "{\"lookback\": 20}"])

    args = _build_args(test_roll_period=None, test_period=None)
    code = optimizer.run_optimizer_mode(
        args=args,
        fixed_params={"lookback": 20},
        risk_params={},
        symbol_list=["AAPL"],
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "测试集回测结果" not in out


def test_run_test_set_backtest_returns_structured_metrics(monkeypatch):
    class DummyBacktester:
        last_kwargs = None
        display_called = False

        def __init__(self, **kwargs):
            DummyBacktester.last_kwargs = kwargs

        def run(self):
            return None

        def display_results(self):
            DummyBacktester.display_called = True

        def get_performance_metrics(self):
            return _sample_metrics(start="20250101", end="20250331")

    monkeypatch.setattr(optimizer, "Backtester", DummyBacktester)

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.test_datas = {"AAPL": object()}
    job.strategy_class = object()
    job.test_range = ("20250101", "20250331")
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    job.args = SimpleNamespace(
        cash=100000.0,
        commission=0.0003,
        slippage=0.0,
        timeframe="Days",
        compression=1,
    )
    job.risk_control_classes = []
    job.risk_params = {}

    got = job._run_test_set_backtest(final_params={"lookback": 20}, verbose=False)

    assert got is not None
    assert got["start_date"] == "20250101"
    assert got["end_date"] == "20250331"
    assert got["annual_return"] == 0.12
    assert got["monthly_win_rate"] == 0.58
    assert DummyBacktester.last_kwargs["start_date"] == "20250101"
    assert DummyBacktester.last_kwargs["end_date"] == "20250331"
    assert not DummyBacktester.display_called


def test_evaluate_trial_params_passes_cli_slippage_to_backtester(monkeypatch):
    class DummyAnalyzer:
        def get_analysis(self):
            return {"total": {"total": 0}, "max": {"drawdown": 0.0}}

    class DummyAnalyzers:
        def getbyname(self, _name):
            return DummyAnalyzer()

    class DummyDataDatetime:
        def datetime(self, idx):
            return pd.Timestamp("2024-12-31").to_pydatetime() if idx == 0 else pd.Timestamp("2024-01-01").to_pydatetime()

    class DummyData:
        datetime = DummyDataDatetime()

        def __len__(self):
            return 2

    class DummyStrat:
        data = DummyData()
        analyzers = DummyAnalyzers()

    class DummyBacktester:
        last_kwargs = None

        def __init__(self, **kwargs):
            DummyBacktester.last_kwargs = kwargs
            self.results = []

        def run(self):
            self.results = [DummyStrat()]

        def get_custom_metric(self, metric_name):
            if metric_name == "return":
                return 0.05
            if metric_name == "sharpe":
                return 1.0
            if metric_name == "calmar":
                return 1.0
            return 0.0

    monkeypatch.setattr(optimizer, "Backtester", DummyBacktester)

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.train_datas = {"AAPL": object()}
    job.strategy_class = object()
    job.train_range = ("20240101", "20241231")
    job.args = SimpleNamespace(
        cash=100000.0,
        commission=0.0003,
        slippage=0.0001,
        timeframe="Days",
        compression=1,
        metric="return",
    )
    job.risk_control_classes = []
    job.risk_params = {}
    indicator_cache = {}
    job._indicator_cache = indicator_cache

    got = job._evaluate_trial_params(current_params={"lookback": 20})

    assert got == 0.05
    assert DummyBacktester.last_kwargs["slippage"] == 0.0001
    assert DummyBacktester.last_kwargs["indicator_cache"] is indicator_cache


def test_evaluate_trial_params_passes_monthly_win_rate_to_metric(monkeypatch):
    captured = {}

    class DummyAnalyzer:
        def __init__(self, payload):
            self.payload = payload

        def get_analysis(self):
            return self.payload

    class DummyAnalyzers:
        def getbyname(self, name):
            if name == "tradeanalyzer":
                return DummyAnalyzer(
                    {
                        "total": {"total": 30},
                        "won": {"total": 18, "pnl": {"total": 1200.0}},
                        "lost": {"pnl": {"total": -600.0}},
                    }
                )
            if name == "drawdown":
                return DummyAnalyzer({"max": {"drawdown": 10.0}})
            if name == "timereturn_monthly":
                return DummyAnalyzer(
                    {
                        "2024-01": 0.05,
                        "2024-02": -0.02,
                        "2024-03": 0.0,
                        "2024-04": 0.03,
                    }
                )
            return DummyAnalyzer({})

    class DummyDataDatetime:
        def datetime(self, idx):
            return pd.Timestamp("2024-12-31").to_pydatetime() if idx == 0 else pd.Timestamp("2024-01-01").to_pydatetime()

    class DummyData:
        datetime = DummyDataDatetime()

        def __len__(self):
            return 2

    class DummyStrat:
        data = DummyData()
        analyzers = DummyAnalyzers()

    class DummyBacktester:
        def __init__(self, **kwargs):
            self.results = []

        def run(self):
            self.results = [DummyStrat()]

        def get_custom_metric(self, metric_name):
            if metric_name == "return":
                return 0.05
            if metric_name == "sharpe":
                return 1.0
            if metric_name == "calmar":
                return 1.0
            return 0.0

    def fake_metric(stats, strat=None, args=None):
        captured.update(stats)
        return 123.0

    monkeypatch.setattr(optimizer, "Backtester", DummyBacktester)
    monkeypatch.setattr(optimizer, "get_metric_function", lambda metric: fake_metric)

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.train_datas = {"AAPL": object()}
    job.strategy_class = object()
    job.train_range = ("20240101", "20241231")
    job.args = SimpleNamespace(
        cash=100000.0,
        commission=0.0003,
        slippage=0.0001,
        timeframe="Days",
        compression=1,
        metric="mix_score_us_turbo",
    )
    job.risk_control_classes = []
    job.risk_params = {}

    got = job._evaluate_trial_params(current_params={"lookback": 20})

    assert got == 123.0
    assert captured["monthly_win_rate"] == 2.0 / 3.0


def test_slice_datas_keeps_fixed_warmup_rows_before_logical_start():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    idx = pd.date_range("2023-01-01", "2025-06-30", freq="D")
    job.raw_datas = {
        "AAPL": pd.DataFrame(
            {
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
            },
            index=idx,
        )
    }

    got = job.slice_datas("20250101", "20250630")

    assert job.warmup_days == 400
    assert got["AAPL"].index.min() == pd.Timestamp("2023-11-28")
    assert got["AAPL"].index.max() == pd.Timestamp("2025-06-30")


def test_slice_datas_normalizes_tz_aware_option_index():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    idx = pd.date_range("2023-01-01", "2025-06-30", freq="D", tz="UTC")
    job.raw_datas = {
        "US.QQQ250117P00400000": pd.DataFrame(
            {
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
            },
            index=idx,
        )
    }

    got = job.slice_datas("20250101", "20250630")

    frame = got["US.QQQ250117P00400000"]
    assert frame.index.tz is None
    assert frame.index.min() == pd.Timestamp("2023-11-28")
    assert frame.index.max() == pd.Timestamp("2025-06-30")


def test_split_data_dynamic_refit_has_no_test_range():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    idx = pd.date_range("2019-01-01", "2025-01-31", freq="D")
    job.raw_datas = {
        "AAPL": pd.DataFrame(
            {
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
            },
            index=idx,
        )
    }
    job.args = SimpleNamespace(
        train_period=None,
        test_period=None,
        train_roll_period="5y",
        test_roll_period=None,
        train_ratio=None,
        end_date="20250131",
        start_date="20181127",
    )

    train_d, test_d, train_range, test_range = job._split_data()

    assert train_range == ("20200131", "20250131")
    assert test_d == {}
    assert test_range == (None, None)
    assert train_d["AAPL"].index.min() == pd.Timestamp("2019-01-01")
    assert train_d["AAPL"].index.max() == pd.Timestamp("2025-01-31")


def test_infer_main_eval_window_covers_train_test_when_longer_than_recent_3y():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(end_date="20260616", start_date="20191112")
    job.train_range = ("20201216", "20251215")
    job.test_range = ("20251216", "20260616")

    got = job._infer_main_eval_window()

    assert got == ("20201216", "20260616")


def test_infer_main_eval_window_uses_recent_3y_when_train_test_is_shorter():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(end_date="20260616", start_date="20250101")
    job.train_range = ("20250101", "20251215")
    job.test_range = ("20251216", "20260616")

    got = job._infer_main_eval_window()

    assert got == ("20230616", "20260616")


def test_infer_main_eval_window_anchors_recent_3y_to_test_end():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(end_date="20260616", start_date="20250101")
    job.train_range = ("20250101", "20251215")
    job.test_range = ("20251216", "20260331")

    got = job._infer_main_eval_window()

    assert got == ("20230331", "20260331")


def test_fetch_all_data_dynamic_roll_uses_fixed_warmup_start():
    calls = []

    class DummyDataManager:
        def get_data(self, symbol, **kwargs):
            calls.append({"symbol": symbol, **kwargs})
            return pd.DataFrame(
                {
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [100.0],
                },
                index=[pd.Timestamp("2024-01-01")],
            )

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    job.target_symbols = ["AAPL"]
    job.data_manager = DummyDataManager()
    job.args = SimpleNamespace(
        start_date="20220101",
        end_date="20250131",
        train_roll_period="5y",
        test_roll_period="6m",
        train_period=None,
        test_period=None,
        data_source="dummy",
        timeframe="Days",
        compression=1,
        refresh=False,
    )

    got = job._fetch_all_data()

    expected_start = (
        pd.Timestamp("2025-01-31")
        - pd.DateOffset(months=6)
        - pd.DateOffset(years=5)
        - pd.DateOffset(days=optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS)
    ).strftime("%Y%m%d")
    assert list(got) == ["AAPL"]
    assert calls[0]["start_date"] == expected_start
    assert calls[0]["end_date"] == "20250131"


def test_fetch_all_data_extends_to_recent_3y_warmup_start_when_earlier():
    calls = []

    class DummyDataManager:
        def get_data(self, symbol, **kwargs):
            calls.append({"symbol": symbol, **kwargs})
            return pd.DataFrame(
                {
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [100.0],
                },
                index=[pd.Timestamp("2024-01-01")],
            )

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    job.target_symbols = ["AAPL"]
    job.data_manager = DummyDataManager()
    job.args = SimpleNamespace(
        start_date="20250101",
        end_date="20260616",
        train_roll_period=None,
        test_roll_period=None,
        train_period=None,
        test_period=None,
        train_ratio=None,
        data_source="dummy",
        timeframe="Days",
        compression=1,
        refresh=False,
    )

    got = job._fetch_all_data()

    expected_start = (
        pd.Timestamp("2026-06-16")
        - pd.DateOffset(years=3)
        - pd.DateOffset(days=optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS)
    ).strftime("%Y%m%d")
    assert list(got) == ["AAPL"]
    assert calls[0]["start_date"] == expected_start
    assert job._raw_data_fetch_range == (expected_start, "20260616")


def test_fetch_all_data_full_window_uses_fixed_warmup_start():
    calls = []

    class DummyDataManager:
        def get_data(self, symbol, **kwargs):
            calls.append({"symbol": symbol, **kwargs})
            return pd.DataFrame(
                {
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [100.0],
                },
                index=[pd.Timestamp("2024-01-01")],
            )

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    job.target_symbols = ["AAPL"]
    job.data_manager = DummyDataManager()
    job.args = SimpleNamespace(
        start_date="20220101",
        end_date="20250131",
        train_roll_period=None,
        test_roll_period=None,
        train_period=None,
        test_period=None,
        train_ratio=None,
        data_source="dummy",
        timeframe="Days",
        compression=1,
        refresh=False,
    )

    got = job._fetch_all_data()

    expected_start = (
        pd.Timestamp("2022-01-01")
        - pd.DateOffset(days=optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS)
    ).strftime("%Y%m%d")
    assert list(got) == ["AAPL"]
    assert calls[0]["start_date"] == expected_start
    assert calls[0]["end_date"] == "20250131"


def test_yearly_validation_reuses_raw_datas_without_provider_fetch(monkeypatch):
    calls = []

    class DummyDataManager:
        def get_data(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("provider fetch should not be used for yearly validation")

    class DummyBacktester:
        seen_windows = []

        def __init__(self, **kwargs):
            DummyBacktester.seen_windows.append((kwargs["start_date"], kwargs["end_date"], kwargs["datas"]))

        def run(self):
            return None

        def get_performance_metrics(self):
            return _sample_metrics(
                start=DummyBacktester.seen_windows[-1][0],
                end=DummyBacktester.seen_windows[-1][1],
            )

    monkeypatch.setattr(optimizer, "Backtester", DummyBacktester)

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    idx = pd.date_range("2020-01-01", "2022-12-31", freq="D")
    job.raw_datas = {
        "AAPL": pd.DataFrame(
            {
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
            },
            index=idx,
        )
    }
    job.target_symbols = ["AAPL"]
    job.data_manager = DummyDataManager()
    job.train_range = ("20200101", "20221231")
    job.test_range = (None, None)
    job.strategy_class = object()
    job.risk_control_classes = []
    job.risk_params = {}
    job._indicator_cache = {}
    job.args = SimpleNamespace(
        cash=100000.0,
        commission=0.0003,
        slippage=0.0,
        timeframe="Days",
        compression=1,
        data_source="dummy",
        refresh=False,
    )

    got = job._run_yearly_validation_backtests(final_params={"lookback": 20})

    assert calls == []
    assert len(got) == 3
    assert [item["start_date"] for item in got] == ["20200101", "20210101", "20220101"]
    assert all("AAPL" in datas for _, _, datas in DummyBacktester.seen_windows)


def test_main_eval_backtest_reuses_preloaded_raw_datas_without_provider_fetch(monkeypatch):
    calls = []

    class DummyDataManager:
        def get_data(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("provider fetch should not be used for MainEval backtest")

    class DummyBacktester:
        last_kwargs = None

        def __init__(self, **kwargs):
            DummyBacktester.last_kwargs = kwargs

        def run(self):
            return None

        def display_results(self):
            return None

        def get_performance_metrics(self):
            return _sample_metrics(start="20201216", end="20260616")

    monkeypatch.setattr(optimizer, "Backtester", DummyBacktester)

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    idx = pd.date_range("2019-11-12", "2026-06-15", freq="D")
    job.raw_datas = {
        "AAPL": pd.DataFrame(
            {
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
            },
            index=idx,
        )
    }
    job.target_symbols = ["AAPL"]
    job.data_manager = DummyDataManager()
    job._window_data_cache = {}
    job._raw_data_fetch_range = ("20191112", "20260616")
    job.strategy_class = object()
    job.risk_control_classes = []
    job.risk_params = {}
    job._indicator_cache = {}
    job.args = SimpleNamespace(
        cash=100000.0,
        commission=0.0003,
        slippage=0.0,
        timeframe="Days",
        compression=1,
        data_source="dummy",
        refresh=False,
        end_date="20260616",
        start_date="20191112",
        train_roll_period="5y",
        test_roll_period="6m",
        metric="mix_score_us_turbo",
    )

    job.train_range = ("20201216", "20251215")
    job.test_range = ("20251216", "20260616")

    got = job._run_main_eval_backtest(final_params={"lookback": 20})

    assert calls == []
    assert got is not None
    expected_warmup_start = (
        pd.Timestamp("2020-12-16") - pd.DateOffset(days=optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS)
    )
    assert DummyBacktester.last_kwargs["datas"]["AAPL"].index.min() == expected_warmup_start
    assert DummyBacktester.last_kwargs["datas"]["AAPL"].index.max() == pd.Timestamp("2026-06-15")
    assert DummyBacktester.last_kwargs["start_date"] == "20201216"
    assert DummyBacktester.last_kwargs["end_date"] == "20260616"


def test_fetch_datas_for_window_reuses_tz_aware_preloaded_data():
    calls = []

    class DummyDataManager:
        def get_data(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("provider fetch should not run when tz-aware raw_datas already cover the window")

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    idx = pd.date_range("2019-11-12", "2026-06-16", freq="D", tz="UTC")
    job.raw_datas = {
        "US.SPY": pd.DataFrame(
            {
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
            },
            index=idx,
        )
    }
    job.target_symbols = ["US.SPY"]
    job.data_manager = DummyDataManager()
    job._window_data_cache = {}
    job._raw_data_fetch_range = ("20191112", "20260616")
    job.args = SimpleNamespace(
        data_source="theta",
        timeframe="Days",
        compression=1,
        refresh=False,
    )

    got = job._fetch_datas_for_window("20201216", "20260616")

    assert calls == []
    assert "US.SPY" in got
    assert got["US.SPY"].index.tz is None
    expected_warmup_start = (
        pd.Timestamp("2020-12-16") - pd.DateOffset(days=optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS)
    )
    assert got["US.SPY"].index.min() == expected_warmup_start
    assert got["US.SPY"].index.max() == pd.Timestamp("2026-06-16")


def test_fetch_datas_for_window_logs_progress_not_per_symbol_short_tail(capsys):
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = 0
    idx = pd.bdate_range("2026-09-01", "2026-09-18")
    frame = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        index=idx,
    )
    job.raw_datas = {
        "US.DIA261009P00501000": frame,
        "US.SPY": frame,
    }
    job.target_symbols = ["US.SPY", "US.DIA261009P00501000"]
    job.data_manager = SimpleNamespace(get_data=lambda *args, **kwargs: None)
    job._window_data_cache = {}
    job._raw_data_fetch_range = ("20260901", "20260920")
    job.args = SimpleNamespace(
        data_source="theta+futu",
        timeframe="Days",
        compression=1,
        refresh=False,
    )

    got = job._fetch_datas_for_window("20260901", "20260920")
    out = capsys.readouterr().out
    assert "US.SPY" in got
    assert "US.DIA261009P00501000" in got
    assert "Reusing preloaded data for US.DIA261009P00501000" not in out
    assert "Align window" in out
    assert "reuse=2" in out
    assert "short_tail=2" in out


def test_request_elevation_skips_for_single_worker(monkeypatch):
    args = SimpleNamespace(n_jobs=1)
    monkeypatch.delenv("QUANTADA_DISABLE_AUTO_ELEVATE", raising=False)
    monkeypatch.setattr(process_elevation, "is_process_elevated", lambda: False)

    called = {"banner": 0}

    def _mark_banner(_):
        called["banner"] += 1

    monkeypatch.setattr(process_elevation, "print_elevation_banner", _mark_banner)

    got = process_elevation.request_optimizer_elevation_if_needed(
        args,
        optimizer.OptimizationJob._resolve_worker_count,
    )

    assert got is False
    assert called["banner"] == 0


def test_request_elevation_windows_branch(monkeypatch):
    args = SimpleNamespace(n_jobs=4)
    monkeypatch.delenv("QUANTADA_DISABLE_AUTO_ELEVATE", raising=False)
    monkeypatch.setattr(process_elevation.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(process_elevation, "is_process_elevated", lambda: False)
    monkeypatch.setattr(process_elevation, "print_elevation_banner", lambda *_: None)
    monkeypatch.setattr(process_elevation, "relaunch_windows_as_admin", lambda: True)

    got = process_elevation.request_optimizer_elevation_if_needed(
        args,
        optimizer.OptimizationJob._resolve_worker_count,
    )

    assert got is True


def test_windows_elevation_command_keeps_console_open(monkeypatch):
    monkeypatch.delenv(terminal_log.OPTIMIZER_TERMINAL_LOG_ENV, raising=False)
    monkeypatch.setattr(process_elevation.sys, "argv", ["run.py", "--opt_params", "{'x': 1}"])
    monkeypatch.setattr(process_elevation.sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(process_elevation.os.path, "abspath", lambda path: rf"E:\Lin\Github\QuantAda\{path}")
    monkeypatch.setattr(process_elevation.os, "getcwd", lambda: r"E:\Lin\Github\QuantAda")
    monkeypatch.setenv("POWERSHELL", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

    target_exe, params = process_elevation.build_windows_elevated_console_command()

    assert target_exe == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert params.startswith("-NoExit -NoProfile")
    decoded = _decode_encoded_command(params)
    assert "Set-Location -LiteralPath 'E:\\Lin\\Github\\QuantAda'" in decoded
    assert "chcp 65001 >$null" in decoded
    assert "$env:PYTHONIOENCODING='utf-8'" in decoded
    assert "$env:PYTHONUTF8='1'" in decoded
    assert "$env:QUANTADA_DISABLE_AUTO_ELEVATE='1'" in decoded
    assert r"& 'C:\Python\python.exe' '-X' 'utf8'" in decoded
    assert r"'E:\Lin\Github\QuantAda\run.py'" in decoded
    assert "Elevated run finished" in decoded


def test_windows_elevation_command_forwards_process_env(monkeypatch):
    monkeypatch.setattr(process_elevation.sys, "argv", ["run.py", "--opt_params", "{'x': 1}"])
    monkeypatch.setattr(process_elevation.sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(process_elevation.os.path, "abspath", lambda path: rf"E:\Lin\Github\QuantAda\{path}")
    monkeypatch.setattr(process_elevation.os, "getcwd", lambda: r"E:\Lin\Github\QuantAda")
    monkeypatch.setenv("POWERSHELL", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    monkeypatch.setenv("THETADATA_API_KEY", "td1_test_token")
    monkeypatch.setenv("FUTU_HOST", "120.79.20.238")
    monkeypatch.setenv("FUTU_PORT", "11111")
    monkeypatch.setenv("PYTHONPATH", r"E:\Lin\Github\Quant-MAGI-CASPER-3")
    monkeypatch.setenv("QUANTADA_DISABLE_AUTO_ELEVATE", "0")

    _, params = process_elevation.build_windows_elevated_console_command()
    decoded = _decode_encoded_command(params)

    assert "$env:THETADATA_API_KEY='td1_test_token'" in decoded
    assert "$env:FUTU_HOST='120.79.20.238'" in decoded
    assert "$env:FUTU_PORT='11111'" in decoded
    assert r"$env:PYTHONPATH='E:\Lin\Github\Quant-MAGI-CASPER-3'" in decoded
    assert "$env:QUANTADA_DISABLE_AUTO_ELEVATE='0'" not in decoded
    disable_idx = decoded.rindex("$env:QUANTADA_DISABLE_AUTO_ELEVATE='1'")
    python_idx = decoded.index(r"& 'C:\Python\python.exe'")
    theta_idx = decoded.index("$env:THETADATA_API_KEY='td1_test_token'")
    assert theta_idx < disable_idx < python_idx


def test_optimizer_terminal_log_path_uses_optuna_style_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _build_args(
        metric="mix_score_us_turbo,mix_score_origin",
        train_roll_period="5y",
        test_roll_period="6m",
        end_date="20260606",
        data_source="tiingo",
        selection=None,
    )

    got = optimizer._build_optimizer_terminal_log_path_for_args(
        args,
        symbol_list=[],
        run_dt=pd.Timestamp("2026-06-07 12:34:56").to_pydatetime(),
        run_pid=12345,
    )

    assert str(tmp_path / ".data" / "optimizer") in got
    assert (
        "optimizer_terminal_5Y_6M_mix_score_us_turbo_mix_score_origin_US_"
        "TR20201206-20251205_TE20251206-20260606_RUN20260607-123456_12345.log"
    ) in got


def test_optimizer_terminal_log_tee_writes_console_and_file(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(terminal_log.OPTIMIZER_TERMINAL_LOG_ENV, raising=False)
    monkeypatch.setattr(terminal_log, "_TERMINAL_LOG_TEE", None)
    log_file = tmp_path / "optimizer_terminal.log"

    tee = terminal_log.install_optimizer_terminal_log(str(log_file))
    print("hello optimizer tee")
    tee.close()

    out = capsys.readouterr().out
    assert "Terminal output tee" in out
    assert "hello optimizer tee" in out
    assert "hello optimizer tee" in log_file.read_text(encoding="utf-8")
    assert terminal_log.get_optimizer_terminal_log_path() == str(log_file)


def test_elevation_command_carries_terminal_log_env(monkeypatch):
    log_file = r"E:\Lin\Github\QuantAda\.data\optimizer\optimizer terminal.log"
    monkeypatch.setattr(process_elevation.sys, "argv", ["run.py", "--opt_params", "{'x': 1}"])
    monkeypatch.setattr(process_elevation.sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(process_elevation.os.path, "abspath", lambda path: rf"E:\Lin\Github\QuantAda\{path}")
    monkeypatch.setattr(process_elevation.os, "getcwd", lambda: r"E:\Lin\Github\QuantAda")
    monkeypatch.setenv("POWERSHELL", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    monkeypatch.setenv(terminal_log.OPTIMIZER_TERMINAL_LOG_ENV, log_file)

    _, params = process_elevation.build_windows_elevated_console_command()

    assert "--terminal_log_file" not in params
    decoded = _decode_encoded_command(params)
    assert log_file in decoded
    assert terminal_log.OPTIMIZER_TERMINAL_LOG_ENV in decoded
    assert f"$env:{terminal_log.OPTIMIZER_TERMINAL_LOG_ENV}='{log_file}'" in decoded
    assert "chcp 65001 >$null" in decoded
    assert "$env:QUANTADA_DISABLE_AUTO_ELEVATE='1'" in decoded
    assert decoded.index("$env:PYTHONIOENCODING='utf-8'") < decoded.index(r"& 'C:\Python\python.exe'")


def test_configure_text_stream_error_handling_downgrades_unicode_encode_error():
    class DummyStream:
        def __init__(self):
            self.encoding = "gbk"
            self.errors = "strict"
            self.buffer = []

        def write(self, text):
            encoded = str(text).encode(self.encoding, errors=self.errors)
            self.buffer.append(encoded.decode(self.encoding))
            return len(text)

        def reconfigure(self, **kwargs):
            if "errors" in kwargs:
                self.errors = kwargs["errors"]

    stream = DummyStream()
    configure_text_stream_error_handling((stream,))

    stream.write("hello 💡 world")

    assert stream.errors == "backslashreplace"
    assert stream.buffer == [r"hello \U0001f4a1 world"]


def test_request_elevation_linux_branch(monkeypatch):
    args = SimpleNamespace(n_jobs=4)
    monkeypatch.delenv("QUANTADA_DISABLE_AUTO_ELEVATE", raising=False)
    monkeypatch.setattr(process_elevation.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(process_elevation, "is_process_elevated", lambda: False)
    monkeypatch.setattr(process_elevation, "print_elevation_banner", lambda *_: None)
    monkeypatch.setattr(process_elevation, "relaunch_unix_with_sudo", lambda: True)

    got = process_elevation.request_optimizer_elevation_if_needed(
        args,
        optimizer.OptimizationJob._resolve_worker_count,
    )

    assert got is True


def test_request_elevation_macos_uses_unix_branch(monkeypatch):
    args = SimpleNamespace(n_jobs=4)
    monkeypatch.delenv("QUANTADA_DISABLE_AUTO_ELEVATE", raising=False)
    monkeypatch.setattr(process_elevation.sys, "platform", "darwin", raising=False)
    monkeypatch.setattr(process_elevation, "is_process_elevated", lambda: False)
    monkeypatch.setattr(process_elevation, "print_elevation_banner", lambda *_: None)
    monkeypatch.setattr(process_elevation, "relaunch_unix_with_sudo", lambda: True)

    got = process_elevation.request_optimizer_elevation_if_needed(
        args,
        optimizer.OptimizationJob._resolve_worker_count,
    )

    assert got is True


def test_request_elevation_respects_disable_flag_on_unix(monkeypatch):
    args = SimpleNamespace(n_jobs=4)
    monkeypatch.setenv("QUANTADA_DISABLE_AUTO_ELEVATE", "1")
    monkeypatch.setattr(process_elevation.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(process_elevation, "is_process_elevated", lambda: False)

    called = {"unix": 0}
    monkeypatch.setattr(process_elevation, "relaunch_unix_with_sudo", lambda: called.__setitem__("unix", 1))

    got = process_elevation.request_optimizer_elevation_if_needed(
        args,
        optimizer.OptimizationJob._resolve_worker_count,
    )

    assert got is False
    assert called["unix"] == 0


def test_relaunch_unix_skips_noninteractive_terminal(monkeypatch, capsys):
    class DummyStdin:
        def isatty(self):
            return False

    monkeypatch.setattr(process_elevation.sys, "stdin", DummyStdin())

    got = process_elevation.relaunch_unix_with_sudo()

    assert got is False
    assert "non-interactive terminal" in capsys.readouterr().out


def test_relaunch_unix_uses_utf8_env_and_disables_recursive_elevation(monkeypatch):
    class DummyStdin:
        def isatty(self):
            return True

    captured = {}

    def fake_execvp(exe, cmd):
        captured["exe"] = exe
        captured["cmd"] = cmd
        raise RuntimeError("stop before exec")

    monkeypatch.setattr(process_elevation.sys, "stdin", DummyStdin())
    monkeypatch.setattr(process_elevation.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(process_elevation.sys, "argv", ["run.py", "--opt_params", "{'x': 1}"])
    monkeypatch.setattr(process_elevation.os, "execvp", fake_execvp)

    got = process_elevation.relaunch_unix_with_sudo()

    assert got is False
    assert captured["exe"] == "sudo"
    assert captured["cmd"][:3] == ["sudo", "-E", "env"]
    assert "PYTHONIOENCODING=utf-8" in captured["cmd"]
    assert "PYTHONUTF8=1" in captured["cmd"]
    assert "QUANTADA_DISABLE_AUTO_ELEVATE=1" in captured["cmd"]
    assert "/usr/bin/python3" in captured["cmd"]
    assert "-X" in captured["cmd"]
    assert "utf8" in captured["cmd"]


def test_optimizer_run_passes_gc_after_trial_to_single_process_optimize(monkeypatch):
    captured = {}

    class DummyTrialState:
        COMPLETE = "complete"

    class DummyTrial:
        state = "complete"

    class DummyStudy:
        user_attrs = {"_optimizer_worker_config_version": 1}
        trials = [DummyTrial()]
        best_params = {"lookback": 20}
        best_value = 1.23

        def set_user_attr(self, key, value):
            return None

        def optimize(self, objective, n_trials=None, n_jobs=1, gc_after_trial=False, **kwargs):
            captured["n_trials"] = n_trials
            captured["n_jobs"] = n_jobs
            captured["gc_after_trial"] = gc_after_trial

    monkeypatch.setattr(optimizer.optuna, "create_study", lambda **kwargs: DummyStudy())
    monkeypatch.setattr(optimizer.optuna.trial, "TrialState", DummyTrialState)
    monkeypatch.setattr(optimizer, "TPESampler", lambda **kwargs: SimpleNamespace(kwargs=kwargs))

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(
        n_jobs=1,
        n_trials=3,
        auto_launch_dashboard=False,
        shared_journal_log_file=None,
        study_name="dummy-study",
        strategy="dummy.strategy",
        metric="sharpe",
        cash=100000.0,
        commission=0.0003,
        slippage=0.0,
        timeframe="Days",
        compression=1,
    )
    job.fixed_params = {}
    job.opt_params_def = {}
    job.risk_params = {}
    job._indicator_cache = {}
    job.test_datas = {}
    job.test_range = (None, None)
    job.objective = lambda trial: 1.0
    job._run_main_eval_backtest = lambda params: _sample_metrics(start="20240101", end="20241231")
    job._run_test_set_backtest = lambda params, verbose=False: None
    job._run_yearly_validation_backtests = lambda params: []

    result = job.run()

    assert result["best_params"] == {"lookback": 20}
    assert captured["gc_after_trial"] is True
    assert captured["n_trials"] == 2
    assert captured["n_jobs"] == 1


def test_optimizer_run_uses_default_tpe_candidates_for_short_runs(monkeypatch):
    sampler_kwargs = {}

    class DummyTrialState:
        COMPLETE = "complete"

    class DummyTrial:
        state = "complete"

    class DummyStudy:
        user_attrs = {"_optimizer_worker_config_version": 1}
        trials = [DummyTrial()]
        best_params = {"lookback": 20}
        best_value = 1.23

        def set_user_attr(self, key, value):
            return None

        def optimize(self, *args, **kwargs):
            return None

    def fake_sampler(**kwargs):
        sampler_kwargs.update(kwargs)
        return SimpleNamespace(kwargs=kwargs)

    monkeypatch.setattr(optimizer.optuna, "create_study", lambda **kwargs: DummyStudy())
    monkeypatch.setattr(optimizer.optuna.trial, "TrialState", DummyTrialState)
    monkeypatch.setattr(optimizer, "TPESampler", fake_sampler)

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(
        n_jobs=1,
        n_trials=50,
        auto_launch_dashboard=False,
        shared_journal_log_file=None,
        study_name="dummy-study",
        strategy="dummy.strategy",
        metric="sharpe",
        cash=100000.0,
        commission=0.0003,
        slippage=0.0,
        timeframe="Days",
        compression=1,
    )
    job.fixed_params = {}
    job.opt_params_def = {}
    job.risk_params = {}
    job._indicator_cache = {}
    job.test_datas = {}
    job.test_range = (None, None)
    job.objective = lambda trial: 1.0
    job._run_main_eval_backtest = lambda params: _sample_metrics(start="20240101", end="20241231")
    job._run_test_set_backtest = lambda params, verbose=False: None
    job._run_yearly_validation_backtests = lambda params: []

    job.run()

    assert sampler_kwargs["n_ei_candidates"] == optimizer.OptimizationJob.TPE_DEFAULT_N_EI_CANDIDATES


def test_tpe_candidates_drop_only_for_spawn_payload_copy_long_runs():
    assert (
        optimizer.OptimizationJob._resolve_spawn_tpe_n_ei_candidates(
            optimizer.OptimizationJob.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD,
            shared_memory_enabled=True,
        )
        == optimizer.OptimizationJob.TPE_DEFAULT_N_EI_CANDIDATES
    )
    assert (
        optimizer.OptimizationJob._resolve_spawn_tpe_n_ei_candidates(
            optimizer.OptimizationJob.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD - 1,
            shared_memory_enabled=False,
        )
        == optimizer.OptimizationJob.TPE_DEFAULT_N_EI_CANDIDATES
    )
    assert (
        optimizer.OptimizationJob._resolve_spawn_tpe_n_ei_candidates(
            optimizer.OptimizationJob.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD,
            shared_memory_enabled=False,
        )
        == optimizer.OptimizationJob.TPE_DEFAULT_N_EI_CANDIDATES
    )
    assert (
        optimizer.OptimizationJob._resolve_spawn_tpe_n_ei_candidates(
            optimizer.OptimizationJob.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD * 2,
            shared_memory_enabled=False,
        )
        == 12
    )
    assert (
        optimizer.OptimizationJob._resolve_spawn_tpe_n_ei_candidates(
            optimizer.OptimizationJob.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD * 4,
            shared_memory_enabled=False,
        )
        == optimizer.OptimizationJob.TPE_PAYLOAD_COPY_MIN_N_EI_CANDIDATES
    )


def test_optimize_worker_entry_returns_early_on_memory_pressure(monkeypatch, capsys):
    class DummyStudy:
        user_attrs = {"_optimizer_worker_config_version": 1}
        def optimize(self, *args, **kwargs):
            raise MemoryError("simulated sampler pressure")

    class DummyJob:
        released = False

        def _release_memory_pressure(self):
            DummyJob.released = True

        @staticmethod
        def objective(trial):
            return 1.0

    monkeypatch.setattr(optimizer, "HAS_JOURNAL", True)
    monkeypatch.setattr(optimizer, "JournalStorage", lambda backend: object())
    monkeypatch.setattr(optimizer, "JournalFileBackendCls", lambda log_file, **kwargs: object())
    monkeypatch.setattr(optimizer.optuna, "create_study", lambda **kwargs: DummyStudy())
    monkeypatch.setattr(optimizer, "TPESampler", lambda **kwargs: SimpleNamespace(kwargs=kwargs))
    monkeypatch.setattr(optimizer.OptimizationJob, "from_worker_payload", lambda payload: DummyJob())
    monkeypatch.setattr(optimizer.OptimizationJob, "_cleanup_shared_segments", lambda handles, unlink=False: None)

    payload = {
        "args": _build_args(strategy="dummy.strategy", risk=None),
        "train_datas": {},
        "runtime_config": {},
    }

    result = optimizer._optimize_worker_entry(
        payload,
        study_name="dummy-study",
        log_file="dummy.log",
        n_trials=10,
        worker_idx=2,
        sampler_seed=123,
    )

    out = capsys.readouterr().out
    assert result["stopped_early"] is True
    assert result["reason"] == "memory_pressure"
    assert DummyJob.released is True
    assert "memory pressure" in out


def test_multiprocess_optimization_absorbs_remote_memory_error(monkeypatch, capsys):
    class DummyFuture:
        cancelled = False

        def result(self):
            raise MemoryError("remote worker oom")

        def cancel(self):
            self.cancelled = True

    class DummyExecutor:
        _processes = {}
        shutdown_called = False

        def __init__(self, max_workers=None, mp_context=None):
            self.max_workers = max_workers
            self.mp_context = mp_context

        def submit(self, *args, **kwargs):
            return future

        def shutdown(self, wait=True, cancel_futures=False):
            DummyExecutor.shutdown_called = True

    future = DummyFuture()
    monkeypatch.setattr(optimizer, "ProcessPoolExecutor", DummyExecutor)
    monkeypatch.setattr(optimizer, "as_completed", lambda futures: futures)
    monkeypatch.setattr(optimizer.mp, "get_context", lambda method: object())

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="dummy-study")
    job._build_worker_payload = lambda: {"train_datas": {}}
    job._build_spawn_shared_payload = lambda payload: (payload, [])

    job._run_multiprocess_optimization(n_jobs=1, n_trials=1, log_file="dummy.log")

    out = capsys.readouterr().out
    assert "memory pressure" in out
    assert "Completed trials" in out
    assert future.cancelled is True
    assert DummyExecutor.shutdown_called is True


def test_multiprocess_optimization_absorbs_broken_worker_pool(monkeypatch, capsys):
    class DummyFuture:
        cancelled = False

        def result(self):
            raise optimizer.BrokenProcessPool("worker died")

        def cancel(self):
            self.cancelled = True

    class DummyExecutor:
        _processes = {}
        shutdown_called = False

        def __init__(self, max_workers=None, mp_context=None):
            self.max_workers = max_workers
            self.mp_context = mp_context

        def submit(self, *args, **kwargs):
            return future

        def shutdown(self, wait=True, cancel_futures=False):
            DummyExecutor.shutdown_called = True

    future = DummyFuture()
    monkeypatch.setattr(optimizer, "ProcessPoolExecutor", DummyExecutor)
    monkeypatch.setattr(optimizer, "as_completed", lambda futures: futures)
    monkeypatch.setattr(optimizer.mp, "get_context", lambda method: object())

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="dummy-study")
    job._build_worker_payload = lambda: {"train_datas": {}}
    job._build_spawn_shared_payload = lambda payload: (payload, [])

    job._run_multiprocess_optimization(n_jobs=1, n_trials=1, log_file="dummy.log")

    out = capsys.readouterr().out
    assert "Worker process exited unexpectedly" in out
    assert "Completed trials" in out
    assert future.cancelled is True
    assert DummyExecutor.shutdown_called is True


def test_multiprocess_optimization_keeps_default_tpe_candidates_when_spawn_shared(monkeypatch):
    captured = {}

    class DummyFuture:
        def result(self):
            return {"worker_idx": 1, "stopped_early": False}

    class DummyExecutor:
        _processes = {}

        def __init__(self, max_workers=None, mp_context=None):
            return None

        def submit(self, *args, **kwargs):
            captured["tpe_n_ei_candidates"] = args[-1]
            return DummyFuture()

        def shutdown(self, wait=True, cancel_futures=False):
            return None

    monkeypatch.setattr(optimizer, "ProcessPoolExecutor", DummyExecutor)
    monkeypatch.setattr(optimizer, "as_completed", lambda futures: futures)
    monkeypatch.setattr(optimizer.mp, "get_context", lambda method: object())

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="dummy-study")
    job._build_worker_payload = lambda: {"train_datas": {"AAPL": object()}}
    job._build_spawn_shared_payload = lambda payload: ({"train_datas": None, "train_datas_shared": {}}, [object()])

    job._run_multiprocess_optimization(
        n_jobs=1,
        n_trials=optimizer.OptimizationJob.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD,
        log_file="dummy.log",
    )

    assert captured["tpe_n_ei_candidates"] == optimizer.OptimizationJob.TPE_DEFAULT_N_EI_CANDIDATES


def test_multiprocess_optimization_drops_tpe_candidates_only_when_spawn_payload_copies(monkeypatch):
    captured = {}

    class DummyFuture:
        def result(self):
            return {"worker_idx": 1, "stopped_early": False}

    class DummyExecutor:
        _processes = {}

        def __init__(self, max_workers=None, mp_context=None):
            return None

        def submit(self, *args, **kwargs):
            captured["tpe_n_ei_candidates"] = args[-1]
            return DummyFuture()

        def shutdown(self, wait=True, cancel_futures=False):
            return None

    monkeypatch.setattr(optimizer, "ProcessPoolExecutor", DummyExecutor)
    monkeypatch.setattr(optimizer, "as_completed", lambda futures: futures)
    monkeypatch.setattr(optimizer.mp, "get_context", lambda method: object())

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="dummy-study")
    job._build_worker_payload = lambda: {"train_datas": {"AAPL": object()}}
    job._build_spawn_shared_payload = lambda payload: (payload, [])

    job._run_multiprocess_optimization(
        n_jobs=1,
        n_trials=optimizer.OptimizationJob.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD * 2,
        log_file="dummy.log",
    )

    assert captured["tpe_n_ei_candidates"] == 12


def test_finite_grid_search_space_is_materialized_without_duplicate_values():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.opt_params_def = {
        "min_dte": {"type": "int", "low": 25, "high": 35, "step": 5},
        "profit_take_fraction": {"type": "float", "low": 0.4, "high": 0.6, "step": 0.1},
        "mode": {"type": "categorical", "choices": ["a", "b"]},
    }

    grid = job._build_grid_search_space()

    assert grid == {
        "min_dte": [25, 30, 35],
        "profit_take_fraction": [0.4, 0.5, 0.6],
        "mode": ["a", "b"],
    }


def test_explicit_study_name_is_not_replaced_by_timestamp():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.args = SimpleNamespace(study_name="csp-resume-20260922")

    job._auto_refine_study_name()

    assert job.args.study_name == "csp-resume-20260922"


def test_remaining_trial_budget_does_not_count_failed_trials():
    class Trial:
        def __init__(self, state):
            self.state = state

    class Study:
        trials = [
            Trial(optimizer.optuna.trial.TrialState.COMPLETE),
            Trial(optimizer.optuna.trial.TrialState.FAIL),
            Trial(optimizer.optuna.trial.TrialState.RUNNING),
        ]

    assert optimizer.OptimizationJob._remaining_trial_budget(Study(), 10) == 9
    assert optimizer.OptimizationJob._remaining_trial_budget(Study(), 2) == 1


def test_spawn_payload_supports_string_metadata_without_strategy_specific_gate():
    class StockStrategy:
        pass

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.strategy_class = StockStrategy
    frame = pd.DataFrame(
        {
            "close": [1.0, 1.1],
            "option_type": ["PUT", "PUT"],
            "expiry": ["2026-10-16", "2026-10-16"],
        },
        index=pd.Index(["2026-09-01", "2026-09-02"], name="datetime"),
    )
    frame.attrs["source"] = "stock-cache"
    payload, handles = job._build_spawn_shared_payload(
        {"train_datas": {"US.SPY_OPTION": frame}}
    )
    try:
        restored, attached = optimizer.OptimizationJob._restore_train_datas_from_shared(
            payload["train_datas_shared"]
        )
        try:
            records_meta = payload["train_datas_shared"]["symbols"]["US.SPY_OPTION"]["records"]
            records_arr, probe = optimizer.OptimizationJob._attach_shared_array(records_meta)
            assert restored["US.SPY_OPTION"]["option_type"].tolist() == ["PUT", "PUT"]
            assert restored["US.SPY_OPTION"]["expiry"].tolist() == [
                "2026-10-16", "2026-10-16",
            ]
            assert restored["US.SPY_OPTION"].attrs["source"] == "stock-cache"
            restored_values = restored["US.SPY_OPTION"]["close"].to_numpy(copy=False)
            assert not restored_values.flags.owndata
            base = restored_values
            mmap_base = False
            while base is not None:
                if type(base).__module__ == "mmap" and type(base).__name__ == "mmap":
                    mmap_base = True
                    break
                base = getattr(base, "base", None)
            assert mmap_base
            probe.close()
        finally:
            optimizer.OptimizationJob._cleanup_shared_segments(attached, unlink=False)
    finally:
        optimizer.OptimizationJob._cleanup_shared_segments(handles, unlink=True)


def test_spawn_payload_falls_back_for_unsupported_object_values():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    frame = pd.DataFrame({"close": [1.0], "metadata": [{"nested": True}]})
    worker_payload = {"train_datas": {"AAA": frame}}

    payload, handles = job._build_spawn_shared_payload(worker_payload)

    assert payload is worker_payload
    assert handles == []


def test_spawn_payload_falls_back_for_extension_dtypes():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    frame = pd.DataFrame({"close": [1.0], "sector": pd.Categorical(["ETF"])})
    worker_payload = {"train_datas": {"AAA": frame}}

    payload, handles = job._build_spawn_shared_payload(worker_payload)

    assert payload is worker_payload
    assert handles == []


def test_spawn_payload_falls_back_for_missing_string_values():
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    frame = pd.DataFrame({"close": [1.0], "sector": pd.Series([None], dtype=object)})
    worker_payload = {"train_datas": {"AAA": frame}}

    payload, handles = job._build_spawn_shared_payload(worker_payload)

    assert payload is worker_payload
    assert handles == []


def test_optimizer_infers_omitted_window_after_schedule_wait(monkeypatch):
    events = []

    def wait(schedule, **_kwargs):
        events.append(("wait", schedule))
        return None

    def infer(args):
        events.append(("infer", args.start_date, args.end_date))
        args.end_date = "20260922"
        args.start_date = "20230922"

    monkeypatch.setattr(optimizer, "OptimizationJob", _DummyOptimizationJob)
    monkeypatch.setattr(
        optimizer.process_elevation,
        "request_optimizer_elevation_if_needed",
        lambda *_args: False,
    )
    monkeypatch.setattr(optimizer.SchedulePlanner, "wait_until_schedule", wait)
    monkeypatch.setattr(optimizer, "infer_omitted_backtest_window", infer)

    args = _build_args(opt_schedule="02:00", start_date=None, end_date=None)
    code = optimizer.run_optimizer_mode(
        args=args,
        fixed_params={"lookback": 20},
        risk_params={},
        symbol_list=["AAPL"],
    )

    assert code == 0
    assert events[0] == ("wait", "02:00")
    assert events[1][0] == "infer"
    assert events[1][1:] == (None, None)
    assert args.start_date == "20230922"
    assert args.end_date == "20260922"
