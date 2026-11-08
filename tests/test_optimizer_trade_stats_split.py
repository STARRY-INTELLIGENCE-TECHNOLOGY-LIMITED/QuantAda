"""优化器交易统计口径与比例切分窗口。"""

from types import SimpleNamespace

import pandas as pd

import optimizer.runtime as optimizer


def _evaluate_with_trade_analysis(monkeypatch, trade_analysis):
    captured = {}

    class Analyzer:
        def __init__(self, payload):
            self.payload = payload

        def get_analysis(self):
            return self.payload

    class Analyzers:
        def getbyname(self, name):
            if name == "tradeanalyzer":
                return Analyzer(trade_analysis)
            if name == "drawdown":
                return Analyzer({"max": {"drawdown": 10.0}})
            return Analyzer({})

    class Clock:
        def datetime(self, idx):
            day = "2024-12-31" if idx == 0 else "2024-01-01"
            return pd.Timestamp(day).to_pydatetime()

    class Data:
        datetime = Clock()

        def __len__(self):
            return 2

    class Strat:
        data = Data()
        analyzers = Analyzers()

    class DummyBacktester:
        def __init__(self, **kwargs):
            self.results = []

        def run(self):
            self.results = [Strat()]

        def get_custom_metric(self, metric_name):
            return 0.05 if metric_name == "return" else 1.0

    def fake_metric(stats, strat=None, args=None):
        captured.update(stats)
        return 1.0

    monkeypatch.setattr(optimizer, "Backtester", DummyBacktester)
    monkeypatch.setattr(optimizer, "get_metric_function", lambda metric: fake_metric)
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.train_datas = {"AAPL": object()}
    job.strategy_class = object()
    job.train_range = ("20240101", "20241231")
    job.args = SimpleNamespace(cash=100000.0, commission=0.0, slippage=0.0, timeframe="Days",
                               compression=1, metric="mix_score_turbo")
    job.risk_control_classes = []
    job.risk_params = {}
    job._evaluate_trial_params(current_params={})
    return captured


def test_profit_factor_without_losses_is_a_bounded_ratio_not_dollar_profit(monkeypatch):
    stats = _evaluate_with_trade_analysis(monkeypatch, {
        "total": {"total": 3, "closed": 3},
        "won": {"total": 3, "pnl": {"total": 50000.0}},
    })
    assert stats["profit_factor"] == optimizer.OptimizationJob.NO_LOSS_PROFIT_FACTOR


def test_profit_factor_without_closed_trades_is_neutral(monkeypatch):
    stats = _evaluate_with_trade_analysis(monkeypatch, {"total": {"total": 0}})
    assert stats["profit_factor"] == 0.0
    assert stats["win_rate"] == 0.0


def test_win_rate_ignores_open_trades(monkeypatch):
    stats = _evaluate_with_trade_analysis(monkeypatch, {
        "total": {"total": 5, "open": 1, "closed": 4},
        "won": {"total": 3, "pnl": {"total": 300.0}},
        "lost": {"total": 1, "pnl": {"total": -100.0}},
    })
    assert stats["win_rate"] == 0.75
    assert stats["profit_factor"] == 3.0


def _ratio_job(start_date):
    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    # 数据从 MainEval 扩展抓取起点开始，远早于用户 start_date。
    idx = pd.date_range("2020-01-01", "2025-12-31", freq="D")
    job.raw_datas = {"AAPL": pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                           "volume": 1.0}, index=idx)}
    job.args = SimpleNamespace(train_period=None, test_period=None, train_roll_period=None,
                               train_ratio=0.5, start_date=start_date, end_date="20251231")
    return job


def test_ratio_split_applies_to_user_window_and_keeps_warmup_outside_training():
    train_d, test_d, train_range, test_range = _ratio_job("20240101")._split_data()

    window_days = len(pd.date_range("2024-01-01", "2025-12-31", freq="D"))
    split = pd.date_range("2024-01-01", "2025-12-31", freq="D")[window_days // 2]
    assert train_range == ("20240101", (split - pd.Timedelta(days=1)).strftime("%Y%m%d"))
    assert test_range == (split.strftime("%Y%m%d"), "20251231")
    # 预热 K 线仍提供给指标，但不改变训练期起点。
    warmup_start = pd.Timestamp("2024-01-01") - pd.DateOffset(days=optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS)
    assert train_d["AAPL"].index.min() == warmup_start


def test_ratio_split_without_start_date_uses_cli_three_year_default():
    _, _, train_range, test_range = _ratio_job(None)._split_data()
    assert train_range[0] == "20221231"
    assert test_range[1] == "20251231"


def test_ratio_mode_fetch_includes_warmup_before_user_start(monkeypatch):
    requested = []

    class DataManager:
        data_path = None

        def get_data(self, symbol, start_date=None, end_date=None, **kwargs):
            requested.append(start_date)
            return pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2024-01-02"]))

    job = optimizer.OptimizationJob.__new__(optimizer.OptimizationJob)
    job.warmup_days = optimizer.OptimizationJob.DEFAULT_WARMUP_DAYS
    job.target_symbols = ["AAPL"]
    job.strategy_class = None
    job.data_manager = DataManager()
    job.args = SimpleNamespace(train_period=None, test_period=None, train_roll_period=None,
                               train_ratio=0.7, start_date="20150101", end_date="20251231",
                               data_source=None, refresh=False, timeframe="Days", compression=1)
    monkeypatch.setattr(job, "_prefetch_option_history", lambda *a, **k: None, raising=False)
    job._fetch_all_data()
    assert job._raw_data_fetch_range[0] == job._warmup_start_date("20150101")
    assert requested == [job._warmup_start_date("20150101")]
    assert job.args.start_date == "20150101"
