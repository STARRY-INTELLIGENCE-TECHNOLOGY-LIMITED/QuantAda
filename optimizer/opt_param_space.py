"""解析并筛选当前策略实际使用的 Optuna 参数空间。"""


def _looks_like_param_spec(value):
    """判断对象是否像单个 Optuna 参数定义。"""
    if not isinstance(value, dict):
        return False
    return bool(
        "type" in value
        or "low" in value
        or "high" in value
        or "choices" in value
        or "value" in value
    )


def _strategy_keys(strategy_name):
    """返回策略全名和类名候选，供分组配置匹配。"""
    text = str(strategy_name or "").strip()
    if not text:
        return set()
    normalized = text[:-3] if text.endswith(".py") else text
    return {normalized, normalized.rsplit(".", 1)[-1]}


def _declared_names(strategy_class):
    """读取策略类级 params；未声明参数时保留旧的动态参数语义。"""
    declared = getattr(strategy_class, "params", None)
    if isinstance(declared, dict) and declared:
        return set(str(name) for name in declared)
    return None


def resolve_opt_params(raw_space, strategy_name, strategy_class=None):
    """解析扁平或按策略分组的空间，并过滤当前策略未声明的参数。

    扁平风格示例：``{"p1": {...}, "p2": {...}}``。
    分组风格示例：``{"StrategyA": {"p1": {...}}, "StrategyB": {...}}``。
    分组配置也可提供 ``__default__`` 作为未分组策略的空间。
    """
    if raw_space is None:
        return {}
    if not isinstance(raw_space, dict):
        raise ValueError("opt_params must be a dictionary")
    if not raw_space:
        return {}

    values = list(raw_space.values())
    is_flat = all(_looks_like_param_spec(value) for value in values)
    if is_flat:
        selected = dict(raw_space)
    else:
        if not all(isinstance(value, dict) for value in values):
            raise ValueError(
                "opt_params must use either a flat parameter map or a strategy-keyed map"
            )
        candidates = _strategy_keys(strategy_name)
        selected_group = next(
            (raw_space[key] for key in raw_space if str(key) in candidates),
            raw_space.get("__default__"),
        )
        if selected_group is None:
            raise ValueError(
                f"opt_params has no parameter group for strategy '{strategy_name}'"
            )
        selected = dict(selected_group)
        if not all(_looks_like_param_spec(value) for value in selected.values()):
            raise ValueError(
                f"opt_params group for strategy '{strategy_name}' is not a parameter map"
            )

    declared = _declared_names(strategy_class)
    if declared is None:
        return selected
    return {name: spec for name, spec in selected.items() if str(name) in declared}
