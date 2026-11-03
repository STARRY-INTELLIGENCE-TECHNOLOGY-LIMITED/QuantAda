"""优化器搜索空间分析、全局覆盖和自适应预算工具。"""

from collections import defaultdict
from itertools import product
import hashlib
import json
import math

import numpy as np


# 自动预算只在未显式指定 --n_trials 时生效，避免改变用户明确给出的硬上限。
EXHAUSTIVE_GRID_MAX_COMBINATIONS = 4096
# Optuna GridSampler 会在构造时物化完整笛卡尔积；超过该值改用内存有界的 TPE。
GRID_SAMPLER_MAX_COMBINATIONS = 100_000
AUTO_MAX_TRIALS = 100000
AUTO_MAX_MULTIPLIER = 4
AUTO_BATCH_MIN = 16
AUTO_BATCH_MAX = 256
GLOBAL_PILOT_MIN = 16
GLOBAL_PILOT_MAX = 512
# Latin hypercube / GP 常用的 10d 初始设计规模。
PILOT_DIM_MULTIPLIER = 10
GRID_BLOCK_MIN_SAMPLES = 8


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


def grid_sampler_is_safe(grid_size):
    """判断有限网格是否可以安全交给 Optuna GridSampler 物化。"""
    try:
        size = int(grid_size)
    except (TypeError, ValueError, OverflowError):
        return False
    return 0 < size <= GRID_SAMPLER_MAX_COMBINATIONS


def finite_axis_size(config):
    """返回有限离散轴的档位数；连续、非法或空轴返回 None。"""
    if not isinstance(config, dict):
        return None
    p_type = config.get("type")
    if p_type == "int":
        try:
            low = int(config["low"])
            high = int(config["high"])
            step = max(1, int(config.get("step", 1) or 1))
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if high < low:
            return None
        return (high - low) // step + 1
    if p_type == "float" and config.get("step") is not None:
        try:
            low = float(config["low"])
            high = float(config["high"])
            step = float(config["step"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if high < low or not math.isfinite(step) or step <= 0:
            return None
        return int(math.floor((high - low) / step + 1e-10)) + 1
    if p_type == "categorical":
        choices = config.get("choices", [])
        if not isinstance(choices, (list, tuple)) or not choices:
            return None
        return len(choices)
    return None


def finite_search_space_size(opt_params_def):
    """返回全部有限离散轴的笛卡尔积大小；含连续轴或非法轴时返回 None。"""
    if not opt_params_def:
        return None
    size = 1
    for spec in opt_params_def.values():
        axis_size = finite_axis_size(spec)
        if not axis_size:
            return None
        size *= int(axis_size)
    return size


def _discrete_axis_value(config, index):
    """按与网格展开相同的离散语义取第 index 档，不物化整轴。"""
    p_type = config.get("type")
    slot = int(index)
    if p_type == "int":
        low = int(config["low"])
        step = max(1, int(config.get("step", 1) or 1))
        return low + slot * step
    if p_type == "float":
        low = float(config["low"])
        step = float(config["step"])
        return round(low + slot * step, 12)
    if p_type == "categorical":
        return list(config.get("choices", []))[slot]
    return config.get("value")


def recommended_pilot_count(target_trials, dimensions):
    """按 10d 规则给出全局 pilot 数；只依赖维度和目标预算，与硬件无关。"""
    target = max(1, int(target_trials))
    dims = max(1, int(dimensions))
    return min(target, GLOBAL_PILOT_MAX, max(GLOBAL_PILOT_MIN, dims * PILOT_DIM_MULTIPLIER))


def spread_pilot_count(target_trials, dimensions, n_blocks):
    """超大/混合空间的铺开量：每块至少 8 点，且不少于 10d 与一批上限，再封顶 512。"""
    target = max(1, int(target_trials))
    blocks = max(1, int(n_blocks))
    needed = blocks * GRID_BLOCK_MIN_SAMPLES
    return min(
        target,
        GLOBAL_PILOT_MAX,
        max(needed, recommended_pilot_count(target, dimensions), AUTO_BATCH_MAX),
    )


def _parameter_bucket(value, config):
    """把参数压到四个粗粒度区间，供区块诊断使用。"""
    if config.get("type") == "categorical":
        choices = list(config.get("choices", []))
        if not choices:
            return 0
        try:
            index = choices.index(value)
        except ValueError:
            return 0
        return min(3, int(index * 4 / len(choices)))

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


def _numeric_level_count(config):
    """按离散档位数衡量数值参数，不用 high-low 原始跨度。"""
    p_type = config.get("type")
    if p_type == "int":
        try:
            low = int(config["low"])
            high = int(config["high"])
            step = max(1, int(config.get("step", 1) or 1))
        except (KeyError, TypeError, ValueError, OverflowError):
            return 1
        if high < low:
            low, high = high, low
        return (high - low) // step + 1
    if p_type == "float":
        try:
            low = float(config.get("low", 0.0))
            high = float(config.get("high", low))
        except (TypeError, ValueError, OverflowError):
            return 1
        if high < low:
            low, high = high, low
        if abs(high - low) <= 1e-12:
            return 1
        step = config.get("step")
        try:
            step = float(step) if step is not None else None
        except (TypeError, ValueError, OverflowError):
            step = None
        if step is not None and math.isfinite(step) and step > 0:
            return int(math.floor((high - low) / step + 1e-10)) + 1
        # 连续浮点仍参与四桶分区，虚拟档位只用于挑选区块轴。
        return 32
    return 1


def _parameter_cardinality(config):
    """返回区块轴所用的离散档位数；网格重建时可带 _levels。"""
    if not isinstance(config, dict):
        return 1
    levels = config.get("_levels")
    if levels is not None:
        try:
            return max(1, int(levels))
        except (TypeError, ValueError, OverflowError):
            return 1
    p_type = config.get("type")
    if p_type == "categorical":
        return max(1, len(config.get("choices", [])))
    if p_type in {"int", "float"}:
        return max(1, _numeric_level_count(config))
    return 1


def _block_dimensions(opt_params_def):
    """选出档位最多的两个参数作为粗粒度区块轴。"""
    candidates = []
    for name, config in (opt_params_def or {}).items():
        cardinality = _parameter_cardinality(config)
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


def encode_block_key(key):
    """把区块键收成 Journal 可写的列表。"""
    encoded = []
    for name, bucket in key or ():
        if isinstance(bucket, (int, np.integer)) and not isinstance(bucket, bool):
            encoded.append([str(name), int(bucket)])
        else:
            encoded.append([str(name), bucket])
    return encoded


def decode_block_key(raw):
    """从 Journal 列表恢复区块键。"""
    decoded = []
    for item in raw or ():
        name, bucket = item[0], item[1]
        if isinstance(bucket, (int, float, np.integer, np.floating)) and not isinstance(bucket, bool):
            bucket = int(bucket)
        decoded.append((str(name), bucket))
    return tuple(decoded)


def encode_block_keys(keys):
    """编码一组区块键。"""
    return [encode_block_key(key) for key in keys or ()]


def decode_block_keys(raw):
    """解码一组区块键；非法输入返回 None。"""
    if not isinstance(raw, list):
        return None
    try:
        return [decode_block_key(item) for item in raw]
    except (TypeError, ValueError, IndexError):
        return None


def stabilize_block_keys(keys):
    """把区块键排成稳定顺序，避免 set 迭代或哈希随机化打乱轮转。"""
    if keys is None:
        return None
    normalized = [tuple((str(name), bucket) for name, bucket in key) for key in keys]
    return sorted(
        normalized,
        key=lambda key: json.dumps(
            encode_block_key(key), ensure_ascii=False, separators=(",", ":"), default=str,
        ),
    )


def _axis_buckets(config):
    """返回该轴实际会出现的四桶编号，超长轴不物化。"""
    if config.get("type") == "categorical":
        choices = list(config.get("choices", []))
        if not choices:
            return (0,)
        return tuple(sorted({min(3, int(index * 4 / len(choices))) for index in range(len(choices))}))
    axis_size = finite_axis_size(config)
    if axis_size is None and config.get("type") == "float":
        return (0, 1, 2, 3)
    try:
        size = int(axis_size or 0)
    except (TypeError, ValueError, OverflowError):
        size = 0
    if size <= 1:
        return (0,)
    if size <= 64:
        present = {
            _parameter_bucket(_discrete_axis_value(config, slot), config)
            for slot in range(size)
        }
        return tuple(sorted(present))
    return (0, 1, 2, 3)


def possible_block_keys(opt_params_def):
    """枚举粗粒度区块键；没有区块轴时只有空键。"""
    dimensions = _block_dimensions(opt_params_def)
    if not dimensions:
        return [()]
    bucket_lists = [_axis_buckets(opt_params_def[name]) for name in dimensions]
    keys = []
    for combo in product(*bucket_lists):
        keys.append(tuple((dimensions[index], combo[index]) for index in range(len(dimensions))))
    return keys


def _slot_range_for_bucket(config, bucket, axis_size):
    """用二分找到落入该桶的档位闭区间，不扫描超长轴。"""
    size = int(axis_size)
    bucket = int(bucket)
    if size <= 0:
        return None

    def bucket_of(slot):
        value = _parameter_bucket(_discrete_axis_value(config, slot), config)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    first = None
    left, right = 0, size - 1
    while left <= right:
        mid = (left + right) // 2
        if bucket_of(mid) < bucket:
            left = mid + 1
        else:
            first = mid
            right = mid - 1
    if first is None or bucket_of(first) != bucket:
        return None
    last = first
    left, right = first, size - 1
    while left <= right:
        mid = (left + right) // 2
        if bucket_of(mid) > bucket:
            right = mid - 1
        else:
            last = mid
            left = mid + 1
    return first, last


def _bucket_ratio(bucket, rank, count):
    """在桶内按 Latin 层取比值；0 桶含低端点，3 桶含高端点。"""
    bucket = int(bucket)
    lo = bucket / 4.0
    hi = 1.0 if bucket >= 3 else (bucket + 1) / 4.0
    if int(count) <= 1:
        if bucket <= 0:
            return lo
        if bucket >= 3:
            return 1.0
        return (lo + hi) / 2.0
    if bucket >= 3:
        return lo + (1.0 - lo) * float(rank) / float(count - 1)
    return lo + (hi - lo) * float(rank) / float(count)


def _random_ratio_in_bucket(bucket, rng):
    """在粗桶内均匀取样；半开区间 [lo, hi)，3 桶为 [0.75, 1.0)。"""
    bucket = int(bucket)
    lo = bucket / 4.0
    hi = 1.0 if bucket >= 3 else (bucket + 1) / 4.0
    span = hi - lo
    if span <= 0:
        return lo
    return lo + float(rng.random()) * span


def _value_from_ratio(config, ratio):
    """按 [0,1] 比值取轴上的值，离散轴走档位索引。"""
    ratio = min(1.0, max(0.0, float(ratio)))
    axis_size = finite_axis_size(config)
    if axis_size:
        slot = 0 if int(axis_size) <= 1 else int(round(ratio * (int(axis_size) - 1)))
        slot = min(int(axis_size) - 1, max(0, slot))
        return _discrete_axis_value(config, slot)
    if config.get("type") == "float":
        low = float(config["low"])
        high = float(config["high"])
        if high < low:
            low, high = high, low
        return round(low + (high - low) * ratio, 12)
    return config.get("value")


def _sample_axis_in_bucket(config, bucket, rank, count, rng):
    """在指定粗桶内取值；有限离散轴按档位闭区间取样，避免比值 round 到邻桶。rank/count 为空时在桶内随机取。"""
    random_slot = rank is None or count is None
    if random_slot and rng is None:
        rng = np.random.default_rng()
    if config.get("type") == "categorical":
        choices = list(config.get("choices", []))
        if not choices:
            return None
        slots = [
            index for index in range(len(choices))
            if min(3, int(index * 4 / len(choices))) == int(bucket)
        ]
        if not slots:
            slots = list(range(len(choices)))
        if random_slot:
            return choices[int(rng.choice(slots))]
        if int(count) <= 1:
            return choices[slots[len(slots) // 2]]
        position = int(round(float(rank) / float(max(1, count - 1)) * (len(slots) - 1)))
        return choices[slots[min(len(slots) - 1, max(0, position))]]
    axis_size = finite_axis_size(config)
    if axis_size:
        slot_range = _slot_range_for_bucket(config, bucket, axis_size)
        if slot_range is None:
            ratio = (
                _random_ratio_in_bucket(bucket, rng) if random_slot
                else _bucket_ratio(bucket, rank, count)
            )
            return _value_from_ratio(config, ratio)
        first, last = slot_range
        if random_slot:
            slot = int(rng.integers(first, last + 1))
        elif int(count) <= 1:
            slot = (first + last) // 2
        else:
            slot = first + int(round(float(rank) / float(count - 1) * (last - first)))
        return _discrete_axis_value(config, min(last, max(first, slot)))
    if random_slot:
        return _value_from_ratio(config, _random_ratio_in_bucket(bucket, rng))
    return _value_from_ratio(config, _bucket_ratio(bucket, rank, count))


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


def _discrete_pilot_indexes(axis_size, count, rng):
    """按轴索引生成 pilot 档位：样本不少于档位数时循环均衡，否则用含端点的 Latin 层。"""
    size = int(axis_size)
    count = int(count)
    if size <= 0 or count <= 0:
        return []
    if count == 1:
        return [size // 2]
    if count >= size:
        return [int(index) for index in rng.permutation(np.arange(count) % size)]
    values_u = np.arange(count, dtype=float) / float(count - 1)
    rng.shuffle(values_u)
    indexes = np.clip(np.round(values_u * (size - 1)).astype(int), 0, size - 1)
    return [int(index) for index in indexes]


def extend_history_record(summary, current_target):
    """把自动扩预算的批次摘要收成可写入 Journal 的记录。"""
    coverage = summary.get("coverage")
    best = summary.get("best")
    try:
        coverage = None if coverage is None else float(coverage)
    except (TypeError, ValueError, OverflowError):
        coverage = None
    try:
        best = None if best is None else float(best)
    except (TypeError, ValueError, OverflowError):
        best = None
    return {
        "target": int(current_target),
        "best": best,
        "coverage": coverage,
        "finished": int(summary.get("finished") or 0),
        "unique": int(summary.get("unique") or 0),
    }


def restore_extend_history(raw_history):
    """从 Study 元数据恢复扩预算历史；非法项丢弃。"""
    restored = []
    if not isinstance(raw_history, list):
        return restored
    for item in raw_history:
        if not isinstance(item, dict) or item.get("target") is None:
            continue
        try:
            restored.append(extend_history_record(item, item["target"]))
        except (TypeError, ValueError, OverflowError):
            continue
    return restored


def build_global_pilot(opt_params_def, count, seed):
    """为连续或混合空间生成确定性的 Latin-hypercube 风格试验点。"""
    if not opt_params_def or count <= 0:
        return []
    names = list(opt_params_def)
    count = int(count)
    rng = np.random.default_rng(int(seed) % (2**32))
    points = []
    shuffled = {}
    for name in names:
        config = opt_params_def[name]
        axis_size = finite_axis_size(config)
        if axis_size:
            # 按轴索引取样，超长离散轴不必先展开成完整列表。
            shuffled[name] = [
                _discrete_axis_value(config, index)
                for index in _discrete_pilot_indexes(axis_size, count, rng)
            ]
            continue
        if config.get("type") == "float":
            try:
                low = float(config["low"])
                high = float(config["high"])
            except (KeyError, TypeError, ValueError):
                shuffled[name] = [config.get("value") for _ in range(count)]
                continue
            if high < low:
                low, high = high, low
            if count == 1:
                shuffled[name] = [round((low + high) / 2.0, 12)]
            else:
                # 含端点的 Latin 层：i/(n-1) 覆盖 low 与 high，各维独立打乱。
                values_u = np.arange(count, dtype=float) / float(count - 1)
                rng.shuffle(values_u)
                shuffled[name] = [round(low + (high - low) * float(u), 12) for u in values_u]
        else:
            shuffled[name] = [config.get("value") for _ in range(count)]

    for index in range(count):
        params = {name: shuffled[name][index] for name in names}
        points.append(params)
    unique = []
    seen = set()
    for params in points:
        key = parameter_key(params)
        if key not in seen:
            seen.add(key)
            unique.append(params)
    return unique


def build_block_balanced_pilot(opt_params_def, count, seed, active_blocks=None):
    """按粗粒度区块轮转铺开；块内用 Latin 层，其它轴沿用全局 pilot。"""
    if not opt_params_def or count <= 0:
        return []
    keys = possible_block_keys(opt_params_def)
    if active_blocks is not None:
        allowed = set(active_blocks)
        keys = [key for key in keys if key in allowed]
    if not keys:
        return build_global_pilot(opt_params_def, count, seed)
    count = int(count)
    rng = np.random.default_rng(int(seed) % (2 ** 32))
    base = build_global_pilot(opt_params_def, count, seed)
    if not base:
        return []
    assignments = [keys[index % len(keys)] for index in range(count)]
    ranks = defaultdict(list)
    for index, key in enumerate(assignments):
        ranks[key].append(index)
    points = []
    for index in range(count):
        params = dict(base[index % len(base)])
        key = assignments[index]
        members = ranks[key]
        rank = members.index(index)
        for name, bucket in key:
            config = opt_params_def.get(name)
            if not isinstance(config, dict):
                continue
            params[name] = _sample_axis_in_bucket(config, bucket, rank, len(members), rng)
        points.append(params)
    unique = []
    seen = set()
    for params in points:
        item_key = parameter_key(params)
        if item_key not in seen:
            seen.add(item_key)
            unique.append(params)
    return unique


def assign_params_to_block(params, opt_params_def, target_block, rng=None):
    """只改两个区块轴，把参数放进指定块；块内随机取样，避免塌成单一代表点。"""
    repaired = dict(params or {})
    if not target_block:
        return repaired
    rng = np.random.default_rng() if rng is None else rng
    for name, bucket in target_block:
        config = opt_params_def.get(name)
        if isinstance(config, dict):
            repaired[name] = _sample_axis_in_bucket(config, bucket, None, None, rng)
    return repaired


def repair_params_to_active_blocks(
    params, opt_params_def, active_blocks, rng=None, trial_number=0,
):
    """多峰竞赛：按试验编号在活跃块间轮转，不扫描全部 trial，避免长跑时顶内存。"""
    repaired = dict(params or {})
    if not active_blocks or not opt_params_def:
        return repaired
    active = list(active_blocks)
    target = active[int(trial_number) % len(active)]
    if block_key(repaired, opt_params_def) == target:
        return repaired
    return assign_params_to_block(repaired, opt_params_def, target, rng)


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


def should_extend(summary, target, max_target):
    """覆盖率未到 95% 且未达上限时继续按批扩展；停机由多峰缩块和上限负责。"""
    if int(target) >= int(max_target):
        return False
    coverage = (summary or {}).get("coverage")
    if coverage is not None and coverage >= 0.95:
        return False
    return True


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
                "_levels": len(values),
            }
        else:
            configs[name] = {"type": "categorical", "choices": values}
    return {
        grid_id: block_key(dict(zip(names, values)), configs)
        for grid_id, values in enumerate(sampler._all_grids)
    }


# 双侧 95% t 临界值，按自由度从低到高。查找时用不超过当前 df 的最大临界值，避免档顶把小样本估松。
_T_CRIT_95 = (
    (1, 12.706),
    (2, 4.303),
    (3, 3.182),
    (4, 2.776),
    (5, 2.571),
    (6, 2.447),
    (7, 2.365),
    (8, 2.306),
    (9, 2.262),
    (10, 2.228),
    (12, 2.179),
    (15, 2.131),
    (20, 2.086),
    (30, 2.042),
    (60, 2.000),
    (120, 1.980),
)


def _t_crit_95(df):
    """双侧 95% t 临界值。小样本使用对应 df 的精确值，不用档顶更小的 t。"""
    try:
        degrees = int(df)
    except (TypeError, ValueError):
        return 12.706
    if degrees <= 1:
        return 12.706
    previous = 12.706
    for cutoff, value in _T_CRIT_95:
        if degrees < cutoff:
            return previous
        previous = value
    return 1.960


def _dominated_blocks(grouped, min_samples=GRID_BLOCK_MIN_SAMPLES):
    """用区块 t 上界对比最优区块 t 下界，返回明显劣势的块。"""
    if len(grouped) < 2:
        return []
    all_values = [value for values in grouped.values() for value in values]
    incumbent = max(all_values)
    best_block = max(grouped, key=lambda block: max(grouped[block]))
    best_values = grouped[best_block]
    if len(best_values) < int(min_samples) or len(best_values) <= 1:
        return []
    best_mean = float(np.mean(best_values))
    best_stderr = float(np.std(best_values, ddof=1)) / math.sqrt(len(best_values))
    best_lower = best_mean - _t_crit_95(len(best_values) - 1) * best_stderr
    spread = float(np.std(all_values, ddof=1)) if len(all_values) > 1 else 0.0
    scale = max(1e-9, spread, abs(float(incumbent)) * 0.02)
    margin = max(scale * 0.5, 1e-9)
    abandoned = []
    for block, values in grouped.items():
        if block == best_block or len(values) < int(min_samples) or len(values) <= 1:
            continue
        mean = float(np.mean(values))
        stderr = float(np.std(values, ddof=1)) / math.sqrt(len(values))
        upper = mean + _t_crit_95(len(values) - 1) * stderr
        if upper < best_lower - margin:
            abandoned.append(block)
    if len(abandoned) >= len(grouped):
        abandoned = abandoned[:-1]
    return abandoned


def shrink_grid_blocks(study, grid_search_space, active_ids, seed=0, min_samples=GRID_BLOCK_MIN_SAMPLES):
    """用区块 t 上界对比最优区块 t 下界，只丢掉明显劣势且尚未执行的格子。"""
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
    abandoned = _dominated_blocks(grouped, min_samples)
    if not abandoned:
        return active_ids, []
    abandoned_set = set(abandoned)
    remaining = {
        grid_id for grid_id in active_ids
        if grid_id in visited_ids or block_by_grid.get(grid_id) not in abandoned_set
    }
    return remaining, abandoned


def shrink_param_blocks(study, opt_params_def, active_blocks=None, min_samples=GRID_BLOCK_MIN_SAMPLES):
    """按试验参数分块，丢掉明显劣势的粗粒度区块；未采样块继续保留。"""
    keys = possible_block_keys(opt_params_def)
    if active_blocks is None:
        active = set(keys)
    else:
        active = set(active_blocks)
    if not active:
        return [], []
    grouped = defaultdict(list)
    for trial in getattr(study, "trials", []):
        if not _finished_trial(trial):
            continue
        try:
            value = float(getattr(trial, "value", None))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        grouped[block_key(getattr(trial, "params", {}) or {}, opt_params_def)].append(value)
    abandoned = _dominated_blocks(grouped, min_samples)
    if not abandoned:
        return stabilize_block_keys(active), []
    abandoned_set = set(abandoned)
    remaining = {key for key in active if key not in abandoned_set}
    if grouped:
        best_block = max(grouped, key=lambda block: max(grouped[block]))
        remaining.add(best_block)
    if not remaining:
        remaining = set(active)
        abandoned = []
    return stabilize_block_keys(remaining), [block for block in abandoned if block not in remaining]
