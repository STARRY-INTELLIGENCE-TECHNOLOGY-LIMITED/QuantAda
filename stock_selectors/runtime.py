"""执行选股器组合，并按首次出现顺序合并标的。"""

import pandas as pd

from common.loader import get_class_from_name


def run_selectors(selection, data_manager, search_paths=('stock_selectors',), class_resolver=None):
    """依次执行加号分隔的选股器，返回去重并集；任一失败则不返回部分结果。"""
    resolver = class_resolver or get_class_from_name
    names = [name.strip() for name in str(selection or '').split('+')]
    if not all(names):
        raise ValueError("--selection requires non-empty selector names separated by '+'")

    symbols = []
    seen = set()
    for name in dict.fromkeys(names):
        selector_class = resolver(name, search_paths)
        selector = selector_class(data_manager=data_manager)
        result = selector.run_selection()
        if isinstance(result, pd.DataFrame):
            result = result.index
        elif not isinstance(result, (list, tuple, set, pd.Index)):
            raise ValueError(f"Selector '{name}' returned unsupported type: {type(result).__name__}")
        if isinstance(result, set):
            result = sorted(result, key=str)
        for symbol in result:
            if symbol is None:
                continue
            symbol = str(symbol).strip()
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols
