"""优化器搜索空间分析、全局覆盖和自适应预算工具。"""

from collections import defaultdict
import hashlib
import json
import math

import numpy as np


# 自动预算只在未显式指定 --n_trials 时生效，避免改变用户明确给出的硬上限。
EXHAUSTIVE_GRID_MAX_COMBINATIONS = 4096
AUTO_MAX_TRIALS = 100000
AUTO_MAX_MULTIPLIER = 4
AUTO_BATCH_MIN = 16
AUTO_BATCH_MAX = 256
GLOBAL_PILOT_MIN = 16
GLOBAL_PILOT_MAX = 64


def grid_signature(grid_search_space):
    """返回有限网格的稳定身份；网格内容变化时不得复用原 Study。"""
    payload = json.dumps(
        {str(name): list(values) for name, values in (grid_search_space or {}).items()},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _state_name(state):
    return str(getattr(state, "name", state)).upper()


def _finished_trial(trial):
    return _state_name(getattr(trial, "state", None)) in {"COMPLETE", "PRUNED"}


def _normalise(value):
    if isinstance(value, float):
        return round(value, 12)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _normalise(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_normalise(item) for item in value)
    return value


def parameter_key(params):
    """把参数字典转换为稳定、可哈希的键。"""
    return tuple(sorted((str(name), _normalise(value)) for name, value in (params or {}).items()))


def finite_grid_size(grid_search_space):
    """返回有限网格大小；空空间和非法值返回 None。"""
    if not grid_search_space:
        return None
    size = 1
    for values in grid_search_space.values():
        if not values:
            return 0
        size *= len(values)
    return int(size)


def _parameter_bucket(value, config):
    """把参数压到四个粗粒度区间，供区块诊断使用。"""
    if config.get("type") == "categorical":
        choices = list(config.get("choices", []))
        try:
            return min(3, choices.index(value))
        except ValueError:
            return 0

    try:
        low = float(config.get("low", value))
        high = float(config.get("high", low))
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if high < low:
        low, high = high, low
    if abs(high - low) <= 1e-12:
        return 0
    ratio = min(1.0, max(0.0, (number - low) / (high - low)))
    return min(3, int(ratio * 4.0))


def _block_dimensions(opt_params_def):
    candidates = []
    for name, config in (opt_params_def or {}).items():
        p_type = config.get("type")
        if p_type == "categorical":
            cardinality = len(config.get("choices", []))
        elif p_type in {"int", "float"}:
            try:
                cardinality = abs(float(config.get("high", 0)) - float(config.get("low", 0)))
            except (TypeError, ValueError):
                cardinality = 1
        else:
            cardinality = 1
        if cardinality > 1:
            candidates.append((float(cardinality), str(name)))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in candidates[:2]]


def block_key(params, opt_params_def):
    """返回稳定的粗粒度搜索区块键。"""
    dimensions = _block_dimensions(opt_params_def)
    return tuple(
        (name, _parameter_bucket((params or {}).get(name), opt_params_def[name]))
        for name in dimensions
    )


def summarise_trials(trials, opt_params_def, finite_size=None):
    """汇总已完成试验、唯一组合、搜索区块和当前最优值。"""
    finished = [trial for trial in trials or [] if _finished_trial(trial)]
    keys = [parameter_key(getattr(trial, "params", {})) for trial in finished]
    unique_keys = set(keys)
    values = []
    blocks = defaultdict(lambda: {"count": 0, "best": None, "values": []})
    for trial in finished:
        value = getattr(trial, "value", None)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        values.append(value)
        key = block_key(getattr(trial, "params", {}), opt_params_def)
        item = blocks[key]
        item["count"] += 1
        item["values"].append(value)
        item["best"] = value if item["best"] is None else max(item["best"], value)

    best = max(values) if values else None
    coverage = None
    if finite_size is not None and finite_size > 0:
        coverage = min(1.0, len(unique_keys) / float(finite_size))
    return {
        "finished": len(finished),
        "unique": len(unique_keys),
        "duplicates": max(0, len(finished) - len(unique_keys)),
        "finite_size": finite_size,
        "coverage": coverage,
        "best": best,
        "blocks": dict(blocks),
    }


def _grid_values(config):
    """按目标函数相同的离散语义展开单个参数。"""
    p_type = config.get("type")
    if p_type == "int":
        low = int(config["low"])
        high = int(config["high"])
        step = max(1, int(config.get("step", 1) or 1))
        return list(range(low, high + 1, step))
    if p_type == "float" and config.get("step") is not None:
        low = float(config["low"])
        high = float(config["high"])
        step = float(config["step"])
        count = int(math.floor((high - low) / step + 1e-10))
        return [round(low + index * step, 12) for index in range(count + 1)]
    if p_type == "categorical":
        return list(config.get("choices", []))
    return None


def build_global_pilot(opt_params_def, count, seed):
    """为连续或混合空间生成确定性的 Latin-hypercube 风格试验点。"""
    if not opt_params_def or count <= 0:
        return []
    names = list(opt_params_def)
    count = int(count)
    rng = np.random.default_rng(int(seed) % (2**32))
    strata = np.arange(count, dtype=float) + 0.5
    points = []
    shuffled = {}
    for name in names:
        values = _grid_values(opt_params_def[name])
        if values:
            indexes = rng.permutation(count) % len(values)
            shuffled[name] = [values[int(index)] for index in indexes]
            continue
        config = opt_params_def[name]
        if config.get("type") == "float":
            try:
                low = float(config["low"])
                high = float(config["high"])
            except (KeyError, TypeError, ValueError):
                shuffled[name] = [config.get("value") for _ in range(count)]
                continue
            if high < low:
                low, high = high, low
            values_u = strata.copy()
            rng.shuffle(values_u)
            shuffled[name] = [round(low + (high - low) * u / count, 12) for u in values_u]
        else:
            shuffled[name] = [config.get("value") for _ in range(count)]

    for index in range(count):
        params = {name: shuffled[name][index] for name in names}
        points.append(params)
    if count > 1:
        points[0] = {
            name: (_grid_values(config)[0] if _grid_values(config) else config.get("low", config.get("value")))
            for name, config in opt_params_def.items()
        }
        points[-1] = {
            name: (_grid_values(config)[-1] if _grid_values(config) else config.get("high", config.get("value")))
            for name, config in opt_params_def.items()
        }
    unique = []
    seen = set()
    for params in points:
        key = parameter_key(params)
        if key not in seen:
            seen.add(key)
            unique.append(params)
    return unique


def build_trial_plan(initial_budget, finite_size=None, explicit=False):
    """生成硬上限、自动上限和批次大小。"""
    initial = max(1, int(initial_budget))
    if explicit:
        target = min(initial, finite_size) if finite_size else initial
        return {
            "target": target,
            "max_target": target,
            "batch": target,
            "adaptive": False,
            "exhaustive": bool(finite_size and target == finite_size),
        }

    if finite_size and finite_size <= EXHAUSTIVE_GRID_MAX_COMBINATIONS:
        target = int(finite_size)
        return {
            "target": target,
            "max_target": target,
            "batch": min(target, AUTO_BATCH_MAX),
            "adaptive": False,
            "exhaustive": True,
        }

    max_target = min(
        AUTO_MAX_TRIALS,
        max(initial, initial * AUTO_MAX_MULTIPLIER),
        finite_size if finite_size else AUTO_MAX_TRIALS,
    )
    batch = min(AUTO_BATCH_MAX, max(AUTO_BATCH_MIN, int(math.ceil(initial / 4.0))))
    return {
        "target": min(initial, max_target),
        "max_target": max_target,
        "batch": min(batch, max_target),
        "adaptive": max_target > initial,
        "exhaustive": False,
    }


def should_extend(history, summary, target, max_target):
    """首批没有对照时允许扩一次；最近一批没有明显改善时停止。"""
    if int(target) >= int(max_target):
        return False
    coverage = summary.get("coverage")
    if coverage is not None and coverage >= 0.95:
        return False
    if len(history) < 2:
        return True
    previous = history[-2]
    current = summary.get("best")
    before = previous.get("best")
    if current is None or before is None:
        return False
    scale = max(1e-9, abs(float(before)), abs(float(current)))
    improvement = float(current) - float(before)
    return improvement > max(1e-9, scale * 0.005)


def grid_block_ids(grid_search_space, seed=0):
    """返回按 GridSampler 顺序排列的网格区块键。"""
    if not grid_search_space:
        return {}
    from optuna.samplers import GridSampler

    sampler = GridSampler(grid_search_space, seed=int(seed))
    names = list(sampler._param_names)
    configs = {}
    for name, values in grid_search_space.items():
        values = list(values)
        numeric = values and all(
            isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool)
            for value in values
        )
        if numeric:
            configs[name] = {
                "type": "float" if any(isinstance(value, (float, np.floating)) for value in values) else "int",
                "low": min(values),
                "high": max(values),
            }
        else:
            configs[name] = {"type": "categorical", "choices": values}
    return {
        grid_id: block_key(dict(zip(names, values)), configs)
        for grid_id, values in enumerate(sampler._all_grids)
    }


def shrink_grid_blocks(study, grid_search_space, active_ids, seed=0, min_samples=4):
    """根据保守的区块上置信界缩减尚未执行的网格。"""
    if not grid_search_space or not active_ids:
        return set(active_ids or ()), []
    block_by_grid = grid_block_ids(grid_search_space, seed=seed)
    active_ids = set(int(value) for value in active_ids)
    grouped = defaultdict(list)
    visited_ids = set()
    for trial in getattr(study, "trials", []):
        if not _finished_trial(trial):
            continue
        grid_id = getattr(trial, "system_attrs", {}).get("grid_id")
        if grid_id is None:
            continue
        grid_id = int(grid_id)
        visited_ids.add(grid_id)
        if grid_id in block_by_grid:
            try:
                value = float(trial.value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                grouped[block_by_grid[grid_id]].append(value)
    if len(grouped) < 2:
        return active_ids, []
    all_values = [value for values in grouped.values() for value in values]
    incumbent = max(all_values)
    scale = max(1e-9, float(np.std(all_values)), abs(float(incumbent)) * 0.02)
    abandoned = []
    for block, values in grouped.items():
        if len(values) < int(min_samples):
            continue
        mean = float(np.mean(values))
        stderr = float(np.std(values)) / math.sqrt(len(values))
        upper = mean + 1.96 * stderr
        if upper < incumbent - max(scale * 0.5, 1e-9):
            abandoned.append(block)
    if not abandoned:
        return active_ids, []
    best_blocks = {
        block for block, values in grouped.items() if values and max(values) >= incumbent
    }
    abandoned = [block for block in abandoned if block not in best_blocks]
    if len(abandoned) >= len(grouped):
        abandoned = abandoned[:-1]
    abandoned_set = set(abandoned)
    remaining = {
        grid_id for grid_id in active_ids
        if grid_id in visited_ids or block_by_grid.get(grid_id) not in abandoned_set
    }
    return remaining, abandoned
