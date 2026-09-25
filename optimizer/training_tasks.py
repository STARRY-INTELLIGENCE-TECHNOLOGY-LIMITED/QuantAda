"""CLI 与命令工作台共用的训练任务目录、参数恢复和交互选择。"""

from datetime import datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import warnings

from common.runtime_command import format_cli_command
from common.terminal_log import (
    OPTIMIZER_AI_ANALYSIS_END_MARKER, OPTIMIZER_AI_ANALYSIS_START_MARKER, get_optimizer_terminal_log_path,
)
from optimizer.journal_metadata import (
    WORKER_CONFIG_VERSION, TRIAL_STATES, finished_trial_count, parse_recorded_value, read_study_metadata, study_identity,
)


RESUME_OPTIONS = (
    "selection", "data_source", "symbols", "cash", "commission", "slippage", "start_date", "end_date",
    "risk", "timeframe", "compression", "desc", "plot_scope", "n_jobs", "train_roll_period",
    "test_roll_period", "train_ratio", "train_period", "test_period",
)
_STATUS_FINISHED = "Finished"
_STATUS_UNFINISHED = "Incomplete"
_DISPLAY_TAIL_BYTES = 1024 * 1024
_SECTION_FINISHED = "finished"
_SECTION_EMPTY = "empty"
_SECTION_OPEN = "open"
_DISPLAY_SECTION_BYTES = 8 * 1024 * 1024
_SUMMARY_SUFFIX_BYTES = 16 * 1024
LOG_PAGE_LINES = 40
_LOG_LINE_LIMIT = 8192
_SNAPSHOT_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_JOURNAL_LOCATION = re.compile(r"(?:Using JournalStorage|Training Journal):\s*(.+)$")
_REUSED_WINDOW = re.compile(r"Reusing training window:\s*(\S+)\s+to\s+(\S+)")
_SCOPE_WINDOW = re.compile(
    r"Training scope:\s*window=(\S+)\.\.(\S+?)(?:\s+snapshot=([0-9a-fA-F]{32}))?\s*$"
)


@lru_cache(maxsize=128)
def _journal_snapshot(path, modified_ns, size):
    """缓存有文件签名的派生摘要，不保留历史 trial 对象。"""
    return read_study_metadata(path, with_trial_counts=True)


@lru_cache(maxsize=128)
def _terminal_header(path, modified_ns, size):
    """旧任务没有保存完整指标列表时，从终端日志头部恢复，避免扫描长篇试验输出。"""
    metrics = []
    journal = None
    budget = None
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        header = stream.read(1024 * 1024)
    for line in header.splitlines():
        target = re.search(r"PARAMETER OPTIMIZATION \(Target:\s*(.*?)\)", line)
        if target:
            metrics = [item.strip() for item in target.group(1).split(",") if item.strip()]
        location = re.search(r"(?:Using JournalStorage|Training Journal):\s*(.+)$", line)
        if location:
            journal = re.split(r"[\\/]", location.group(1).strip().strip("\"'"))[-1]
        inferred = re.search(r"Auto-inferred n_trials:\s*(\d+)", line)
        if inferred:
            budget = int(inferred.group(1))
        if journal and "Starting Optimization (" in line:
            break
    return journal, metrics, budget


def _summary_suffix_empty(text):
    """结束标记后的崩溃摘要没有训练结果，不能当成训练完毕。"""
    if any(marker in text for marker in (
        "训练指标未返回结果",
        "所有指标均未返回结果",
        "Training metrics returned no results",
        "All metrics returned no results",
    )):
        return True
    return bool(
        re.search(r"Metrics completed:\s*0\b", text)
        and re.search(r"Completed trials:\s*0\b", text)
    )


@lru_cache(maxsize=128)
def _display_section_state(path, modified_ns, size):
    """只读日志末尾展示段。返回 finished、empty 或 open；空摘要不是训练完毕。"""
    if size <= 0:
        return _SECTION_OPEN
    end_marker = OPTIMIZER_AI_ANALYSIS_END_MARKER.encode("utf-8")
    start_marker = OPTIMIZER_AI_ANALYSIS_START_MARKER.encode("utf-8")
    with Path(path).open("rb") as stream:
        probe = min(size, _DISPLAY_TAIL_BYTES)
        stream.seek(size - probe)
        tail = stream.read(probe)
        end_at = tail.rfind(end_marker)
        start_at = tail.rfind(start_marker)
        if end_at < 0 or start_at > end_at:
            return _SECTION_OPEN
        if start_at >= 0:
            between = tail[start_at:end_at]
        else:
            end_pos = size - probe + end_at
            window = min(end_pos, _DISPLAY_SECTION_BYTES)
            stream.seek(end_pos - window)
            display = stream.read(window)
            marker_at = display.rfind(start_marker)
            if marker_at < 0:
                return _SECTION_OPEN
            between = display[marker_at:]
        stream.seek(size - probe + end_at + len(end_marker))
        suffix = stream.read(_SUMMARY_SUFFIX_BYTES)
    text = (between + b"\n" + suffix).decode("utf-8", errors="replace")
    if _summary_suffix_empty(text):
        return _SECTION_EMPTY
    return _SECTION_FINISHED


def _journal_basename(value):
    """只保留 Journal 文件名，不接受路径跳转。"""
    text = str(value or "").strip().strip("\"'")
    name = re.split(r"[\\/]", text)[-1] if text else ""
    if not name or name in {".", ".."} or ".." in name:
        return ""
    return name


def _normalize_window(window):
    """窗口必须是两个非空日期；缺失时不作为区分条件。"""
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        return None
    start, end = str(window[0]).strip(), str(window[1]).strip()
    if not start or not end or start == "None" or end == "None":
        return None
    return (start, end)


def note_terminal_scope(journal, *, window=None, snapshot_id=None):
    """把本轮打开的 Journal 追加到终端日志旁的 scope，不扫描试验全文。"""
    normalized = _normalize_window(window)
    snapshot = str(snapshot_id).lower() if snapshot_id and _SNAPSHOT_ID.fullmatch(str(snapshot_id)) else ""
    line = ""
    if normalized:
        line = f"[Optimizer] Training scope: window={normalized[0]}..{normalized[1]}"
        if snapshot:
            line += f" snapshot={snapshot}"
    terminal = get_optimizer_terminal_log_path()
    name = _journal_basename(journal)
    if terminal and name:
        record = {"journal": name}
        if normalized:
            record["window"] = list(normalized)
        if snapshot:
            record["snapshot"] = snapshot
        try:
            path = Path(str(terminal) + ".scope")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return line


def announce_training_scope(journal, window, snapshot_id=None):
    """记录并打印训练范围，供状态和日志按 Journal、窗口、快照对应。"""
    line = note_terminal_scope(journal, window=window, snapshot_id=snapshot_id)
    if line:
        print(line)
    return line


def _task_snapshot_id(task):
    """从任务记录取出快照 ID；旧字符串记录先还原，不执行表达式。"""
    raw = (task.get("recorded") or {}).get("_optimizer_data_snapshot")
    parsed = parse_recorded_value(raw)
    value = str(parsed.get("id") or "") if isinstance(parsed, dict) else str(parsed or "")
    return value.lower() if _SNAPSHOT_ID.fullmatch(value) else None


def _scope_matches(scope, *, journal_name, window, snapshot_id, journal_task_count):
    """日志上出现的窗口和快照都必须与任务一致；都没有时不能覆盖同名多任务。"""
    if journal_name not in scope["journals"]:
        return False
    discriminators = 0
    if scope["window"] is not None:
        discriminators += 1
        if window != scope["window"]:
            return False
    if scope["snapshot"] is not None:
        discriminators += 1
        if snapshot_id != scope["snapshot"]:
            return False
    if discriminators == 0 and journal_task_count > 1:
        return False
    return True


def _read_terminal_scope(path, modified_ns, size):
    """合并日志头部和旁路 scope。头部在首个试验开始处停止，不读试验全文。"""
    scope_path = Path(str(path) + ".scope")
    try:
        info = scope_path.stat()
        scope_mtime, scope_size = info.st_mtime_ns, info.st_size
    except OSError:
        scope_mtime, scope_size = 0, 0
    journals, window, snapshot = _terminal_scope(str(path), modified_ns, size, scope_mtime, scope_size)
    return {"journals": journals, "window": window, "snapshot": snapshot}


@lru_cache(maxsize=128)
def _terminal_scope(path, modified_ns, size, scope_mtime_ns, scope_size):
    """缓存有文件签名的 Journal 范围；scope 变化单独失效，不另建任务索引。"""
    journals = []
    window = None
    snapshot = None
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        header = stream.read(1024 * 1024)
    for line in header.splitlines():
        location = _JOURNAL_LOCATION.search(line)
        if location:
            name = _journal_basename(location.group(1))
            if name and name not in journals:
                journals.append(name)
        reused = _REUSED_WINDOW.search(line)
        if reused:
            parsed_window = _normalize_window((reused.group(1), reused.group(2)))
            if parsed_window:
                window = parsed_window
        parsed = _SCOPE_WINDOW.search(line)
        if parsed:
            parsed_window = _normalize_window((parsed.group(1), parsed.group(2)))
            if parsed_window:
                window = parsed_window
            if parsed.group(3):
                snapshot = parsed.group(3).lower()
        if journals and "Starting Optimization (" in line:
            break
    if scope_mtime_ns and scope_size:
        try:
            text = Path(str(path) + ".scope").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for raw in text.splitlines():
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            name = _journal_basename(record.get("journal"))
            if name and name not in journals:
                journals.append(name)
            parsed_window = _normalize_window(record.get("window"))
            if parsed_window:
                window = parsed_window
            recorded_snapshot = str(record.get("snapshot") or "")
            if _SNAPSHOT_ID.fullmatch(recorded_snapshot):
                snapshot = recorded_snapshot.lower()
    return tuple(journals), window, snapshot


def _ranked_terminal_logs(directory):
    """按修改时间倒序列出终端日志及其范围；空摘要留给调用方决定是否跳过。"""
    terminal_dir = Path(directory).parent / "optimizer"
    if not terminal_dir.is_dir():
        return []
    logs = []
    for terminal in terminal_dir.glob("optimizer_terminal_*.log"):
        try:
            info = terminal.stat()
            scope = _read_terminal_scope(str(terminal), info.st_mtime_ns, info.st_size)
            section = _display_section_state(str(terminal), info.st_mtime_ns, info.st_size)
        except OSError:
            continue
        logs.append((info.st_mtime_ns, terminal, scope, section))
    logs.sort(reverse=True)
    return logs


def _status_for_task(task, logs, journal_task_count):
    """最新匹配且非空的展示段决定状态；崩溃空摘要继续看更早的匹配日志。"""
    journal_name = _journal_basename(task.get("journal"))
    window = _normalize_window((task.get("start_date"), task.get("end_date")))
    snapshot_id = _task_snapshot_id(task)
    for _modified_ns, _path, scope, section in logs:
        if not _scope_matches(
            scope, journal_name=journal_name, window=window, snapshot_id=snapshot_id,
            journal_task_count=journal_task_count,
        ):
            continue
        if section == _SECTION_EMPTY:
            continue
        return _STATUS_FINISHED if section == _SECTION_FINISHED else _STATUS_UNFINISHED
    return _STATUS_UNFINISHED


def _apply_training_status(tasks, directory):
    """同一 Journal 文件的多个任务分别匹配日志，不能共用一个 Finished。"""
    logs = _ranked_terminal_logs(directory)
    counts = {}
    for task in tasks:
        name = _journal_basename(task.get("journal"))
        counts[name] = counts.get(name, 0) + 1
    for task in tasks:
        task["training_status"] = _status_for_task(task, logs, counts[_journal_basename(task.get("journal"))])


def _legacy_isolation_notice(complete, budget):
    """没有口径标记时加载原 Study，已完成试验计入预算，控制台编号不从 0 重开。"""
    budget_text = str(budget) if isinstance(budget, int) and budget >= 0 else "the saved budget"
    return (
        "No current configuration marker was recorded. Resume loads the original study; "
        "{complete} completed trials count toward budget {budget}, and only unfinished combinations run. "
        "Console Trial numbers match Journal trial_id and continue from the original study instead of starting at 0."
    ).format(complete=int(complete), budget=budget_text)
def _recorded_metrics(value):
    parsed = parse_recorded_value(value)
    if isinstance(parsed, str):
        parsed = parsed.split(",")
    if not isinstance(parsed, (list, tuple)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in parsed if str(item).strip()))


def list_training_tasks(journal_dir):
    """按最近更新时间倒序列出任务；同 Journal 的配置和窗口不同则分开。"""
    directory = Path(journal_dir).resolve()
    tasks = []
    legacy_headers = None
    for path in sorted(directory.glob("optuna_*.log"), reverse=True):
        try:
            stat = path.stat()
            studies = _journal_snapshot(str(path), stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            warnings.warn(f"Unable to read training record {path.name}: {exc}", RuntimeWarning)
            continue
        groups = {}
        contexts = set()
        for study in studies:
            attrs = study["attrs"]
            identity = study_identity(attrs)
            window = [None if identity["train_roll_period"] else attrs.get("start_date"), attrs.get("end_date")]
            context = {"identity": identity, "window": window}
            contexts.add(json.dumps(context, sort_keys=True, default=str))
            key = json.dumps({**context, "worker_config_version": attrs.get("_optimizer_worker_config_version"),
                              "data_snapshot": attrs.get("_optimizer_data_snapshot")}, sort_keys=True, default=str)
            group = groups.setdefault(key, {"studies": {}, "metrics": []})
            metric = str(attrs.get("metric") or "").strip()
            group["studies"].pop(metric, None)
            group["studies"][metric] = study
            group["metrics"].extend(_recorded_metrics(attrs.get("_optimizer_metrics")))
        for key, group in groups.items():
            latest = list(group["studies"].values())
            if not latest:
                continue
            attrs = dict(latest[-1]["attrs"])
            metrics = list(dict.fromkeys(group["metrics"]))
            inferred_budget = None
            if not metrics and len(contexts) == 1:
                if legacy_headers is None:
                    legacy_headers = {}
                    terminal_dir = directory.parent / "optimizer"
                    for terminal in sorted(terminal_dir.glob("optimizer_terminal_*.log"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
                        try:
                            info = terminal.stat()
                            journal, original_metrics, budget = _terminal_header(str(terminal), info.st_mtime_ns, info.st_size)
                            if journal and original_metrics:
                                previous_metrics, previous_budget = legacy_headers.get(journal, ([], None))
                                legacy_headers[journal] = (
                                    list(dict.fromkeys(previous_metrics + original_metrics)),
                                    previous_budget if previous_budget is not None else budget,
                                )
                        except OSError:
                            continue
                original_metrics, inferred_budget = legacy_headers.get(path.name, ([], None))
                if set(group["studies"]).intersection(original_metrics):
                    metrics = list(original_metrics)
            metrics = list(dict.fromkeys(metrics + [metric for metric in group["studies"] if metric]))
            counts = {state: sum(study["trial_counts"][state] for study in latest) for state in TRIAL_STATES}
            budget = parse_recorded_value(attrs.get("_optimizer_target_trials"))
            if budget is None:
                budget = parse_recorded_value(attrs.get("n_trials")) or inferred_budget
            task = {
                "task_id": hashlib.sha256((str(path) + "\n" + key).encode("utf-8")).hexdigest()[:24],
                "journal": str(path), "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "updated_ns": stat.st_mtime_ns, "strategy": attrs.get("strategy") or latest[-1]["name"],
                "metrics": metrics, "start_date": attrs.get("start_date"), "end_date": attrs.get("end_date"),
                "trial_counts": counts, "n_trials": budget, "study_names": [study["name"] for study in latest],
                "recorded": attrs, "resumable": True, "reason": "",
                "worker_config_version": attrs.get("_optimizer_worker_config_version"),
                "training_status": _STATUS_UNFINISHED,
                "notice": "" if attrs.get("_optimizer_worker_config_version") == WORKER_CONFIG_VERSION else
                          _legacy_isolation_notice(counts.get("COMPLETE", 0), budget),
            }
            if not task["notice"] and not attrs.get("_optimizer_data_snapshot"):
                task["notice"] = "No training data snapshot was recorded. Resume still loads the original study and counts completed trials. Data is prepared once and bound; a new empty study is not created."
            try:
                if any(study["directions"] != [2] for study in latest):
                    raise ValueError("The saved task score direction does not match the training entry point")
                build_resume_arguments(task)
            except (ValueError, TypeError) as exc:
                task.update(resumable=False, reason=str(exc))
            tasks.append(task)
    _apply_training_status(tasks, directory)
    _prefer_richer_resume_target(tasks)
    return sorted(tasks, key=lambda task: (
        -task["updated_ns"], task["worker_config_version"] != WORKER_CONFIG_VERSION, task["task_id"],
    ))


def _prefer_richer_resume_target(tasks):
    """同一批次和训练身份里，恢复命令指向已完成更多的 Study，避免薄的隔离 Study 盖过已探索参数。"""
    groups = {}
    for task in tasks:
        batch = parse_recorded_value(task["recorded"].get("_optimizer_batch_journal")) or task["journal"]
        try:
            batch = str(Path(str(batch)).resolve())
        except OSError:
            batch = str(batch)
        key = json.dumps({
            "batch": batch, "identity": study_identity(task["recorded"]),
            "window": [task["start_date"], task["end_date"]], "metrics": task["metrics"],
        }, sort_keys=True, default=str)
        groups.setdefault(key, []).append(task)
    for group in groups.values():
        best = max(group, key=lambda task: finished_trial_count(task["trial_counts"]))
        if finished_trial_count(best["trial_counts"]) <= 0 or not best.get("study_names"):
            continue
        for task in group:
            if task is best or finished_trial_count(best["trial_counts"]) <= finished_trial_count(task["trial_counts"]):
                continue
            task["resume_anchor"] = best["study_names"][0]
            extra = "Another study in the same batch has more completed trials. Resume loads that study and reuses explored parameters."
            task["notice"] = f"{task.get('notice') or ''} {extra}".strip()


def build_resume_arguments(task, extra_arguments=()):
    """恢复训练参数数组；不会恢复实盘入口、启动等待或强制刷新行情。"""
    attrs = task["recorded"]
    strategy = str(attrs.get("strategy") or "").strip()
    if not strategy or strategy.startswith("-"):
        raise ValueError("The saved task has no valid strategy name")
    required = set(study_identity(attrs)) - ({"symbols"} if parse_recorded_value(attrs.get("selection")) else set())
    if not required.issubset(attrs):
        raise ValueError("The saved training configuration is incomplete")
    if parse_recorded_value(attrs.get("connect")):
        raise ValueError("Live connections cannot be resumed")
    if not task["metrics"] or not parse_recorded_value(attrs.get("end_date")) or not parse_recorded_value(attrs.get("start_date")):
        raise ValueError("The saved task is missing metrics or the original data window")
    arguments = [strategy]
    for key in ("params", "opt_params", "risk_params", "config"):
        value = parse_recorded_value(attrs.get(key))
        if not isinstance(value, dict) or (key == "opt_params" and not value):
            raise ValueError(f"The saved {key} is not a valid parameter dictionary")
        arguments.extend(("--" + key, repr(value)))
    for key in RESUME_OPTIONS:
        value = parse_recorded_value(attrs.get(key))
        if value is not None and value != "":
            arguments.extend(("--" + key, str(value)))
    if task["n_trials"] is not None:
        arguments.extend(("--n_trials", str(task["n_trials"])))
    if parse_recorded_value(attrs.get("no_plot", "True")):
        arguments.append("--no_plot")
    arguments.extend(("--metric", ",".join(task["metrics"]), "--study_journal", task["journal"]))
    anchor = task.get("resume_anchor")
    if not anchor and (attrs.get("_optimizer_worker_config_version") == WORKER_CONFIG_VERSION
            and attrs.get("_optimizer_data_snapshot") and task.get("study_names")):
        anchor = task["study_names"][0]
    if anchor:
        arguments.extend(("--study_name", anchor))
    arguments.extend(extra_arguments)
    for index, item in enumerate(extra_arguments):
        if item == "--config" or item.startswith("--config="):
            value = item.split("=", 1)[1] if "=" in item else extra_arguments[index + 1]
            overrides = parse_recorded_value(value)
            if not isinstance(overrides, dict):
                raise ValueError("--config must be a Python dictionary")
            merged = {**parse_recorded_value(attrs["config"]), **overrides}
            arguments.extend(("--config", repr(merged)))
    return arguments


def training_task_commands(task, shell=None):
    """恢复原始启动参数，并提供不绑定旧任务、强制获取最新行情的手动命令。"""
    recorded = task["recorded"]
    original = recorded.get("_optimizer_original_argv")
    has_recorded = isinstance(original, list) and bool(original) and all(isinstance(item, str) for item in original)
    exact = has_recorded and recorded.get("_optimizer_original_exact", True) is True
    if not has_recorded:
        original = build_resume_arguments(task)
    # Journal 是恢复入口，不属于用户手动重新训练的任务绑定。
    fresh = []
    skip_value = False
    remove_options = {"--study_name", "--study_journal", "--opt_schedule"}
    if not exact and parse_recorded_value(recorded.get("train_roll_period")):
        remove_options.update(("--start_date", "--end_date"))
    for item in original:
        if skip_value:
            skip_value = False
            continue
        option = item.split("=", 1)[0]
        if option in remove_options:
            skip_value = "=" not in item
        elif option not in {"--train_resume", "--refresh"}:
            fresh.append(item)
    if not has_recorded:
        original = []
        skip_value = False
        for item in build_resume_arguments(task):
            if skip_value:
                skip_value = False
            elif item in {"--study_name", "--study_journal"}:
                skip_value = True
            else:
                original.append(item)
    fresh.append("--refresh")
    return {
        "original_argv": list(original), "original_exact": exact,
        "original_command": format_cli_command(["python", "run.py", *original], shell),
        "fresh_argv": fresh,
        "fresh_command": format_cli_command(["python", "run.py", *fresh], shell),
    }


@lru_cache(maxsize=8)
def _log_page_offsets(path, modified_ns, size, page_lines):
    """只保存每页起点，不把整份终端日志留在内存。"""
    offsets = [0]
    if size <= 0:
        return tuple(offsets)
    lines = 0
    with Path(path).open("rb") as stream:
        while True:
            line = stream.readline()
            if not line:
                break
            lines += 1
            if lines % page_lines == 0:
                pos = stream.tell()
                if pos < size:
                    offsets.append(pos)
    return tuple(offsets)


def _page_index_for_offset(offsets, target):
    index = 0
    for pos, offset in enumerate(offsets):
        if offset <= target:
            index = pos
        else:
            break
    return index


def _display_marker_offset(path, size):
    """定位末尾展示段的开始标记；不在末尾窗口内则视为尚未写完。"""
    if size <= 0:
        return None
    end_marker = OPTIMIZER_AI_ANALYSIS_END_MARKER.encode("utf-8")
    start_marker = OPTIMIZER_AI_ANALYSIS_START_MARKER.encode("utf-8")
    with Path(path).open("rb") as stream:
        probe = min(size, _DISPLAY_TAIL_BYTES)
        stream.seek(size - probe)
        tail = stream.read(probe)
        end_at = tail.rfind(end_marker)
        start_at = tail.rfind(start_marker)
        if start_at >= 0 and (end_at < 0 or start_at < end_at):
            return size - probe + start_at
        if end_at < 0:
            return None
        end_pos = size - probe + end_at
        window = min(end_pos, _DISPLAY_SECTION_BYTES)
        stream.seek(end_pos - window)
        display = stream.read(window)
        at = display.rfind(start_marker)
        if at < 0:
            return None
        return end_pos - window + at


def find_latest_terminal_log(
    journal_path, *, start_date=None, end_date=None, snapshot_id=None, journal_task_count=1,
):
    """返回匹配该任务的最新终端日志；没有关联日志时返回 None。"""
    journal_path = Path(journal_path)
    terminal_dir = journal_path.parent.parent / "optimizer"
    if not terminal_dir.is_dir():
        return None
    logs = []
    for terminal in terminal_dir.glob("optimizer_terminal_*.log"):
        try:
            info = terminal.stat()
        except OSError:
            continue
        logs.append((info.st_mtime_ns, terminal, info.st_size))
    window = _normalize_window((start_date, end_date))
    snapshot = str(snapshot_id).lower() if snapshot_id and _SNAPSHOT_ID.fullmatch(str(snapshot_id)) else None
    name = journal_path.name
    for modified_ns, terminal, size in sorted(logs, reverse=True):
        try:
            scope = _read_terminal_scope(str(terminal), modified_ns, size)
        except OSError:
            continue
        if _scope_matches(
            scope, journal_name=name, window=window, snapshot_id=snapshot, journal_task_count=journal_task_count,
        ):
            return terminal
    return None


def task_terminal_log(task, tasks):
    """按任务窗口和快照找日志，避免打开同一 Journal 里另一个任务的输出。"""
    name = _journal_basename(task.get("journal"))
    count = sum(1 for item in tasks if _journal_basename(item.get("journal")) == name)
    return find_latest_terminal_log(
        task["journal"], start_date=task.get("start_date"), end_date=task.get("end_date"),
        snapshot_id=_task_snapshot_id(task), journal_task_count=count,
    )


def read_terminal_log_page(path, *, page=None, where="end", page_lines=LOG_PAGE_LINES):
    """按页读取终端日志。默认末尾；where 还可为 start、middle、display。"""
    path = Path(path)
    info = path.stat()
    offsets = _log_page_offsets(str(path), info.st_mtime_ns, info.st_size, page_lines)
    pages = len(offsets)
    if page is not None:
        index = min(pages - 1, max(0, int(page) - 1))
    elif where == "start":
        index = 0
    elif where == "middle":
        index = _page_index_for_offset(offsets, info.st_size // 2)
    elif where == "display":
        marker = _display_marker_offset(path, info.st_size)
        if marker is None:
            raise ValueError("The summary section has not been written yet")
        index = _page_index_for_offset(offsets, marker)
    else:
        index = pages - 1
    lines = []
    with path.open("rb") as stream:
        stream.seek(offsets[index])
        for _ in range(page_lines):
            line = stream.readline()
            if not line:
                break
            if len(line) > _LOG_LINE_LIMIT:
                line = line[:_LOG_LINE_LIMIT] + b"...\n"
            lines.append(line.decode("utf-8", errors="replace"))
    return {
        "path": str(path), "name": path.name, "page": index + 1, "pages": pages,
        "text": "".join(lines),
    }


def _print_log_page(page):
    print(f"\nTerminal log {page['name']} | page {page['page']}/{page['pages']}")
    if page["text"]:
        print(page["text"], end="" if page["text"].endswith("\n") else "\n")
    else:
        print("(this page is empty)")
    print("n next | p previous | h first | e end | m middle | s summary | g page | b back | q quit")


def _view_terminal_log(task, tasks):
    """从末尾打开该任务匹配的最新终端日志；返回 detail 或 quit。"""
    path = task_terminal_log(task, tasks)
    if path is None:
        print("No terminal log found for this task.")
        return "detail"
    where = "end"
    page_number = None
    while True:
        try:
            current = read_terminal_log_page(path, page=page_number, where=where)
        except ValueError as exc:
            print(exc)
            where = "end"
            page_number = None
            continue
        except OSError as exc:
            print(f"Unable to read terminal log: {exc}")
            return "detail"
        _print_log_page(current)
        try:
            answer = input("Log action: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nResume cancelled.")
            return "quit"
        if answer in {"q", "quit", "exit", "0"}:
            print("Resume cancelled.")
            return "quit"
        if answer in {"b", "返回", ""}:
            return "detail"
        page_number = None
        where = "end"
        if answer in {"n", "next"}:
            page_number = min(current["pages"], current["page"] + 1)
        elif answer in {"p", "prev"}:
            page_number = max(1, current["page"] - 1)
        elif answer in {"h", "home", "首页"}:
            where = "start"
        elif answer in {"m", "middle", "中间"}:
            where = "middle"
        elif answer in {"s", "summary", "展示段"}:
            where = "display"
        elif answer.startswith("g") and answer[1:].strip().isdecimal():
            target = int(answer[1:].strip())
            if 1 <= target <= current["pages"]:
                page_number = target
            else:
                print(f"Page must be from 1 to {current['pages']}.")
                page_number = current["page"]
        elif answer not in {"e", "end", "末尾"}:
            print("Enter n / p / h / e / m / s / g page, b to go back, or q to quit.")
            page_number = current["page"]


def choose_training_task(tasks, extra_arguments=()):
    """每页十项；选择后进入详情，确认才启动，也可分页查看终端日志。"""
    if not tasks:
        print("No historical training tasks found.")
        return None
    page = 0
    pages = (len(tasks) + 9) // 10
    selected = None
    redraw = True
    while True:
        if redraw and selected is None:
            print("\nHistorical training tasks (newest first)")
            for number, task in enumerate(tasks[page * 10:(page + 1) * 10], start=page * 10 + 1):
                counts = task["trial_counts"]
                print(f"{number:>3}. {task['updated_at']}  {task['strategy']}")
                print(f"     Metrics: {', '.join(task['metrics'])}  Window: {task['start_date']} -> {task['end_date']}")
                print(
                    f"     {task.get('training_status', _STATUS_UNFINISHED)}  "
                    f"complete {counts['COMPLETE']} / failed {counts['FAIL']} / pending {counts['RUNNING'] + counts['WAITING']}"
                )
                if not task["resumable"]:
                    print(f"     Not resumable: {task['reason']}")
            print(f"Total {len(tasks)} tasks | page {page + 1}/{pages} | 10 per page")
            redraw = False
        elif redraw:
            commands = training_task_commands(selected)
            print(
                f"\nSelected: {selected['strategy']} | {', '.join(selected['metrics'])} | "
                f"{selected.get('training_status', _STATUS_UNFINISHED)}"
            )
            label = "Original launch command" if commands["original_exact"] else "Original launch command (reconstructed from saved settings)"
            print(f"{label}:\n{commands['original_command']}")
            print(f"Manually train with fresh market data:\n{commands['fresh_command']}")
            print("Resume command:\n" + format_cli_command(["python", "run.py", *build_resume_arguments(selected, extra_arguments)]))
            if selected.get("notice"):
                print(selected["notice"])
            print("1 / y: confirm resume | l: view log | b / n / Enter: back to list | q: quit")
            redraw = False
        try:
            prompt = "Action: " if selected else "Task number | n next | p previous | g page | q quit: "
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nResume cancelled.")
            return None
        if answer in {"q", "quit", "exit", "0"} or (not answer and selected is None):
            print("Resume cancelled.")
            return None
        if selected is not None:
            if answer in {"1", "y", "yes", "确认"}:
                return selected
            if answer in {"l", "log", "日志"}:
                if _view_terminal_log(selected, tasks) == "quit":
                    return None
                redraw = True
                continue
            if answer in {"b", "n", "no", "", "返回"}:
                selected = None
                redraw = True
            else:
                print("Enter 1 / y to confirm, l to view the log, b to go back, or q to quit.")
            continue
        if answer in {"n", "p"}:
            page = min(pages - 1, page + 1) if answer == "n" else max(0, page - 1)
            redraw = True
            continue
        if answer.startswith("g") and answer[1:].strip().isdecimal():
            target = int(answer[1:].strip())
            if 1 <= target <= pages:
                page = target - 1
                redraw = True
            else:
                print(f"Page must be from 1 to {pages}.")
            continue
        if answer.isdecimal() and 1 <= int(answer) <= len(tasks):
            task = tasks[int(answer) - 1]
            if task["resumable"]:
                selected = task
                page = (int(answer) - 1) // 10
                redraw = True
            else:
                print(task["reason"])
        else:
            print("Enter a valid task number from the list.")
