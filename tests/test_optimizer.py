from types import SimpleNamespace

import pandas as pd

import common.optimizer as optimizer


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
        "profit_factor": 1.90,
        "final_portfolio": 110000.0,
    }


class _DummyOptimizationJob:
    def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
        self.args = args
        self.fixed_params = fixed_params
        self.train_range = ("20240101", "20241231")
        self.test_range = ("20250101", "20250331")
        self.target_symbols = ["AAPL"]

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

    def _run_recent_3y_backtest(self, params):
        return _sample_metrics(start="20230101", end="20251231")

    def _run_test_set_backtest(self, params, verbose=False):
        return _sample_metrics(start="20250101", end="20250331")

    def run(self):
        return {
            "best_score": "1.2345",
            "best_params": {"lookback": 20},
            "trials_completed": 5,
            "log_file": "dummy.log",
            "recent_backtest": _sample_metrics(start="20230101", end="20251231"),
            "test_backtest": _sample_metrics(start="20250101", end="20250331"),
        }

    @classmethod
    def build_optuna_name_tag(cls, **kwargs):
        return "dummy_name_tag"

    def _launch_dashboard(self, log_file, port=8090, background=False):
        return None


def test_run_optimizer_mode_prints_test_backtest_section(monkeypatch, capsys):
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
    assert "20250101 -> 20250331" in out
    assert "当前基准" in out


def test_run_optimizer_mode_skips_test_backtest_section_without_test_config(monkeypatch, capsys):
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

    got = job._evaluate_trial_params(current_params={"lookback": 20})

    assert got == 0.05
    assert DummyBacktester.last_kwargs["slippage"] == 0.0001


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


def test_request_elevation_skips_for_single_worker(monkeypatch):
    args = SimpleNamespace(n_jobs=1)
    monkeypatch.delenv("QUANTADA_DISABLE_AUTO_ELEVATE", raising=False)
    monkeypatch.setattr(optimizer, "_is_process_elevated", lambda: False)

    called = {"banner": 0}

    def _mark_banner(_):
        called["banner"] += 1

    monkeypatch.setattr(optimizer, "_print_elevation_banner", _mark_banner)

    got = optimizer._request_elevation_if_needed(args)

    assert got is False
    assert called["banner"] == 0


def test_request_elevation_windows_branch(monkeypatch):
    args = SimpleNamespace(n_jobs=4)
    monkeypatch.delenv("QUANTADA_DISABLE_AUTO_ELEVATE", raising=False)
    monkeypatch.setattr(optimizer.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(optimizer, "_is_process_elevated", lambda: False)
    monkeypatch.setattr(optimizer, "_print_elevation_banner", lambda _: None)
    monkeypatch.setattr(optimizer, "_relaunch_windows_as_admin", lambda: True)

    got = optimizer._request_elevation_if_needed(args)

    assert got is True


def test_request_elevation_linux_branch(monkeypatch):
    args = SimpleNamespace(n_jobs=4)
    monkeypatch.delenv("QUANTADA_DISABLE_AUTO_ELEVATE", raising=False)
    monkeypatch.setattr(optimizer.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(optimizer, "_is_process_elevated", lambda: False)
    monkeypatch.setattr(optimizer, "_print_elevation_banner", lambda _: None)
    monkeypatch.setattr(optimizer, "_relaunch_unix_with_sudo", lambda: True)

    got = optimizer._request_elevation_if_needed(args)

    assert got is True
