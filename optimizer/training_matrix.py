"""解析训练命令中的策略与选股器组合。"""


def _split_comma_values(value, label, allow_empty=False):
    """按逗号拆分训练组合，保留每项内部的加号表达式。"""
    text = str(value or "").strip()
    if not text and allow_empty:
        return [None]
    values = [item.strip() for item in text.split(",")]
    if not values or any(not item for item in values):
        raise ValueError(f"{label} 不允许包含空的逗号项")
    return list(dict.fromkeys(values))


def expand_training_matrix(strategy, selection=None):
    """按策略优先顺序返回笛卡尔组合；选股器项中的加号保持原样。"""
    if strategy is None or not str(strategy).strip():
        return [(strategy, selection)]
    strategies = _split_comma_values(strategy, "strategy")
    selections = _split_comma_values(selection, "selection", allow_empty=True)
    return [
        (strategy_name, selection_name)
        for strategy_name in strategies
        for selection_name in selections
    ]
