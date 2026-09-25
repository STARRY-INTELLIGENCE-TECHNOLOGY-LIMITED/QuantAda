"""按训练配置发现 Journal Study，并隔离同一 Journal 的重复启动。"""

import copy
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import re

import pandas as pd
import optuna
from optuna.samplers import GridSampler

from optimizer.journal_metadata import WORKER_CONFIG_VERSION, batch_journal_path, finished_trial_count, read_study_metadata, related_journal_paths, study_identity


def ensure_study_config_version(study):
    """空白 Study 登记当前口径。已有试验但没有标记时允许加载，不把旧评分改标成当前口径。"""
    version = study.user_attrs.get("_optimizer_worker_config_version")
    if version == WORKER_CONFIG_VERSION:
        return
    if version is not None:
        raise ValueError(
            f"Study {study.study_name} has an incompatible worker configuration version; "
            "use a new --study_name or omit --study_name for automatic isolation."
        )
    if study.get_trials(deepcopy=False):
        if not study.user_attrs.get("_optimizer_reused_pre_config_version"):
            study.set_user_attr("_optimizer_reused_pre_config_version", True)
        return
    study.set_user_attr("_optimizer_worker_config_version", WORKER_CONFIG_VERSION)


class RetryAwareGridSampler(GridSampler):
    """重试记录不消耗新的网格槽位，也不因旧 FAIL 已占满网格而提前停止重试。"""

    def before_trial(self, study, trial):
        if "grid_id" in trial.system_attrs or "fixed_params" in trial.system_attrs:
            return super().before_trial(study, trial)
        source_number = trial.system_attrs.get("failed_trial", trial.number)
        trials = study.get_trials(deepcopy=False)
        logical_number = sum(
            item.number < source_number and "failed_trial" not in item.system_attrs for item in trials
        )
        # 只调整采样器看到的编号；存储中的审计编号及试验对象保持不变。
        sampling_trial = copy.copy(trial)
        sampling_trial.number = logical_number
        return super().before_trial(study, sampling_trial)

    def after_trial(self, study, trial, state, values):
        pending = study.get_trials(
            deepcopy=False, states=(optuna.trial.TrialState.WAITING, optuna.trial.TrialState.RUNNING),
        )
        if any(item.number != trial.number and "failed_trial" in item.system_attrs for item in pending):
            return
        return super().after_trial(study, trial, state, values)


def prepare_trial_resume(study, storage):
    """调用方持有运行锁；恢复中断试验，并为尚无后续尝试的失败试验登记一次重试。"""
    state = optuna.trial.TrialState
    trials = study.get_trials(deepcopy=False)
    already_retried = {number for trial in trials for number in trial.system_attrs.get("retry_history", [])}
    running_count = 0
    failed_count = 0
    for trial in trials:
        if trial.state == state.RUNNING:
            storage.set_trial_state_values(trial._trial_id, state.WAITING)
            running_count += 1
        elif trial.state == state.FAIL and trial.number not in already_retried:
            # FAIL 是 Optuna 终态，保留原记录；重试血缘与参数在一次 CREATE_TRIAL 中原子写入。
            system_attrs = {
                **trial.system_attrs,
                "failed_trial": trial.system_attrs.get("failed_trial", trial.number),
                "retry_history": [*trial.system_attrs.get("retry_history", []), trial.number],
            }
            study.add_trial(optuna.trial.create_trial(
                state=state.WAITING,
                params=trial.params,
                distributions=trial.distributions,
                user_attrs=trial.user_attrs,
                system_attrs=system_attrs,
            ))
            failed_count += 1
    return running_count, failed_count



def _snapshot_key(study):
    ref = study["attrs"].get("_optimizer_data_snapshot")
    if isinstance(ref, dict):
        return str(ref.get("id") or "")
    if ref in (None, "", "None"):
        return ""
    return str(ref)


def _version_reusable(study):
    return study["attrs"].get("_optimizer_worker_config_version") in (None, WORKER_CONFIG_VERSION)


def select_resume_studies(source_studies, metrics, requested_name):
    """同一身份加载已完成更多的 Study，供 create_study(load_if_exists=True) 跳过已探索参数。"""
    named = next((study for study in source_studies if study["name"] == requested_name), None) if requested_name else None
    if named is not None and not _version_reusable(named):
        raise ValueError(
            "The requested Study has an incompatible worker configuration version; "
            "use a new --study_name or omit --study_name for automatic isolation."
        )
    reusable = [study for study in source_studies if _version_reusable(study)]
    incompatible = [study for study in source_studies if not _version_reusable(study)]
    groups = {}
    for study in reusable:
        groups.setdefault(_snapshot_key(study), []).append(study)
    if not groups:
        return [], incompatible, False, None

    def group_finished(key):
        return sum(finished_trial_count(study.get("trial_counts")) for study in groups[key])

    def group_recency(key):
        return min(study.get("_recency", 0) for study in groups[key])

    ranked = sorted(groups, key=lambda key: (group_finished(key), -group_recency(key)), reverse=True)
    chosen_key = ranked[0]
    switched = False
    if named is not None:
        preferred = _snapshot_key(named)
        if preferred in groups and group_finished(chosen_key) <= group_finished(preferred):
            chosen_key = preferred
        elif preferred in groups and chosen_key != preferred:
            switched = True
    fallback_snapshot = None
    if chosen_key == "":
        donor = next((key for key in ranked if key), "")
        if donor:
            fallback_snapshot = next(
                study["attrs"].get("_optimizer_data_snapshot")
                for study in groups[donor]
                if study["attrs"].get("_optimizer_data_snapshot")
            )
    matched = []
    for metric in metrics:
        options = [study for study in groups[chosen_key] if study["attrs"].get("metric") == metric]
        if not options:
            continue
        matched.append(max(options, key=lambda study: (
            finished_trial_count(study.get("trial_counts")),
            -study.get("_recency", 0),
            study["attrs"].get("_optimizer_worker_config_version") == WORKER_CONFIG_VERSION,
        )))
    covered = {study["attrs"].get("metric") for study in matched}
    incompatible = [study for study in incompatible if study["attrs"].get("metric") not in covered]
    for study in source_studies:
        study.pop("_recency", None)
    return matched, incompatible, switched, fallback_snapshot


def resolve_study_plan(args, fixed_params, opt_params_def, risk_params, metrics, log_dir, requested_window):
    """优先匹配现有批次；未指定日期时沿用匹配窗口，否则使用本轮推断窗口。"""
    values = dict(vars(args), params=fixed_params, opt_params=opt_params_def, risk_params=risk_params)
    identity = study_identity(values)
    identity_json = json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str)
    requested_name = str(getattr(args, "study_name", None) or os.environ.get("QUANTADA_STUDY_NAME", "")).strip()
    requested_journal = str(getattr(args, "study_journal", None) or os.environ.get("QUANTADA_STUDY_JOURNAL", "")).strip()
    directory = Path(log_dir).resolve()
    selected_path = Path(requested_journal).resolve() if requested_journal else None
    if selected_path:
        paths = [selected_path] if selected_path.exists() else []
    elif requested_name:
        selected_path = directory / f"optuna_{requested_name}.log"
        paths = [selected_path] if selected_path.exists() else sorted(
            directory.glob("optuna_*.log"), key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True
        )
    else:
        paths = sorted(directory.glob("optuna_*.log"), key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    # 刷新表示用新行情创建独立实验，不沿用旧日期和试验；运行锁仍保护所选 Journal。
    if getattr(args, "refresh", False):
        paths = []
    if not identity["strategy"]:
        paths = []

    start_filter, end_filter = requested_window
    if getattr(args, "opt_schedule", None):
        start_filter, end_filter = args.start_date, args.end_date
    matched = []
    selected_window = None
    named_study = None
    for path in paths:
        try:
            studies = read_study_metadata(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if path == selected_path:
                raise ValueError(f"Cannot inspect requested Journal {path}: {exc}") from exc
            print(f"[Optimizer] Warning: skipping unreadable Journal {path.name}: {exc}")
            continue
        candidates = []
        anchor = next((study for study in studies if study["name"] == requested_name), None) if requested_name else None
        if anchor is not None:
            named_study = anchor
        anchored_snapshot = anchor["attrs"].get("_optimizer_data_snapshot") if anchor else None
        for study in reversed(studies):
            attrs = study["attrs"]
            metric = attrs.get("metric")
            if requested_name and study["name"] == requested_name and metric not in metrics and not anchored_snapshot:
                raise ValueError(f"Study {study['name']} belongs to a different metric")
            # 快照锚点只负责定位同批指标；指标不符不能进入候选，否则会把旧名称复用给新指标。
            if metric not in metrics:
                continue
            # 显式 Journal 内按已完成数选择，不能因快照或名称前缀漏掉已探索更多的 Study。
            if not requested_journal:
                if anchored_snapshot:
                    if attrs.get("_optimizer_data_snapshot") != anchored_snapshot:
                        continue
                elif requested_name and study["name"] != requested_name and not study["name"].startswith(f"{requested_name}__"):
                    continue
            # 旧记录至少要完整保存训练配置和日期，缺元数据时不猜测其身份。
            required = set(identity) - ({"symbols"} if identity["selection"] else set())
            compatible = (
                required.issubset(attrs)
                and study["directions"] == [2]
                and json.dumps(study_identity(attrs), sort_keys=True, ensure_ascii=False, default=str) == identity_json
            )
            start = str(attrs.get("start_date", ""))
            end = str(attrs.get("end_date", ""))
            compatible = compatible and start not in {"", "None"} and end not in {"", "None"}
            # 滚动窗口由 end 和周期决定；旧版本有时把预热起点回写到 start_date。
            if start_filter and not identity["train_roll_period"]:
                compatible = compatible and pd.Timestamp(start) == pd.Timestamp(str(start_filter))
            if end_filter:
                compatible = compatible and pd.Timestamp(end) == pd.Timestamp(str(end_filter))
            if compatible:
                candidates.append(study)
            elif requested_name and study["name"] == requested_name:
                raise ValueError(f"Study {study['name']} has a different training configuration or window")
        if candidates:
            selected_path = path
            selected_window = (candidates[0]["attrs"]["start_date"], candidates[0]["attrs"]["end_date"])
            matched = [
                study for study in candidates
                if study["attrs"]["end_date"] == selected_window[1]
                and (identity["train_roll_period"] or study["attrs"]["start_date"] == selected_window[0])
            ]
            for index, study in enumerate(matched):
                study["_recency"] = index
            break

    if selected_path is not None and selected_path.exists():
        anchor = batch_journal_path(selected_path)
        seen = {study["name"] for study in matched}
        for related in related_journal_paths(anchor if anchor.exists() else selected_path):
            if related.resolve() == Path(selected_path).resolve():
                continue
            try:
                sibling_studies = read_study_metadata(related)
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                continue
            for study in sibling_studies:
                if study["name"] in seen:
                    continue
                attrs = study["attrs"]
                if attrs.get("metric") not in metrics:
                    continue
                required = set(identity) - ({"symbols"} if identity["selection"] else set())
                compatible = (
                    required.issubset(attrs)
                    and study["directions"] == [2]
                    and json.dumps(study_identity(attrs), sort_keys=True, ensure_ascii=False, default=str) == identity_json
                )
                start = str(attrs.get("start_date", ""))
                end = str(attrs.get("end_date", ""))
                compatible = compatible and start not in {"", "None"} and end not in {"", "None"}
                if start_filter and not identity["train_roll_period"]:
                    compatible = compatible and pd.Timestamp(start) == pd.Timestamp(str(start_filter))
                if end_filter:
                    compatible = compatible and pd.Timestamp(end) == pd.Timestamp(str(end_filter))
                if selected_window:
                    compatible = compatible and end == str(selected_window[1])
                    compatible = compatible and (identity["train_roll_period"] or start == str(selected_window[0]))
                if not compatible:
                    continue
                matched.append(study)
                seen.add(study["name"])
        if anchor.exists() and anchor != Path(selected_path).resolve():
            selected_path = anchor
        counted = {}
        for related in related_journal_paths(selected_path):
            if not related.exists():
                continue
            try:
                counted.update({
                    item["name"]: item.get("trial_counts", {})
                    for item in read_study_metadata(related, with_trial_counts=True)
                })
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                continue
        for study in matched:
            if study["name"] in counted:
                study["trial_counts"] = counted[study["name"]]

    source_studies = matched
    matched, incompatible, switched, fallback_snapshot = select_resume_studies(source_studies, metrics, requested_name)
    if selected_window:
        args.start_date, args.end_date = selected_window
    digest = hashlib.sha256(json.dumps(
        {"identity": identity, "window": [str(pd.Timestamp(args.start_date)), str(pd.Timestamp(args.end_date))],
         "worker_config_version": WORKER_CONFIG_VERSION},
        sort_keys=True, ensure_ascii=False, default=str,
    ).encode("utf-8")).hexdigest()[:20]
    strategy_tag = re.sub(r"[^A-Za-z0-9_]+", "_", str(identity["strategy"] or "optimizer").split(".")[-1])[:40]
    base_name = requested_name or f"{strategy_tag}_{args.end_date}_{digest}"
    if selected_path is None:
        selected_path = directory / f"optuna_{base_name}.log"
    study_names = {}
    for metric in metrics:
        found = next((study for study in matched if study["attrs"]["metric"] == metric), None)
        if found:
            study_names[metric] = found["name"]
        elif requested_name and len(metrics) == 1:
            anchor_metric = str(named_study["attrs"].get("metric") or "") if named_study else ""
            if named_study is not None and anchor_metric not in metrics:
                raise ValueError(f"Study {named_study['name']} belongs to a different metric")
            study_names[metric] = requested_name
        else:
            suffix = re.sub(r"[^A-Za-z0-9_]+", "_", metric)[:36]
            metric_hash = hashlib.sha256(metric.encode("utf-8")).hexdigest()[:8]
            study_names[metric] = f"{base_name}__{suffix}_{metric_hash}"
    return {
        "journal": str(selected_path), "studies": study_names, "matched": matched, "source_studies": source_studies,
        "incompatible": incompatible, "reused_pre_config": any(
            study["attrs"].get("_optimizer_worker_config_version") != WORKER_CONFIG_VERSION for study in matched
        ),
        "switched_to_richer": switched, "fallback_snapshot": fallback_snapshot,
    }


def legacy_owner_running(study_name, workers_only=False):
    """查询旧名称或新所有者记录；新命令由进程锁管父进程，本函数补查 Windows 遗留 worker。"""
    match = re.search(r"_RUN(\d{8}-\d{6})_(\d+)$", study_name)
    if not match:
        return False
    pid = int(match.group(2))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("pid", wintypes.DWORD),
                ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                ("exe", wintypes.WCHAR * 260),
            ]

        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        pids = [pid]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot != ctypes.c_void_p(-1).value:
            try:
                entry = ProcessEntry()
                entry.dwSize = ctypes.sizeof(entry)
                found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
                while found:
                    if entry.parent == pid and entry.exe.lower().startswith("python"):
                        pids.append(entry.pid)
                    found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            finally:
                kernel.CloseHandle(snapshot)
        study_time = datetime.datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").timestamp()
        for candidate in pids:
            handle = kernel.OpenProcess(0x1000, False, candidate)
            if not handle:
                if ctypes.get_last_error() == 5:
                    return True
                continue
            try:
                code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                    continue
                stamps = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in stamps)):
                    return True
                created = ((stamps[0].dwHighDateTime << 32) + stamps[0].dwLowDateTime) / 10**7 - 11644473600
                if candidate == pid:
                    # PID 被新进程复用时，不把新进程及其后代当成旧任务。
                    if created > study_time + 1:
                        return False
                    if not workers_only:
                        return True
                    continue
                if created >= study_time - 1:
                    return True
            finally:
                kernel.CloseHandle(handle)
        return False
    if workers_only:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextmanager
def study_run_lock(journal_path):
    """非阻塞进程锁；退出或崩溃由操作系统释放，不删除锁文件以避免文件替换竞态。"""
    path = Path(str(journal_path) + ".optimizer.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                # 锁冲突才表示已有训练；其它 IO 错误必须抛出，不能静默跳过。
                if exc.errno not in {13, 36} and getattr(exc, "winerror", None) not in {33}:
                    raise
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        try:
            yield True
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
