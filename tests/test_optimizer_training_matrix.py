from optimizer.training_matrix import expand_training_matrix
from optimizer.result_file import append_optimizer_result
from optimizer.opt_param_space import resolve_opt_params


def test_training_matrix_is_strategy_major_and_keeps_selection_plus_groups():
    assert expand_training_matrix(
        "strategy_a,strategy_b", "selector_a,selector_b+selector_c",
    ) == [
        ("strategy_a", "selector_a"),
        ("strategy_a", "selector_b+selector_c"),
        ("strategy_b", "selector_a"),
        ("strategy_b", "selector_b+selector_c"),
    ]


def test_training_matrix_accepts_single_legacy_values_and_empty_selection():
    assert expand_training_matrix("strategy_a", None) == [("strategy_a", None)]
    assert expand_training_matrix("strategy_a", "") == [("strategy_a", None)]


def test_optimizer_result_file_uses_utf8_and_lf_newlines(tmp_path):
    path = tmp_path / "result.txt"
    append_optimizer_result(path, "第一行\r\n第二行\r")
    assert path.read_bytes() == "第一行\n第二行\n\n".encode("utf-8")


def test_flat_opt_params_are_filtered_by_declared_strategy_params():
    class Strategy:
        params = {"p1": 1, "p3": 3}

    space = {
        "p1": {"type": "int", "low": 1, "high": 3},
        "p2": {"type": "int", "low": 1, "high": 3},
        "p3": {"type": "float", "low": 0.1, "high": 0.9},
    }
    assert set(resolve_opt_params(space, "Strategy", Strategy)) == {"p1", "p3"}


def test_strategy_keyed_opt_params_support_full_name_and_class_name():
    class StrategyB:
        params = {"p3": 3, "p5": 5}

    space = {
        "StrategyA": {"p1": {"type": "int", "low": 1, "high": 2}},
        "StrategyB": {
            "p3": {"type": "int", "low": 1, "high": 2},
            "p4": {"type": "int", "low": 1, "high": 2},
            "p5": {"type": "int", "low": 1, "high": 2},
        },
    }
    assert set(resolve_opt_params(space, "strategies.example.StrategyB", StrategyB)) == {"p3", "p5"}
