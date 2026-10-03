from optimizer.training_matrix import expand_training_matrix
from optimizer.result_file import append_optimizer_result


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
