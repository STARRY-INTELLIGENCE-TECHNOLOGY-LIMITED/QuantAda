"""配置快照须在真实 worker 加载策略前恢复，并与单进程评分保持一致。"""

from concurrent.futures import ProcessPoolExecutor
import copy
import multiprocessing
from types import SimpleNamespace

import optuna
from optuna.storages import JournalStorage
from optuna.storages.journal import JournalFileBackend, JournalFileOpenLock
import pandas as pd
import pytest

import config
from optimizer.runtime import OptimizationJob, _optimize_worker_entry


def _job(module):
    job = OptimizationJob.__new__(OptimizationJob)
    job.args = SimpleNamespace(
        strategy=f"{module}.Strategy", risk=f"{module}.Risk", metric=f"{module}.score",
        cash=1000.0, commission=0.0, slippage=0.0, timeframe="Days", compression=1,
        config="{'LOT_SIZE': 7, 'ANNUAL_FACTOR': 365}",
    )
    job.fixed_params = job.opt_params_def = job.risk_params = {}
    job.train_range = ("20240101", "20240105")
    job.warmup_days = 0
    job.train_datas = {"AAA": pd.DataFrame(
        {key: [10.0, 11.0, 12.0, 13.0, 14.0] for key in ("open", "high", "low", "close", "volume")},
        index=pd.date_range("2024-01-01", periods=5),
    )}
    return job


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("start_method", ["spawn", "fork"])
def test_worker_config_matches_parent_before_plugin_import(monkeypatch, tmp_path, shared, start_method):
    if start_method not in multiprocessing.get_all_start_methods():
        pytest.skip(f"当前平台不支持 {start_method}")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    monkeypatch.delenv("QUANTADA_OPTIMIZER_TERMINAL_LOG", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    module = f"worker_config_plugin_{start_method}_{int(shared)}"
    (tmp_path / (module + ".py")).write_text('''import config
from config import LOT_SIZE, ANNUAL_FACTOR, FUTU_HOST
from strategies.base_strategy import BaseStrategy
from risk_controls.base_risk_control import BaseRiskControl

assert LOT_SIZE == 7
assert ANNUAL_FACTOR == 365
assert FUTU_HOST == "192.0.2.77"
assert config.DATA_PROVIDER_COMPOSITIONS["probe"]["sources"] == ["csv"]
assert config.BROKER_ENVIRONMENTS == {"fixture": {"schedule": "1d"}}
assert config.PRINT_PLAN is False and config.LOG is False
assert not hasattr(config, "UNKNOWN_WORKER_KEY")

class Strategy(BaseStrategy):
    def init(self):
        self.sent = False

    def next(self):
        if not self.sent:
            self.broker.order_target_percent(self.broker.datas[0], target=0.5)
            self.sent = True

class Risk(BaseRiskControl):
    def check(self, data):
        assert config.ANNUAL_FACTOR == 365
        return None

def score(stats, **kwargs):
    assert config.LOT_SIZE == LOT_SIZE
    return stats["total_return_pct"]
''', encoding="utf-8")
    for key, value in {
        "LOT_SIZE": 7, "ANNUAL_FACTOR": 365, "FUTU_HOST": "192.0.2.77", "LOG": False,
        "PRINT_PLAN": False, "ALARMS_ENABLED": False,
        "DATA_PROVIDER_COMPOSITIONS": {"probe": {"sources": ["csv"]}},
        "BROKER_ENVIRONMENTS": {"fixture": {"schedule": "1d"}},
    }.items():
        monkeypatch.setattr(config, key, value)
    job = _job(module)
    payload = job._build_worker_payload()
    # 运行期最终值优先于旧参数字符串，不能在 worker 中重新解释未生效的输入。
    payload["args"].config = "{'LOT_SIZE': 99, 'UNKNOWN_WORKER_KEY': 1, 'PRINT_PLAN': True}"
    parent_job = OptimizationJob.from_worker_payload(payload)
    expected = parent_job._evaluate_trial_params({})
    assert expected > 0.0
    parent_handles = []
    if shared:
        payload, parent_handles = job._build_spawn_shared_payload(payload)
        assert parent_handles
    journal = str(tmp_path / "worker.log")
    try:
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context(start_method)) as pool:
            result = pool.submit(_optimize_worker_entry, payload, "config-test", journal, 1, 1, 0).result(timeout=30)
        assert result["stopped_early"] is False
        study = optuna.load_study(
            study_name="config-test", storage=JournalStorage(JournalFileBackend(journal, lock_obj=JournalFileOpenLock(journal))),
        )
        assert study.best_value == pytest.approx(expected, abs=1e-12)
        assert config.LOT_SIZE == 7
        assert config.BROKER_ENVIRONMENTS == {"fixture": {"schedule": "1d"}}
    finally:
        OptimizationJob._cleanup_shared_segments(parent_handles, unlink=True)


def test_worker_payload_is_an_independent_complete_config_snapshot(monkeypatch):
    monkeypatch.setattr(config, "DATA_PROVIDER_COMPOSITIONS", {"probe": {"sources": ["csv"]}})
    monkeypatch.setattr(config, "FRAMEWORK_EXTENSION_KEY", [1, 2], raising=False)
    expected = copy.deepcopy({key: value for key, value in vars(config).items() if key.isupper()})
    payload = _job("unused")._build_worker_payload()
    assert payload["runtime_config"] == expected
    config.DATA_PROVIDER_COMPOSITIONS["probe"]["sources"].append("new")
    config.FRAMEWORK_EXTENSION_KEY.append(3)
    assert payload["runtime_config"] == expected
    assert "log_enabled" not in payload


def test_worker_refuses_missing_config_snapshot_before_opening_journal(tmp_path):
    payload = _job("unused")._build_worker_payload()
    payload.pop("runtime_config")
    journal = tmp_path / "absent.log"
    with pytest.raises(ValueError, match="snapshot is missing"):
        _optimize_worker_entry(payload, "unused", str(journal), 1, 1, 0)
    assert not journal.exists()
