"""训练工作台的源码分析、范围推荐和结果索引测试。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from command_center.training import (
    TrainingSelectionStore,
    extract_strategy_params,
    recommend_ranges,
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


def test_result_index_scans_logs_and_recovers_params(tmp_path: Path) -> None:
    optimizer = tmp_path / ".data" / "optimizer"
    optimizer.mkdir(parents=True)
    first = optimizer / "optimizer_terminal_old.log"
    second = optimizer / "optimizer_terminal_new.log"
    first.write_text(
        "Best Training Score (sharpe): 1.25\nParams: {'lookback': 10}\n",
        encoding="utf-8",
    )
    second.write_text(
        "noise\nBest Training Score (total_return): 2.5\n"
        "Params: {'lookback': 20, 'threshold': 0.3}\n",
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


def test_selection_store_persists_unique_selected_result_ids(tmp_path: Path) -> None:
    store = TrainingSelectionStore(tmp_path)
    assert store.load() == set()

    store.save(["new:2", "old:1", "new:2"])

    assert store.load() == {"new:2", "old:1"}
    assert store.path.is_file()
