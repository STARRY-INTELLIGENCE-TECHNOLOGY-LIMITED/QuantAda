import pandas as pd

from stock_selectors.runtime import run_selectors


def test_run_selectors_returns_ordered_deduplicated_union_for_lists_and_frames():
    class First:
        def __init__(self, data_manager):
            pass

        def run_selection(self):
            return ["AAA", "BBB", "AAA", ""]

    class Second:
        def __init__(self, data_manager):
            pass

        def run_selection(self):
            return pd.DataFrame(index=["BBB", "CCC", "CCC"])

    classes = {"first": First, "second": Second}
    calls = []

    def resolver(name, paths):
        calls.append((name, tuple(paths)))
        return classes[name]

    symbols = run_selectors("first+second+first", object(), ["custom"], resolver)

    assert symbols == ["AAA", "BBB", "CCC"]
    assert calls == [("first", ("custom",)), ("second", ("custom",))]


def test_run_selectors_rejects_empty_selector_parts():
    try:
        run_selectors("first+", object(), class_resolver=lambda *_: None)
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("empty selector component should fail")
