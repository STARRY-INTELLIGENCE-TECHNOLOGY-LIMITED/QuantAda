"""只用标准库读取 Journal 元数据与轻量计数，供续传匹配和任务列表共用。"""

from array import array
import ast
import hashlib
import json
import os
from pathlib import Path
import re


TRIAL_STATES = ("RUNNING", "COMPLETE", "PRUNED", "FAIL", "WAITING")
# 新 Study 登记此口径。没有标记的历史 Study 按原名加载，不把旧评分改标成当前口径。
WORKER_CONFIG_VERSION = 1


def finished_trial_count(counts):
    """COMPLETE 与 PRUNED 计入预算；失败不占有效完成额度。"""
    if not counts:
        return 0
    return int(counts.get("COMPLETE", 0)) + int(counts.get("PRUNED", 0))


def parse_recorded_value(value):
    """恢复旧版以字符串保存的参数，不执行其中的表达式。"""
    if isinstance(value, str):
        try:
            return ast.literal_eval(value.strip())
        except (ValueError, SyntaxError):
            return value.strip()
    return value


def study_identity(values):
    """只比较影响训练的配置；并行度、预算和展示选项不改变已有试验的含义。"""
    defaults = {
        "strategy": None, "selection": None, "symbols": "SHSE.510300", "data_source": None,
        "cash": 100000.0, "commission": 0.0, "slippage": 0.001,
        "timeframe": "Days", "compression": 1, "risk": None,
        "params": {}, "opt_params": {}, "risk_params": {}, "config": {},
        "train_roll_period": None, "test_roll_period": None, "train_ratio": None,
        "train_period": None, "test_period": None,
    }
    result = {key: parse_recorded_value(values.get(key, default)) for key, default in defaults.items()}
    if result["cash"] is None:
        result["cash"] = 100000.0
    for key in ("train_roll_period", "test_roll_period"):
        if result[key]:
            result[key] = str(result[key]).lower()
    if result["selection"]:
        result["symbols"] = None
    else:
        result["symbols"] = [item.strip() for item in str(result["symbols"] or "").split(",") if item.strip()]
    return result


def read_study_metadata(path, *, with_trial_counts=False):
    """保留 Study 元数据；需要进度时每个 trial 仅保存 Study ID 和一个状态字节。"""
    studies = {}
    names = set()
    next_id = 0
    trial_studies = array("I")
    trial_states = bytearray()
    operations = {"0", "1", "2", "4", "6"} if with_trial_counts else {"0", "1", "2"}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            # 标准编码把 op_code 放在首字段；参数与采样器事件无需反序列化。
            if line.startswith('{"op_code":') and line[11:].lstrip()[:1] not in operations:
                continue
            record = json.loads(line)
            operation = record.get("op_code")
            if operation == 0 and record["study_name"] not in names:
                name = record["study_name"]
                studies[next_id] = {"name": name, "attrs": {}, "directions": record["directions"]}
                if with_trial_counts:
                    studies[next_id]["trial_counts"] = {state: 0 for state in TRIAL_STATES}
                names.add(name)
                next_id += 1
            elif operation == 1:
                removed = studies.pop(record["study_id"], None)
                if removed:
                    names.discard(removed["name"])
            elif operation == 2 and record["study_id"] in studies:
                # 续传会改写旧 Study 的参数快照；排序必须反映最近写入，而非创建先后。
                study = studies.pop(record["study_id"])
                study["attrs"].update(record["user_attr"])
                studies[record["study_id"]] = study
            elif with_trial_counts and operation == 4 and record["study_id"] in studies:
                study_id = record["study_id"]
                state = int(record.get("state", 0))
                trial_studies.append(study_id)
                trial_states.append(state)
                studies[study_id]["trial_counts"][TRIAL_STATES[state]] += 1
            elif with_trial_counts and operation == 6:
                trial_id = record["trial_id"]
                if not 0 <= trial_id < len(trial_states):
                    continue
                if trial_states[trial_id] in (1, 2, 3):
                    continue
                study = studies.get(trial_studies[trial_id])
                if study is None:
                    continue
                state = int(record["state"])
                study["trial_counts"][TRIAL_STATES[trial_states[trial_id]]] -= 1
                study["trial_counts"][TRIAL_STATES[state]] += 1
                trial_states[trial_id] = state
    return list(studies.values())

def batch_journal_path(path, studies=None):
    """批次 Journal 是快照和运行锁的锚点；没有标记时就是当前文件。"""
    path = Path(path).resolve()
    if studies is None and path.is_file():
        try:
            studies = read_study_metadata(path)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return path
    for study in studies or []:
        batch = study["attrs"].get("_optimizer_batch_journal")
        if batch:
            return Path(str(batch)).resolve()
    return path


def _batch_values(studies):
    values = set()
    for study in studies:
        batch = study["attrs"].get("_optimizer_batch_journal")
        if batch:
            values.add(str(Path(str(batch)).resolve()))
    return values


def related_journal_paths(path):
    """同一批次的 Journal。只跟随批次标记，不按训练身份合并两次运行。"""
    path = Path(path).resolve()
    if not path.is_file():
        return [path]
    anchor = batch_journal_path(path)
    keys = {str(anchor), str(path)}
    found = []
    for candidate in sorted(path.parent.glob("optuna_*.log")):
        resolved = candidate.resolve()
        try:
            studies = read_study_metadata(resolved)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            if resolved == path:
                raise
            continue
        batches = _batch_values(studies)
        if str(resolved) in keys or batches.intersection(keys):
            found.append(resolved)
            keys.update(batches)
            keys.add(str(resolved))
    return found or [path]


def _read_journal_lines(path):
    lines = []
    records = []
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            if not line.strip():
                continue
            records.append(json.loads(line))
            lines.append(line)
    return lines, records


def _annotate_journal(records):
    """按 Optuna 回放规则标注每行所属 Study，被忽略的 CREATE_TRIAL 不占用 trial_id。"""
    name_to_id = {}
    next_id = 0
    active = set()
    trial_owner = {}
    next_trial = 0
    owners = []
    for record in records:
        operation = record.get("op_code")
        owner = None
        if operation == 0:
            name = record.get("study_name")
            if name not in name_to_id:
                name_to_id[name] = next_id
                active.add(next_id)
                next_id += 1
            owner = name
        elif operation == 1:
            study_id = record.get("study_id")
            owner = next((name for name, sid in name_to_id.items() if sid == study_id), None)
            active.discard(study_id)
        elif operation in (2, 3):
            study_id = record.get("study_id")
            owner = next((name for name, sid in name_to_id.items() if sid == study_id), None)
        elif operation == 4:
            study_id = record.get("study_id")
            if study_id in active:
                owner = next((name for name, sid in name_to_id.items() if sid == study_id), None)
                trial_owner[next_trial] = owner
                next_trial += 1
        elif operation in (5, 6, 7, 8, 9):
            owner = trial_owner.get(record.get("trial_id"))
        owners.append(owner)
    return name_to_id, trial_owner, owners


def _dedicated_journal_path(directory, study_name, avoid, batch):
    digest = hashlib.sha256(study_name.encode("utf-8")).hexdigest()[:8]
    batch_tag = hashlib.sha256(str(batch).encode("utf-8")).hexdigest()[:6]
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", study_name).strip("_")[:40] or "study"
    candidate = Path(directory) / f"optuna_{safe}_{digest}_{batch_tag}.log"
    if candidate.resolve() == Path(avoid).resolve():
        candidate = Path(directory) / f"optuna_{safe}_{digest}_{batch_tag}_split.log"
    return candidate


def _atomic_write(path, text):
    path = Path(path)
    temporary = path.with_name(path.name + ".aligning")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise OSError(f"无法整理 Journal {path}: {exc}") from exc


def _has_batch_attr(records, owners, study_name):
    for record, owner in zip(records, owners):
        if owner == study_name and record.get("op_code") == 2:
            if "_optimizer_batch_journal" in (record.get("user_attr") or {}):
                return True
    return False


def _batch_attr_line(batch):
    return json.dumps({
        "op_code": 2, "worker_id": "quantada-journal-align", "study_id": 0,
        "user_attr": {"_optimizer_batch_journal": str(batch)},
    }, separators=(",", ":"), ensure_ascii=False) + "\n"


def _render_study(records, owners, study_name, batch):
    """重写为单 Study Journal，使回放后的 trial_id 等于控制台编号。"""
    name_to_id = {}
    next_id = 0
    active = set()
    trial_map = {}
    next_global = 0
    next_local = 0
    rendered = []
    seen_create = False
    has_batch = False
    for record, owner in zip(records, owners):
        operation = record.get("op_code")
        assigned = None
        if operation == 0 and record.get("study_name") not in name_to_id:
            name_to_id[record["study_name"]] = next_id
            active.add(next_id)
            next_id += 1
        elif operation == 1:
            active.discard(record.get("study_id"))
        elif operation == 4 and record.get("study_id") in active:
            assigned = next_global
            next_global += 1
        if owner != study_name:
            continue
        updated = dict(record)
        if operation == 0:
            if seen_create:
                continue
            seen_create = True
        elif operation in (1, 2, 3):
            updated["study_id"] = 0
            if operation == 2 and "_optimizer_batch_journal" in (record.get("user_attr") or {}):
                has_batch = True
        elif operation == 4:
            updated["study_id"] = 0
            trial_map[assigned] = next_local
            next_local += 1
        elif operation in (5, 6, 7, 8, 9):
            updated["trial_id"] = trial_map[record["trial_id"]]
        rendered.append(updated)
    text = "".join(json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n" for item in rendered)
    if batch and not has_batch:
        text += _batch_attr_line(batch)
    return text


def _find_study_journal(batch, study_name):
    if not Path(batch).is_file() and not Path(batch).parent.is_dir():
        return None
    for candidate in related_journal_paths(batch):
        if not candidate.is_file():
            continue
        try:
            studies = read_study_metadata(candidate)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            if candidate.resolve() == Path(batch).resolve():
                raise
            continue
        if any(study["name"] == study_name for study in studies):
            return candidate
    return None


def _trial_count(path, study_name):
    if not Path(path).is_file():
        return -1
    for study in read_study_metadata(path, with_trial_counts=True):
        if study["name"] == study_name:
            return sum(study.get("trial_counts", {}).values())
    return -1


def isolate_study_journal(path, study_name, batch_journal=None):
    """让目标 Study 独占 Journal。返回实际路径，以及被拆出的 Study 名称。"""
    path = Path(path).resolve()
    batch = Path(batch_journal).resolve() if batch_journal else path
    existing = _find_study_journal(batch, study_name)
    if existing is not None:
        path = existing
    elif path.is_file():
        present = [study["name"] for study in read_study_metadata(path)]
        if present and study_name not in present:
            path = _dedicated_journal_path(path.parent, study_name, path, batch)
            if not path.is_file():
                return str(path), ()
    if not path.is_file():
        return str(path), ()
    lines, records = _read_journal_lines(path)
    if not records:
        return str(path), ()
    name_to_id, trial_owner, owners = _annotate_journal(records)
    names = list(dict.fromkeys(name for name in owners if name))
    if study_name not in names:
        # 当前文件没有目标 Study，不能把新试验追加进来，否则 trial_id 会再次岔开。
        dedicated = _dedicated_journal_path(path.parent, study_name, path, batch)
        if dedicated.resolve() == path.resolve() or dedicated.is_file():
            dedicated = dedicated.with_name(dedicated.stem + "_new.log")
        return str(dedicated), ()
    if names == [study_name]:
        return str(path), ()
    moved = []
    for name in names:
        if name == study_name:
            continue
        destination = _dedicated_journal_path(path.parent, name, path, batch)
        while destination.exists() and _trial_count(destination, name) < 0:
            destination = destination.with_name(destination.stem + "_split.log")
        source_count = sum(
            1 for record, owner in zip(records, owners)
            if owner == name and record.get("op_code") == 4
        )
        if _trial_count(destination, name) < source_count:
            _atomic_write(destination, _render_study(records, owners, name, batch))
        moved.append(name)
    owned_ids = [trial_id for trial_id, owner in trial_owner.items() if owner == study_name]
    identity = name_to_id.get(study_name) == 0 and owned_ids == list(range(len(owned_ids)))
    if identity:
        text = "".join(line for line, owner in zip(lines, owners) if owner == study_name)
        if batch and not _has_batch_attr(records, owners, study_name):
            text += _batch_attr_line(batch)
    else:
        text = _render_study(records, owners, study_name, batch)
    _atomic_write(path, text)
    return str(path), tuple(moved)
