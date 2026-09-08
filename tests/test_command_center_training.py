"""训练工作台的源码分析、范围推荐和结果索引测试。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from command_center.training import (
    TrainingSelectionStore,
    TrainingResult,
    extract_strategy_params,
    recommend_ranges,
    recommendation_notes,
    result_to_params,
    scan_training_results,
    suggestions_to_dict,
)


def test_extract_strategy_params_reads_static_defaults_only(tmp_path: Path) -> None:
    source = tmp_path / "demo_strategy.py"
    source.write_text(
        """
class DemoStrategy:
    params = {
        "lookback": 20,
        "threshold": 0.2,
        "enabled": True,
        "mode": "fast",
        "dynamic": SOME_RUNTIME_VALUE,
    }
""",
        encoding="utf-8",
    )

    assert extract_strategy_params(source) == {
        "lookback": 20,
        "threshold": 0.2,
        "enabled": True,
        "mode": "fast",
    }


def test_extract_strategy_params_accepts_static_dict_call(tmp_path: Path) -> None:
    source = tmp_path / "dict_strategy.py"
    source.write_text(
        "class DemoStrategy:\n    params = dict(lookback=15, threshold=0.1)\n",
        encoding="utf-8",
    )
    assert extract_strategy_params(source) == {"lookback": 15, "threshold": 0.1}


def test_extract_strategy_params_ignores_method_local_params(tmp_path: Path) -> None:
    source = tmp_path / "local_params_strategy.py"
    source.write_text(
        "class DemoStrategy:\n"
        "    params = {'lookback': 15}\n"
        "    def next(self):\n"
        "        params = {'not_strategy_param': 99}\n",
        encoding="utf-8",
    )
    assert extract_strategy_params(source) == {"lookback": 15}


def test_recommend_ranges_accepts_annotated_params_assignment(tmp_path: Path) -> None:
    source = tmp_path / "annotated_strategy.py"
    source.write_text(
        "class DemoStrategy:\n"
        "    params: dict = {'lookback': 10}\n",
        encoding="utf-8",
    )

    suggestions = recommend_ranges(source)

    assert len(suggestions) == 1
    assert suggestions[0].name == "lookback"


def test_recommend_ranges_preserves_dict_call_source_line(tmp_path: Path) -> None:
    source = tmp_path / "dict_recommend_strategy.py"
    source.write_text(
        "class DemoStrategy:\n"
        "    params = dict(lookback=15, threshold=0.1)  # 静态参数\n",
        encoding="utf-8",
    )

    suggestions = recommend_ranges(source)

    assert {item.name for item in suggestions} == {"lookback", "threshold"}
    assert all(item.source_line == 2 for item in suggestions)


def test_recommend_ranges_are_type_aware_and_include_source_metadata(tmp_path: Path) -> None:
    source = tmp_path / "demo_strategy.py"
    source.write_text(
        """class DemoStrategy:
    params = {
        "lookback": 20,  # 观察窗口
        "threshold": 0.2,
        "enabled": True,
        "mode": "fast",
    }
""",
        encoding="utf-8",
    )

    suggestions = recommend_ranges(source)
    by_name = {item.name: item for item in suggestions}

    assert by_name["lookback"].current == 20
    assert by_name["lookback"].value_type == "int"
    assert by_name["lookback"].recommendation == {
        "type": "int",
        "low": 10,
        "high": 30,
        "step": 2,
    }
    assert by_name["lookback"].comment == "观察窗口"
    assert by_name["threshold"].recommendation == {
        "type": "float",
        "low": 0.1,
        "high": 0.3,
        "step": 0.01,
    }
    assert by_name["enabled"].recommendation == {
        "type": "categorical",
        "choices": [False, True],
    }
    assert by_name["mode"].recommendation == {
        "type": "categorical",
        "choices": ["fast"],
    }
    assert suggestions_to_dict(suggestions)["lookback"] == {
        "type": "int",
        "low": 10,
        "high": 30,
        "step": 2,
    }
    notes = recommendation_notes(suggestions)
    assert any("mode" in note and "固定类别" in note for note in notes)


def test_result_index_scans_logs_and_recovers_params(tmp_path: Path) -> None:
    optimizer = tmp_path / ".data" / "optimizer"
    optimizer.mkdir(parents=True)
    first = optimizer / "optimizer_terminal_old.log"
    second = optimizer / "optimizer_terminal_new.log"
    first.write_text(
        "Best Training Score (sharpe): 1.25\n"
        + "report line\n" * 60
        + " Params: {'lookback': 10}\n",
        encoding="utf-8",
    )
    second.write_text(
        "noise\nBest Training Score (total_return): 2.5\n"
        "Params: {'lookback': 20, 'threshold': 0.3}\n"
        "SUMMARY OF BEST CONFIGURATION\n"
        "MainEval: 20230101 -> 20251231\n"
        "Annual: 20.00%\n"
        "Calmar: 2.5\n"
        "TestSet: 20260101 -> 20260630\n"
        "Annual: 10.00%\n",
        encoding="utf-8",
    )
    now = time.time()
    os.utime(first, (now - 20, now - 20))
    os.utime(second, (now, now))

    results = scan_training_results(tmp_path)

    assert [item.metric for item in results] == ["total_return", "sharpe"]
    assert results[0].score == "2.5"
    assert results[0].params == {"lookback": 20, "threshold": 0.3}
    assert results[0].path.endswith("optimizer_terminal_new.log")
    assert results[0].result_id.startswith("optimizer_terminal_new.log:2:total_return")
    assert result_to_params(results[0]) == {"lookback": 20, "threshold": 0.3}
    assert results[0].main_eval == {
        "window": "20230101 -> 20251231",
        "annual": "20.00%",
        "calmar": "2.5",
    }
    assert results[0].test_set == {
        "window": "20260101 -> 20260630",
        "annual": "10.00%",
    }


def test_result_index_recovers_wrapped_params_before_next_metric(tmp_path: Path) -> None:
    optimizer = tmp_path / ".data" / "optimizer"
    optimizer.mkdir(parents=True)
    log = optimizer / "optimizer_terminal_wrapped.log"
    log.write_text(
        "Best Training Score (sharpe): 1.25\n"
        "SUMMARY OF BEST CONFIGURATION\n"
        " Params:   {'lookback': 20, 'threshold':\n"
        " 0.3, 'mode': 'fast'}\n"
        "Best Training Score (calmar): 2.50\n"
        " Params:   {'lookback': 30}\n",
        encoding="utf-8",
    )

    results = scan_training_results(tmp_path)

    by_metric = {item.metric: item for item in results}
    assert by_metric["calmar"].params == {"lookback": 30}
    assert by_metric["sharpe"].params == {"lookback": 20, "threshold": 0.3, "mode": "fast"}


def test_result_index_ignores_malformed_params_without_aborting_scan(tmp_path: Path) -> None:
    optimizer = tmp_path / ".data" / "optimizer"
    optimizer.mkdir(parents=True)
    log = optimizer / "optimizer_terminal_broken.log"
    log.write_text(
        "Best Training Score (sharpe): 1.0\n Params: {'broken': }\n"
        "Best Training Score (return): 2.0\n Params: {'lookback': 8}\n",
        encoding="utf-8",
    )

    results = scan_training_results(tmp_path)

    assert len(results) == 2
    by_metric = {item.metric: item for item in results}
    assert by_metric["sharpe"].params == {}
    assert by_metric["return"].params == {"lookback": 8}


def test_result_index_reassembles_wrapped_params_dict(tmp_path: Path) -> None:
    optimizer = tmp_path / ".data" / "optimizer"
    optimizer.mkdir(parents=True)
    log = optimizer / "optimizer_terminal_wrapped.log"
    log.write_text(
        "Best Training Score (sharpe): 1.5\n"
        " Params: {'lookback': 20,\n"
        "  'nested': {'window': 5}}\n",
        encoding="utf-8",
    )

    results = scan_training_results(tmp_path)

    assert len(results) == 1
    assert results[0].params == {"lookback": 20, "nested": {"window": 5}}


def test_selection_store_persists_unique_selected_result_ids(tmp_path: Path) -> None:
    store = TrainingSelectionStore(tmp_path)
    assert store.load() == set()

    store.save(["new:2", "old:1", "new:2"])

    assert store.load() == {"new:2", "old:1"}
    assert store.path.is_file()


def test_selection_store_keeps_selected_result_snapshot(tmp_path: Path) -> None:
    store = TrainingSelectionStore(tmp_path)
    result = TrainingResult(
        "run.log:10:sharpe",
        "run.log",
        "sharpe",
        "1.2",
        {"lookback": 20},
        "2026-01-01T00:00:00",
    )
    store.save_result(result)
    assert store.load() == {result.result_id}
    assert store.load_results()[result.result_id]["params"] == {"lookback": 20}
    assert store.load_results()[result.result_id]["main_eval"] == {}
    store.unselect(result.result_id)
    assert store.load() == set()
    assert result.result_id in store.load_results()
