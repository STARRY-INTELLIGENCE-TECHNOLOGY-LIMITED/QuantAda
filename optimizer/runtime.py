"""
QuantAda Heuristic Parallel Bayesian Optimizer
-------------------------------------------------------------------
Copyright (c) 2026 Starry Intelligence Technology Limited. All rights reserved.

This module implements the Entropy-Based Computational Budgeting and
Mix-Score evaluation mechanism described in our IEEE Access research.

Author: Xingchen Lin (ceo@starryint.hk)
Grant: SIT-2026-Q1
-------------------------------------------------------------------
QuantAda 启发式并行贝叶斯优化器
===============================

基于 TPE (Tree-structured Parzen Estimator) 算法的高性能参数寻优框架，
专为解决非凸、高维的金融时间序列参数优化问题而设计。

核心特性：
1. **贝叶斯内核**：利用 TPE 算法建模目标函数的后验概率分布，高效定位高潜参数区域。
2. **启发式算力评估**：基于参数空间复杂度（熵）与硬件算力（CPU核数），
   通过非线性公式动态估算最佳尝试次数 ($N_{trials}$)，拒绝盲目穷举。
3. **随机并发探索**：引入 `Constant-Liar` 采样策略与哈希去重机制，
   解决多核环境下的"并发踩踏"问题，模拟退火特性以有效跳出局部最优陷阱。
4. **工程鲁棒性**：内置跨平台文件锁管理、异常自动降级及全自动环境清理机制。
5. **动态滚动训练**：支持基于时间周期的自动滚动切分 (Walk-Forward)，自动推断训练/测试窗口。
"""

import ast
import copy
import datetime
import gc
import importlib
import logging
import math
import multiprocessing as mp
import os
import re
import socket
import sys
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import shared_memory

import numpy as np
import optuna
import optuna.visualization as vis
import pandas as pd
from optuna.samplers import TPESampler

import config
from backtest.backtester import Backtester
from common.formatters import format_float, format_recent_backtest_metrics
from common.indicator_cache import BoundedIndicatorCache
from common.loader import get_class_from_name, parse_period_string
from common import process_elevation
from common.terminal_log import (
    build_optimizer_terminal_log_path,
    get_optimizer_terminal_log_path,
    install_optimizer_terminal_log,
    set_optimizer_terminal_log_path,
)
from data_providers.manager import DataManager
from optimizer.reporting import (
    collect_dashboard_logs,
    normalize_metric_date,
    print_optimizer_ai_summary,
    print_run_summary as print_optimizer_run_summary,
)

try:
    from optuna.storages import JournalStorage
    try:
        # Optuna 4.0+ 新版路径
        from optuna.storages.journal import JournalFileBackend
        # 给它起个通用的别名
        JournalFileBackendCls = JournalFileBackend
    except ImportError:
        # 旧版路径 (兼容老环境)
        from optuna.storages import JournalFileStorage
        JournalFileBackendCls = JournalFileStorage
    HAS_JOURNAL = True
except ImportError:
    HAS_JOURNAL = False

try:
    from optuna_dashboard import run_server
    HAS_DASHBOARD = True
except ImportError:
    HAS_DASHBOARD = False

# 以 metric_arg 为键缓存函数指针，避免多指标串用同一个函数。
# key 格式: "{default_pkg}:{metric_arg}"
_METRIC_FUNC_CACHE = {}
_FORK_SHARED_WORKER_PAYLOAD = None


def get_metric_function(metric_arg, default_pkg="metrics"):
    """
    获取指标方法路由：支持绝对路径反射与缺省降级。

    支持格式：
    1. 绝对路径模式: --metric a_share.turbo_assault
       -> 加载根目录 a_share 包下的 turbo_assault 模块中的 turbo_assault / evaluate 函数
    2. 深度路径模式: --metric my_private.scores.v1.assault
       -> 加载 my_private/scores/v1 包下的 assault 模块
    3. 极简缺省模式: --metric turbo_assault (没有点号)
       -> 降级加载 default_pkg (默认 metrics) 下的 turbo_assault.py
    """
    global _METRIC_FUNC_CACHE

    metric_arg = (metric_arg or "").strip()
    cache_key = f"{default_pkg}:{metric_arg}"

    if cache_key in _METRIC_FUNC_CACHE:
        return _METRIC_FUNC_CACHE[cache_key]

    try:
        # 1. 路径解析解析 (路由分离)
        if '.' in metric_arg:
            # 存在点号，说明用户传入了具体的包路径。从最右侧切分一次。
            # 例如 "a_share.turbo_assault" -> module_path="a_share", func_name="turbo_assault"
            # 例如 "my.private.pkg.score_func" -> module_path="my.private.pkg", func_name="score_func"
            module_path, func_name = metric_arg.rsplit('.', 1)
        else:
            # 没有点号，触发极简模式，回退到默认的 metrics 包
            module_path = f"{default_pkg}.{metric_arg}"
            func_name = metric_arg

        # 2. O(1) 绝对寻址导入
        module = importlib.import_module(module_path)

        # 3. 提取执行函数 (支持同名函数或 evaluate 语法糖)
        if hasattr(module, func_name):
            metric_func = getattr(module, func_name)
        elif hasattr(module, "evaluate"):
            metric_func = getattr(module, "evaluate")
        else:
            raise AttributeError(f"模块 '{module_path}' 已加载，但找不到名为 '{func_name}' 或 'evaluate' 的打分函数。")

        _METRIC_FUNC_CACHE[cache_key] = metric_func
        return metric_func

    except ModuleNotFoundError as e:
        raise ValueError(f"[致命错误] 指标寻址失败，请放入metrics包中或pkg.fun格式调用私有指标。传入参数: '{metric_arg}'。Python底层报错: {e}")

def is_port_in_use(port):
    """检查本地端口是否被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def _infer_optimizer_terminal_log_ranges(args):
    start_date = getattr(args, "start_date", None)
    end_date = getattr(args, "end_date", None) or pd.Timestamp.now().strftime("%Y%m%d")

    try:
        if getattr(args, "train_period", None) and getattr(args, "test_period", None):
            tr_s, tr_e = str(args.train_period).split("-", 1)
            te_s, te_e = str(args.test_period).split("-", 1)
            return (tr_s, tr_e), (te_s, te_e)

        if getattr(args, "train_roll_period", None):
            anchor_dt = pd.to_datetime(str(end_date))
            test_roll = getattr(args, "test_roll_period", None)
            if test_roll:
                test_offset = parse_period_string(test_roll)
                if test_offset is None:
                    raise ValueError(f"Invalid test_roll_period: {test_roll}")
                split_dt = anchor_dt - test_offset
                train_end_dt = split_dt - pd.DateOffset(days=1)
                test_range = (split_dt.strftime("%Y%m%d"), anchor_dt.strftime("%Y%m%d"))
            else:
                split_dt = anchor_dt
                train_end_dt = split_dt
                test_range = (None, None)

            train_offset = parse_period_string(args.train_roll_period)
            if train_offset is None:
                raise ValueError(f"Invalid train_roll_period: {args.train_roll_period}")
            train_start_dt = split_dt - train_offset
            train_range = (train_start_dt.strftime("%Y%m%d"), train_end_dt.strftime("%Y%m%d"))
            return train_range, test_range
    except Exception:
        pass

    return (start_date, end_date), (None, None)


def _build_optimizer_terminal_log_path_for_args(args, symbol_list, run_dt=None, run_pid=None):
    train_range, test_range = _infer_optimizer_terminal_log_ranges(args)
    name_symbols = None if getattr(args, "selection", None) else symbol_list
    name_tag = OptimizationJob.build_optuna_name_tag(
        metric=getattr(args, "metric", "metric"),
        train_period=getattr(args, "train_roll_period", None),
        test_period=getattr(args, "test_roll_period", None),
        train_range=train_range,
        test_range=test_range,
        data_source=getattr(args, "data_source", None),
        symbols=name_symbols,
        selection=getattr(args, "selection", None),
        run_dt=run_dt,
        run_pid=run_pid,
    )
    return build_optimizer_terminal_log_path(name_tag)


def run_optimizer_mode(args, fixed_params, risk_params, symbol_list):
    terminal_log_path = get_optimizer_terminal_log_path()
    if not terminal_log_path:
        terminal_log_path = _build_optimizer_terminal_log_path_for_args(args, symbol_list)
        set_optimizer_terminal_log_path(terminal_log_path)

    terminal_tee = install_optimizer_terminal_log(terminal_log_path)
    try:
        return _run_optimizer_mode_impl(
            args=args,
            fixed_params=fixed_params,
            risk_params=risk_params,
            symbol_list=symbol_list,
        )
    except Exception:
        print("\n[Optimizer] Fatal exception captured in optimizer mode:")
        traceback.print_exc()
        return 1
    finally:
        terminal_tee.close()


def _run_optimizer_mode_impl(args, fixed_params, risk_params, symbol_list):
    """
    运行优化模式主流程（从 run.py 下沉的编排逻辑）。

    Args:
        args: argparse 解析结果。
        fixed_params: 策略固定参数（由 --params 解析）。
        risk_params: 风控参数（由 --risk_params 解析）。
        symbol_list: CLI symbols 列表（用于共享上下文兜底）。

    Returns:
        int: 进程退出码（0=成功；1=输入/初始化错误）。
    """
    print(f"\n>>> Mode: PARAMETER OPTIMIZATION (Target: {args.metric}) <<<")

    # 1. 解析传入的 metric (支持单个或逗号分隔的多个)
    # 自动过滤空字符串，避免出现如 "sharpe,,calmar," 的脏输入
    metrics_list = [m.strip() for m in args.metric.split(',') if m.strip()]
    if not metrics_list:
        print("Error: --metric contains no valid metric after filtering empty entries.")
        return 1

    resolve_worker_count = getattr(OptimizationJob, "_resolve_worker_count", lambda _requested_jobs: 1)
    if process_elevation.request_optimizer_elevation_if_needed(args, resolve_worker_count):
        return 0

    config.LOG = False
    logging.getLogger("optuna").setLevel(logging.INFO)

    try:
        opt_p_def = ast.literal_eval(args.opt_params)
    except Exception as e:
        print(f"Error parsing opt_params JSON: {e}")
        return 1

    final_reports = []
    total_metrics = len(metrics_list)
    is_multi_metric = total_metrics > 1
    explicit_params_passed = any(
        (arg == '--params') or arg.startswith('--params=')
        for arg in sys.argv[1:]
    )
    baseline_report = None
    baseline_test_report = None
    baseline_yearly_reports = None
    baseline_elapsed_hours = None
    shared_context = None
    bootstrap_job = None
    dashboard_launcher_job = None
    shared_dashboard_log_file = None
    log_dir = None
    test_set_requested = bool(getattr(args, "test_period", None) or getattr(args, "test_roll_period", None))

    def print_run_summary():
        print_optimizer_run_summary(
            final_reports=final_reports,
            total_metrics=total_metrics,
            explicit_params_passed=explicit_params_passed,
            baseline_report=baseline_report,
            baseline_test_report=baseline_test_report,
            baseline_yearly_reports=baseline_yearly_reports,
        )

    if is_multi_metric:
        log_dir = os.path.join(os.getcwd(), config.DATA_PATH, 'optuna')
        os.makedirs(log_dir, exist_ok=True)

    def build_shared_optuna_log_file(train_range, test_range, symbols):
        if not log_dir:
            return None

        name_tag = OptimizationJob.build_optuna_name_tag(
            metric=args.metric,
            train_period=args.train_roll_period,
            test_period=args.test_roll_period,
            train_range=train_range,
            test_range=test_range,
            data_source=args.data_source,
            symbols=symbols,
            selection=args.selection,
            run_dt=datetime.datetime.now(),
            run_pid=os.getpid(),
        )
        return os.path.join(log_dir, f"optuna_{name_tag}.log")

    # 先构建一次共享上下文，确保基准与多指标训练处于同一数据宇宙（同一选股与同一数据切分）
    try:
        bootstrap_args = copy.deepcopy(args)
        bootstrap_args.metric = metrics_list[0]
        bootstrap_args.auto_launch_dashboard = not is_multi_metric
        bootstrap_job = OptimizationJob(
            args=bootstrap_args,
            fixed_params=fixed_params,
            opt_params_def=opt_p_def,
            risk_params=risk_params
        )
        shared_context = bootstrap_job.export_shared_context()
        dashboard_launcher_job = bootstrap_job
        if is_multi_metric:
            shared_dashboard_log_file = build_shared_optuna_log_file(
                train_range=bootstrap_job.train_range,
                test_range=bootstrap_job.test_range,
                symbols=bootstrap_job.target_symbols,
            )
    except Exception as e:
        print(f"[警告] 共享上下文构建失败，将降级为逐metric独立初始化: {e}")
        if is_multi_metric and not shared_dashboard_log_file:
            fallback_train_range = (args.start_date, args.end_date)
            fallback_test_range = (args.end_date, args.end_date) if args.test_roll_period else (None, None)
            shared_dashboard_log_file = build_shared_optuna_log_file(
                train_range=fallback_train_range,
                test_range=fallback_test_range,
                symbols=symbol_list,
            )

    if explicit_params_passed:
        print("\n--- Running Baseline Backtest from --params (MainEval) ---")
        baseline_start = time.time()
        try:
            if bootstrap_job is not None:
                if test_set_requested:
                    baseline_test_report = bootstrap_job._run_test_set_backtest(copy.deepcopy(fixed_params), verbose=False)
                baseline_report = bootstrap_job._run_main_eval_backtest(copy.deepcopy(fixed_params))
                baseline_yearly_reports = bootstrap_job._run_yearly_validation_backtests(copy.deepcopy(fixed_params))
            else:
                baseline_args = copy.deepcopy(args)
                baseline_args.metric = metrics_list[0]
                baseline_args.auto_launch_dashboard = not is_multi_metric
                if shared_dashboard_log_file:
                    baseline_args.shared_journal_log_file = shared_dashboard_log_file
                baseline_job = OptimizationJob(
                    args=baseline_args,
                    fixed_params=fixed_params,
                    opt_params_def=opt_p_def,
                    risk_params=risk_params
                )
                if test_set_requested:
                    baseline_test_report = baseline_job._run_test_set_backtest(copy.deepcopy(fixed_params), verbose=False)
                baseline_report = baseline_job._run_main_eval_backtest(copy.deepcopy(fixed_params))
                baseline_yearly_reports = baseline_job._run_yearly_validation_backtests(copy.deepcopy(fixed_params))
        except Exception as e:
            print(f"[警告] 当前基准回测失败: {e}")
        finally:
            baseline_elapsed_hours = (time.time() - baseline_start) / 3600.0

    for idx, current_metric in enumerate(metrics_list, 1):
        print(f"\n\n{'=' * 65}")
        print(f"[指标 {idx}/{total_metrics} 正在训练]: {current_metric}")
        print(f"{'=' * 65}")

        # 深拷贝 args，确保物理隔离
        current_args = copy.deepcopy(args)
        current_args.metric = current_metric
        current_args.auto_launch_dashboard = not is_multi_metric
        if shared_dashboard_log_file:
            current_args.shared_journal_log_file = shared_dashboard_log_file

        start_time = time.time()

        try:
            job_kwargs = {
                "args": current_args,
                "fixed_params": fixed_params,
                "opt_params_def": opt_p_def,
                "risk_params": risk_params,
            }
            if shared_context is not None:
                job_kwargs["shared_context"] = shared_context

            job = OptimizationJob(**job_kwargs)
            if dashboard_launcher_job is None:
                dashboard_launcher_job = job

            # 执行优化并接收返回的字典战报
            result_dict = job.run()
            elapsed_hours = (time.time() - start_time) / 3600.0

            if result_dict and isinstance(result_dict, dict):
                result_dict['metric_name'] = current_args.metric
                result_dict['elapsed_hours'] = elapsed_hours
                result_dict['study_db'] = getattr(current_args, 'study_name', 'N/A')
                final_reports.append(result_dict)

        except Exception as e:
            print(f"\n[致命错误] 指标 '{current_metric}' 训练崩溃: {e}")
            traceback.print_exc()
            print(">>> 引擎防宕机保护触发，强行切入下一个指标...")
            continue

    if final_reports or explicit_params_passed:
        test_section_title = None
        if test_set_requested:
            test_section_start = None
            test_section_end = None

            for r in final_reports:
                test_metrics = r.get('test_backtest') or {}
                test_section_start = normalize_metric_date(test_metrics.get('start_date'))
                test_section_end = normalize_metric_date(test_metrics.get('end_date'))
                if test_section_start and test_section_end:
                    break

            if not (test_section_start and test_section_end):
                test_section_start = normalize_metric_date((baseline_test_report or {}).get('start_date'))
                test_section_end = normalize_metric_date((baseline_test_report or {}).get('end_date'))

            if not (test_section_start and test_section_end) and bootstrap_job is not None:
                try:
                    tr = getattr(bootstrap_job, "test_range", (None, None))
                    test_section_start = normalize_metric_date((tr or (None, None))[0])
                    test_section_end = normalize_metric_date((tr or (None, None))[1])
                except Exception:
                    pass

            period_text = None
            if test_section_start and test_section_end:
                period_text = f"{test_section_start} -> {test_section_end}"
            elif getattr(args, "test_period", None):
                raw = str(args.test_period)
                if '-' in raw:
                    s, e = raw.split('-', 1)
                    period_text = f"{s} -> {e}"
                else:
                    period_text = raw
            elif getattr(args, "test_roll_period", None):
                period_text = f"{args.test_roll_period} Rolling Window"

            test_title = "测试集回测结果 (Out-of-Sample Test Set)"
            if period_text:
                test_title = f"{period_text} 测试集回测结果 (Out-of-Sample Test Set)"
            test_section_title = test_title

        print_optimizer_ai_summary(
            final_reports=final_reports,
            explicit_params_passed=explicit_params_passed,
            fixed_params=fixed_params,
            baseline_report=baseline_report,
            baseline_test_report=baseline_test_report,
            baseline_yearly_reports=baseline_yearly_reports,
            baseline_elapsed_hours=baseline_elapsed_hours,
            test_set_requested=test_set_requested,
            test_section_title=test_section_title,
        )

        if final_reports:
            print("请在 Dashboard 中回放并排查孤点: ")
            dashboard_logs = collect_dashboard_logs(final_reports)
            for log_file in dashboard_logs:
                print(f"optuna-dashboard {log_file}")

            print_run_summary()

            # 多 metric 场景只在末尾弹一次 Dashboard（共享 Journal 可聚合全部 metric）
            if is_multi_metric and dashboard_launcher_job and dashboard_logs:
                final_log = shared_dashboard_log_file or dashboard_logs[0]
                if os.path.exists(final_log):
                    base_port = getattr(config, 'OPTUNA_DASHBOARD_PORT', 8090)
                    target_port = base_port
                    for _ in range(100):
                        if not is_port_in_use(target_port):
                            break
                        target_port += 1
                    else:
                        print(f"[Warning] Could not find an available port starting from {base_port}.")
                        target_port = base_port
                    print(f"[Info] Multi-metric training completed. Launching aggregated dashboard: {final_log}")
                    print("[Info] Dashboard will run in foreground. Analyze results, then press Ctrl-C to exit.")
                    dashboard_launcher_job._launch_dashboard(final_log, port=target_port, background=False)
                else:
                    print(f"[Warning] Aggregated dashboard log file not found: {final_log}")
        else:
            print("[警告] 当前仅有基准回测结果，训练指标未返回结果。")
            print_run_summary()
    else:
        print("\n[警告] 所有指标均未返回结果")
        print_run_summary()
    return 0


class OptimizationJob:
    CN_EXCHANGE_PREFIXES = {"SHSE", "SZSE", "SH", "SZ"}
    HK_EXCHANGE_PREFIXES = {"SEHK", "HK"}
    US_EXCHANGE_PREFIXES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "ISLAND", "SMART", "PINK", "US"}
    DEFAULT_WARMUP_DAYS = 400
    OPTIMIZER_INDICATOR_CACHE_MAX_ENTRIES = 512
    TPE_DEFAULT_N_EI_CANDIDATES = 24
    TPE_PAYLOAD_COPY_MIN_N_EI_CANDIDATES = 8
    TPE_PAYLOAD_COPY_TRIAL_THRESHOLD = 10000

    def __init__(self, args, fixed_params, opt_params_def, risk_params, shared_context=None):
        self.args = args
        self.fixed_params = fixed_params
        self.opt_params_def = opt_params_def
        self.risk_params = risk_params
        self._reset_trial_dedupe_cache()
        self._indicator_cache = BoundedIndicatorCache(self.OPTIMIZER_INDICATOR_CACHE_MAX_ENTRIES)
        self.warmup_days = self.DEFAULT_WARMUP_DAYS

        # 共享上下文模式：复用选股、数据抓取与切分结果，确保多指标/基准对比在同一数据宇宙下进行
        if shared_context is not None:
            self.strategy_class = shared_context["strategy_class"]
            self.risk_control_classes = shared_context["risk_control_classes"]
            self.data_manager = shared_context["data_manager"]
            self.target_symbols = shared_context["target_symbols"]
            self.raw_datas = shared_context["raw_datas"]
            self.train_datas = shared_context["train_datas"]
            self.test_datas = shared_context["test_datas"]
            self.train_range = shared_context["train_range"]
            self.test_range = shared_context["test_range"]
            self.warmup_days = shared_context.get("warmup_days", self.warmup_days)
            self._window_data_cache = shared_context.get("window_data_cache", {})
            self._indicator_cache = shared_context.get("indicator_cache", self._indicator_cache)
            if not isinstance(self._indicator_cache, BoundedIndicatorCache):
                self._indicator_cache = BoundedIndicatorCache(self.OPTIMIZER_INDICATOR_CACHE_MAX_ENTRIES)
            self._raw_data_fetch_range = shared_context.get("raw_data_fetch_range", (None, None))

            # 在复用数据上下文的前提下，仅重建本次 metric 对应的 study_name
            self._auto_refine_study_name()
            return

        self.strategy_class = get_class_from_name(args.strategy, ['strategies'])
        self.risk_control_classes = []
        if args.risk:
            # 支持逗号分隔
            risk_names = args.risk.split(',')
            for r_name in risk_names:
                r_name = r_name.strip()
                if r_name:
                    cls = get_class_from_name(r_name, ['risk_controls', 'strategies'])
                    self.risk_control_classes.append(cls)

        self.data_manager = DataManager()

        # 选股逻辑。
        self.target_symbols = []
        if self.args.selection:
            print(f"\n--- Running Selection Phase: {self.args.selection} ---")
            try:
                selector_class = get_class_from_name(self.args.selection, ['stock_selectors', 'stock_selectors_custom'])
                selector_instance = selector_class(data_manager=self.data_manager)
                selection_result = selector_instance.run_selection()
                if isinstance(selection_result, list):
                    self.target_symbols = selection_result
                elif isinstance(selection_result, pd.DataFrame):
                    self.target_symbols = selection_result.index.tolist()
                print(f"  Selector returned {len(self.target_symbols)} symbols: {self.target_symbols}")
            except Exception as e:
                print(f"Error during selection execution: {e}")
                sys.exit(1)
        else:
            if self.args.symbols:
                self.target_symbols = [s.strip() for s in self.args.symbols.split(',')]

        if not self.target_symbols:
            print("\nError: No symbols found for optimization.")
            sys.exit(1)

        self.raw_datas = self._fetch_all_data()
        self.train_datas, self.test_datas, self.train_range, self.test_range = self._split_data()
        self._window_data_cache = {}
        if not hasattr(self, "_raw_data_fetch_range"):
            self._raw_data_fetch_range = (None, None)

        # 根据实际日期和市场类型自动精细化 study_name
        self._auto_refine_study_name()

    def export_shared_context(self):
        """
        导出可复用的优化上下文，供多指标串行任务复用，避免重复选股/拉数导致结果不可比。
        """
        return {
            "strategy_class": self.strategy_class,
            "risk_control_classes": self.risk_control_classes,
            "data_manager": self.data_manager,
            "target_symbols": self.target_symbols,
            "raw_datas": self.raw_datas,
            "train_datas": self.train_datas,
            "test_datas": self.test_datas,
            "train_range": self.train_range,
            "test_range": self.test_range,
            "warmup_days": self.warmup_days,
            "window_data_cache": self._window_data_cache,
            "indicator_cache": self._indicator_cache,
            "raw_data_fetch_range": self._raw_data_fetch_range,
        }

    def _reset_trial_dedupe_cache(self):
        """
        进程内结果缓存：参数哈希 -> 评分。
        仅用于避免同一 worker 重复评估相同参数。
        """
        self._completed_trial_cache = {}

    def _release_memory_pressure(self):
        cache = getattr(self, "_indicator_cache", None)
        if isinstance(cache, dict):
            cache.clear()
        dedupe_cache = getattr(self, "_completed_trial_cache", None)
        if isinstance(dedupe_cache, dict):
            dedupe_cache.clear()
        gc.collect()

    @classmethod
    def _resolve_spawn_tpe_n_ei_candidates(cls, n_trials, shared_memory_enabled):
        try:
            n_trials = int(n_trials)
        except (TypeError, ValueError):
            n_trials = 0

        if shared_memory_enabled or n_trials < cls.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD:
            return cls.TPE_DEFAULT_N_EI_CANDIDATES

        scale = max(1.0, n_trials / float(cls.TPE_PAYLOAD_COPY_TRIAL_THRESHOLD))
        candidates = int(round(cls.TPE_DEFAULT_N_EI_CANDIDATES / max(1.0, math.log2(scale) + 1.0)))
        return max(cls.TPE_PAYLOAD_COPY_MIN_N_EI_CANDIDATES, candidates)

    @staticmethod
    def _get_total_cpu_cores():
        return max(1, os.cpu_count() or 1)

    def _warmup_start_date(self, start_date):
        if not start_date:
            return start_date

        ts = pd.to_datetime(str(start_date)) - pd.DateOffset(days=self.warmup_days)
        text = str(start_date)
        return ts.strftime('%Y-%m-%d %H:%M:%S') if ':' in text else ts.strftime('%Y%m%d')

    @classmethod
    def _resolve_worker_count(cls, requested_jobs):
        """
        将 n_jobs 解析为实际 worker 数。
        规则：
        - n_jobs > 0: 指定 worker 数（上限为机器总核数）
        - n_jobs = -1: 自动保留系统冗余，workers = C - max(2, ceil(0.15 * C))
        - n_jobs < -1: joblib 风格，workers = C - (abs(n_jobs) - 1)
        - n_jobs = 0 或非法值: 降级为 1
        """
        total_cores = cls._get_total_cpu_cores()
        try:
            requested_jobs = int(requested_jobs)
        except (TypeError, ValueError):
            requested_jobs = 1

        if requested_jobs == -1:
            reserved_cores = max(2, math.ceil(total_cores * 0.15))
            return max(1, total_cores - reserved_cores)

        if requested_jobs < -1:
            reserved_cores = abs(requested_jobs) - 1
            return max(1, total_cores - reserved_cores)

        if requested_jobs == 0:
            return 1

        return max(1, min(total_cores, requested_jobs))

    def _build_worker_payload(self):
        """
        构造多进程 worker 所需的最小上下文，避免传输不必要对象。
        """
        return {
            "args": self.args,
            "fixed_params": self.fixed_params,
            "opt_params_def": self.opt_params_def,
            "risk_params": self.risk_params,
            "train_datas": self.train_datas,
            "train_range": self.train_range,
            "warmup_days": self.warmup_days,
            "terminal_log_path": get_optimizer_terminal_log_path(),
            # spawn 子进程不会继承主进程运行期改写的 config，显式透传日志开关。
            "log_enabled": bool(getattr(config, "LOG", False)),
        }

    @staticmethod
    def _create_shared_array(array_like):
        arr = np.ascontiguousarray(array_like)
        if arr.dtype == object:
            raise TypeError("object dtype is not supported in shared memory mode")

        alloc_size = max(1, int(arr.nbytes))
        shm = shared_memory.SharedMemory(create=True, size=alloc_size)
        shm_arr = np.ndarray(arr.shape, dtype=arr.dtype, buffer=shm.buf)
        if arr.size > 0:
            np.copyto(shm_arr, arr)

        meta = {
            "name": shm.name,
            "shape": arr.shape,
        }
        if arr.dtype.names:
            # structured dtype 需要保留字段描述，dtype.str 会丢失 field names
            meta["dtype_descr"] = arr.dtype.descr
        else:
            meta["dtype"] = arr.dtype.str

        return meta, shm

    @staticmethod
    def _attach_shared_array(meta):
        shm = shared_memory.SharedMemory(name=meta["name"])
        if "dtype_descr" in meta:
            dtype = np.dtype(meta["dtype_descr"])
        else:
            dtype = np.dtype(meta["dtype"])
        arr = np.ndarray(tuple(meta["shape"]), dtype=dtype, buffer=shm.buf)
        arr.setflags(write=False)
        return arr, shm

    @staticmethod
    def _cleanup_shared_segments(shm_handles, unlink=False):
        for shm in shm_handles:
            try:
                shm.close()
            except Exception:
                pass
            if unlink:
                try:
                    shm.unlink()
                except Exception:
                    pass

    @staticmethod
    def _force_shutdown_process_pool(executor, futures):
        """
        在 Ctrl-C 等中断场景下，尽快回收 ProcessPoolExecutor 及其子进程。
        """
        if executor is None:
            return

        for fut in futures or []:
            try:
                fut.cancel()
            except Exception:
                pass

        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        # 访问私有字段做兜底清理，避免 spawn 子进程在中断后继续占用 CPU。
        processes = getattr(executor, "_processes", None)
        if not processes:
            return

        for proc in list(processes.values()):
            try:
                if proc.is_alive():
                    proc.terminate()
            except Exception:
                pass

        deadline = time.time() + 2.0
        for proc in list(processes.values()):
            try:
                remain = max(0.0, deadline - time.time())
                proc.join(timeout=remain)
            except Exception:
                pass

        for proc in list(processes.values()):
            try:
                if proc.is_alive() and hasattr(proc, "kill"):
                    proc.kill()
            except Exception:
                pass

    def _build_spawn_shared_payload(self, worker_payload):
        """
        spawn 模式下将 train_datas 放入 shared_memory，避免为每个 worker 重复序列化大对象。
        """
        train_datas = worker_payload.get("train_datas") or {}
        if not train_datas:
            return worker_payload, []

        shm_handles = []
        shared_meta = {"symbols": {}}

        try:
            for symbol, df in train_datas.items():
                idx_meta, idx_shm = self._create_shared_array(df.index.to_numpy(copy=False))
                shm_handles.append(idx_shm)

                # 将所有列压成一个 structured array，只占用一个共享内存段
                columns_meta = []
                structured_fields = []
                col_arrays = []
                for i, col in enumerate(df.columns):
                    arr = np.ascontiguousarray(df[col].to_numpy(copy=False))
                    if arr.dtype == object:
                        raise TypeError("object dtype is not supported in shared memory mode")
                    field_name = f"f{i}"
                    structured_fields.append((field_name, arr.dtype))
                    col_arrays.append((field_name, arr))
                    columns_meta.append({
                        "name": col,
                        "field": field_name,
                    })

                records = np.empty(len(df), dtype=structured_fields)
                for field_name, arr in col_arrays:
                    records[field_name] = arr
                records_meta, records_shm = self._create_shared_array(records)
                shm_handles.append(records_shm)

                symbol_meta = {
                    "index": idx_meta,
                    "index_name": df.index.name,
                    "columns": columns_meta,
                    "records": records_meta,
                }

                shared_meta["symbols"][symbol] = symbol_meta

            shared_payload = dict(worker_payload)
            shared_payload["train_datas"] = None
            shared_payload["train_datas_shared"] = shared_meta
            return shared_payload, shm_handles
        except Exception:
            self._cleanup_shared_segments(shm_handles, unlink=True)
            return worker_payload, []

    @staticmethod
    def _restore_train_datas_from_shared(shared_meta):
        train_datas = {}
        shm_handles = []

        for symbol, symbol_meta in (shared_meta or {}).get("symbols", {}).items():
            idx_arr, idx_shm = OptimizationJob._attach_shared_array(symbol_meta["index"])
            shm_handles.append(idx_shm)
            index_obj = pd.Index(idx_arr, name=symbol_meta.get("index_name"))

            records_arr, records_shm = OptimizationJob._attach_shared_array(symbol_meta["records"])
            shm_handles.append(records_shm)

            data_dict = {}
            for col_spec in symbol_meta.get("columns", []):
                data_dict[col_spec["name"]] = records_arr[col_spec["field"]]

            train_datas[symbol] = pd.DataFrame(data_dict, index=index_obj, copy=False)

        return train_datas, shm_handles

    @classmethod
    def from_worker_payload(cls, payload):
        """
        在子进程内恢复可执行 objective 的最小 Job 实例。
        """
        obj = cls.__new__(cls)
        obj.args = payload["args"]
        obj.fixed_params = payload["fixed_params"]
        obj.opt_params_def = payload["opt_params_def"]
        obj.risk_params = payload["risk_params"]
        obj.train_datas = payload["train_datas"]
        obj.train_range = payload["train_range"]
        obj.warmup_days = payload.get("warmup_days", cls.DEFAULT_WARMUP_DAYS)
        obj._indicator_cache = BoundedIndicatorCache(cls.OPTIMIZER_INDICATOR_CACHE_MAX_ENTRIES)

        obj.strategy_class = get_class_from_name(obj.args.strategy, ['strategies'])
        obj.risk_control_classes = []
        if obj.args.risk:
            for r_name in obj.args.risk.split(','):
                r_name = r_name.strip()
                if r_name:
                    rc_cls = get_class_from_name(r_name, ['risk_controls', 'strategies'])
                    obj.risk_control_classes.append(rc_cls)

        obj._reset_trial_dedupe_cache()
        return obj

    @staticmethod
    def _normalize_param_value(value):
        if isinstance(value, float):
            # 限制浮点抖动，保证哈希稳定
            return round(value, 12)
        if isinstance(value, list):
            return tuple(OptimizationJob._normalize_param_value(v) for v in value)
        if isinstance(value, dict):
            return tuple(sorted((k, OptimizationJob._normalize_param_value(v)) for k, v in value.items()))
        return value

    def _params_to_key(self, params_dict):
        return tuple(sorted((k, self._normalize_param_value(v)) for k, v in params_dict.items()))

    def _get_cached_trial_value(self, params_key):
        return self._completed_trial_cache.get(params_key)

    def _cache_completed_trial(self, params_key, score):
        try:
            score_val = float(score)
        except Exception:
            return

        if math.isnan(score_val) or math.isinf(score_val):
            return

        self._completed_trial_cache[params_key] = score_val

    def _run_multiprocess_optimization(
        self,
        n_jobs,
        n_trials,
        log_file,
        prefer_fork_cow=False,
    ):
        if not log_file:
            raise RuntimeError("Multi-process mode requires a shared JournalStorage log file.")
        if int(n_trials) <= 0:
            return

        worker_count = self._resolve_worker_count(n_jobs)
        worker_count = min(worker_count, max(1, int(n_trials)))

        base = n_trials // worker_count
        rem = n_trials % worker_count
        worker_trials = [base + (1 if i < rem else 0) for i in range(worker_count)]
        worker_trials = [x for x in worker_trials if x > 0]
        if not worker_trials:
            return

        start_method = "spawn"
        if prefer_fork_cow and sys.platform.startswith("linux"):
            try:
                mp.get_context("fork")
                start_method = "fork"
            except ValueError:
                start_method = "spawn"

        print(f"[Optimizer] Multi-process mode: launching {len(worker_trials)} workers ({start_method}).")

        worker_payload = self._build_worker_payload()
        payload_arg = worker_payload
        shared_parent_handles = []
        seed_base = int(time.time() * 1_000_000) % (2 ** 31 - 1)
        futures = []
        ctx = mp.get_context(start_method)

        global _FORK_SHARED_WORKER_PAYLOAD
        if start_method == "fork":
            # fork 模式下，子进程会继承父进程内存页（Copy-on-Write），
            # 避免将大体量 train_datas 再序列化传输给每个 worker。
            _FORK_SHARED_WORKER_PAYLOAD = worker_payload
            payload_arg = None
        else:
            payload_arg, shared_parent_handles = self._build_spawn_shared_payload(worker_payload)
            if shared_parent_handles:
                print("[Optimizer] Spawn mode: train_datas shared via multiprocessing.shared_memory.")
            else:
                print(
                    "[Optimizer] Spawn mode: shared_memory unavailable; "
                    "falling back to payload copy so training can continue."
                )

        spawn_shared_memory_enabled = start_method != "spawn" or bool(shared_parent_handles)
        tpe_n_ei_candidates = self._resolve_spawn_tpe_n_ei_candidates(
            n_trials,
            shared_memory_enabled=spawn_shared_memory_enabled,
        )
        if tpe_n_ei_candidates != self.TPE_DEFAULT_N_EI_CANDIDATES:
            print(
                "[Optimizer] Payload-copy memory guard enabled: "
                f"n_ei_candidates={tpe_n_ei_candidates} "
                f"(n_trials={n_trials}, default={self.TPE_DEFAULT_N_EI_CANDIDATES})"
            )

        executor = None
        interrupted = False
        stopped_early = False
        try:
            executor = ProcessPoolExecutor(max_workers=len(worker_trials), mp_context=ctx)
            for worker_idx, local_trials in enumerate(worker_trials, start=1):
                futures.append(
                    executor.submit(
                        _optimize_worker_entry,
                        payload_arg,
                        self.args.study_name,
                        log_file,
                        local_trials,
                        worker_idx,
                        seed_base + worker_idx,
                        tpe_n_ei_candidates,
                    )
                )

            for fut in as_completed(futures):
                result = fut.result()
                if isinstance(result, dict) and result.get("stopped_early"):
                    stopped_early = True
                    print(
                        f"[Optimizer] Worker {result.get('worker_idx')} reported memory pressure; "
                        "stopping remaining workers for this metric."
                    )
                    self._force_shutdown_process_pool(executor, futures)
                    print("[Optimizer] Completed trials will be used for the final report if available.")
                    break
        except KeyboardInterrupt:
            interrupted = True
            print("\n[Optimizer] Ctrl-C detected. Forcing worker shutdown...")
            self._force_shutdown_process_pool(executor, futures)
            raise
        except MemoryError as exc:
            stopped_early = True
            print(f"\n[Optimizer] Multi-process optimization stopped due to memory pressure: {exc}")
            self._force_shutdown_process_pool(executor, futures)
            print("[Optimizer] Completed trials will be used for the final report if available.")
            return
        except BrokenProcessPool as exc:
            stopped_early = True
            print(f"\n[Optimizer] Worker process exited unexpectedly: {exc}")
            print("[Optimizer] Treating this as recoverable worker pressure for the current metric.")
            self._force_shutdown_process_pool(executor, futures)
            print("[Optimizer] Completed trials will be used for the final report if available.")
            return
        finally:
            if executor is not None and not interrupted and not stopped_early:
                executor.shutdown(wait=True, cancel_futures=False)
            if start_method == "fork":
                _FORK_SHARED_WORKER_PAYLOAD = None
            self._cleanup_shared_segments(shared_parent_handles, unlink=True)

    def _fetch_all_data(self):
        print("\n--- Fetching Data for Optimization ---")

        # 1. 锚点初始化 (Anchor Point: Test End)
        req_end = self.args.end_date
        if not req_end:
            req_end = pd.Timestamp.now().strftime('%Y%m%d')
            self.args.end_date = req_end  # 回写

        req_start = self.args.start_date

        # 2. 动态周期计算 (支持 Train Roll + Test Roll)
        if getattr(self.args, 'train_roll_period', None):

            # A. 计算测试集长度
            test_duration = pd.Timedelta(0)
            if getattr(self.args, 'test_roll_period', None):
                offset_test = parse_period_string(self.args.test_roll_period)
                if offset_test:
                    test_duration = offset_test

            # B. 计算训练集长度
            train_duration = parse_period_string(self.args.train_roll_period)

            # C. 计算总回溯起点
            if train_duration:
                anchor_dt = pd.to_datetime(str(req_end))

                # 依次扣除：测试期 -> 训练期 -> 指标预热期
                fetch_start_dt = anchor_dt - test_duration - train_duration - pd.DateOffset(days=self.warmup_days)

                req_start = fetch_start_dt.strftime('%Y%m%d')

                # 回写 start_date
                self.args.start_date = req_start

                print(f"[Auto-Fetch] Dynamic Rolling Detected:")
                print(f"  Train Roll: {self.args.train_roll_period}")
                print(f"  Test Roll:  {getattr(self.args, 'test_roll_period', 'None (Refit Mode)')}")
                print(f"  Warm-up:    {self.warmup_days} calendar days")
                print(f"  => Fetching data from {req_start} to {req_end}")

        elif getattr(self.args, 'train_period', None) and getattr(self.args, 'test_period', None):
            tr_s, _ = self.args.train_period.split('-')
            te_s, _ = self.args.test_period.split('-')
            req_start = self._warmup_start_date(min(tr_s, te_s))
            print(f"[Auto-Fetch] Explicit Split Detected:")
            print(f"  Warm-up:    {self.warmup_days} calendar days")
            print(f"  => Fetching data from {req_start} to {req_end}")

        elif getattr(self.args, 'train_ratio', None) is None and req_start:
            req_start = self._warmup_start_date(req_start)
            print(f"[Auto-Fetch] Full Window Detected:")
            print(f"  Warm-up:    {self.warmup_days} calendar days")
            print(f"  => Fetching data from {req_start} to {req_end}")

        # 3. 统一抓取窗口：训练需求 vs MainEval 需求取更早起点，确保后续多指标/基准完全可比
        req_fetch_start = req_start
        recent_start, _ = self._infer_recent_3y_window()
        if recent_start:
            recent_fetch_start = self._warmup_start_date(recent_start)
            if not req_fetch_start or pd.to_datetime(recent_fetch_start) < pd.to_datetime(req_fetch_start):
                req_fetch_start = recent_fetch_start
                print(f"[Auto-Fetch] Extended fetch window for MainEval consistency:")
                print(f"  MainEval Minimum Warm-up Start: {recent_fetch_start}")
                print(f"  => Fetching data from {req_fetch_start} to {req_end}")

        self._raw_data_fetch_range = (req_fetch_start, req_end)

        datas = {}
        for symbol in self.target_symbols:
            # 优先使用缓存
            df = self.data_manager.get_data(
                symbol,
                start_date=req_fetch_start,
                end_date=req_end,
                specified_sources=self.args.data_source,
                timeframe=self.args.timeframe,
                compression=self.args.compression,
                refresh=self.args.refresh
            )
            if df is not None and not df.empty:
                datas[symbol] = df
            else:
                print(f"Warning: No data for {symbol}, skipping.")

        if not datas:
            raise ValueError("No data fetched. Check symbols, selection or date range.")
        return datas

    def _split_data(self):
        # 1. 显式指定模式 (最高优先级)
        if self.args.train_period and self.args.test_period:
            tr_s, tr_e = self.args.train_period.split('-')
            te_s, te_e = self.args.test_period.split('-')

            print(f"Split Mode: Explicit Period")
            print(f"  Train: {tr_s} -> {tr_e}")
            print(f"  Test:  {te_s} -> {te_e}")

            train_d = self.slice_datas(tr_s, tr_e)
            test_d = self.slice_datas(te_s, te_e)
            return train_d, test_d, (tr_s, tr_e), (te_s, te_e)

        # 2. 动态滚动训练模式 (Dynamic Rolling)
        elif getattr(self.args, 'train_roll_period', None):
            train_roll = self.args.train_roll_period
            test_roll = getattr(self.args, 'test_roll_period', None)

            print(f"Split Mode: Dynamic Rolling")

            # A. 确定时间锚点 (Anchor: Test End)
            # self.args.end_date 已经在 _fetch_all_data 中补全
            anchor_dt = pd.to_datetime(str(self.args.end_date))

            # B. 计算切分点
            if test_roll:
                # 有测试集：Split Point = End - Test Roll
                test_offset = parse_period_string(test_roll)
                split_dt = anchor_dt - test_offset
                # 防止训练集与测试集在 split_dt 当日重叠（slice 是闭区间）
                train_end_dt = split_dt - pd.DateOffset(days=1)
            else:
                # 无测试集 (Refit模式)：Split Point = End
                split_dt = anchor_dt
                train_end_dt = split_dt

            # 训练开始 = Split Point - Train Roll。
            train_offset = parse_period_string(train_roll)
            train_start_dt = split_dt - train_offset

            tr_s = train_start_dt.strftime('%Y%m%d')
            tr_e = train_end_dt.strftime('%Y%m%d')
            te_s = split_dt.strftime('%Y%m%d') if test_roll else None
            te_e = anchor_dt.strftime('%Y%m%d') if test_roll else None

            print(f"  [Auto-Inferred] Train Set: {tr_s} -> {tr_e} ({train_roll})")

            if test_roll:
                print(f"  [Auto-Inferred] Test Set:  {te_s} -> {te_e} ({test_roll})")
                test_d = self.slice_datas(te_s, te_e)
            else:
                print(f"  [Auto-Inferred] Test Set:  (Skipped / Production Refit Mode)")
                test_d = {}  # 空测试集

            train_d = self.slice_datas(tr_s, tr_e)

            return train_d, test_d, (tr_s, tr_e), (te_s, te_e)

        # 3. 比例切分模式
        elif self.args.train_ratio is not None:
            ratio = float(self.args.train_ratio)
            if not (0 < ratio < 1):
                raise ValueError(f"train_ratio must be between 0 and 1 (exclusive), got: {ratio}")
            print(f"Split Mode: Ratio ({ratio * 100}% Train)")

            all_dates = sorted(list(set().union(*[self.prepare_data_index(df).index for df in self.raw_datas.values()])))
            if not all_dates:
                raise ValueError("Data has no valid dates.")
            if len(all_dates) < 2:
                raise ValueError("Need at least 2 timestamps to split train/test.")

            # 采用半开区间切分语义：[0, split_idx) 为训练，[split_idx, n) 为测试
            split_idx = int(len(all_dates) * ratio)
            split_idx = min(max(split_idx, 1), len(all_dates) - 1)

            train_end_date = all_dates[split_idx - 1]
            test_start_date = all_dates[split_idx]

            start_date_str = all_dates[0].strftime('%Y%m%d')
            split_date_str = train_end_date.strftime('%Y%m%d')
            test_start_str = test_start_date.strftime('%Y%m%d')
            end_date_str = all_dates[-1].strftime('%Y%m%d')

            print(f"  Train End: {split_date_str}")
            print(f"  Test Start: {test_start_str}")

            train_d = self.slice_datas(start_date_str, split_date_str)
            test_d = self.slice_datas(test_start_str, end_date_str)
            return train_d, test_d, (start_date_str, split_date_str), (test_start_str, end_date_str)

        # 4. 全量模式 (无测试集)
        else:
            print("Warning: No split method defined. Running optimization on FULL dataset.")
            return self.raw_datas, {}, (self.args.start_date, self.args.end_date), (None, None)

    @staticmethod
    def _sanitize_name_token(value, default="NA", max_len=48):
        text = str(value or "").strip()
        text = text.replace(".", "_").replace("-", "_")
        text = re.sub(r"[^0-9A-Za-z_]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        if not text:
            text = default
        return text[:max_len]

    @classmethod
    def _normalize_date_tag(cls, value, default="NA"):
        if value is None:
            return default
        text = str(value).strip()
        digits = re.sub(r"[^0-9]", "", text)
        if len(digits) >= 8:
            return digits[:8]
        return cls._sanitize_name_token(text, default=default, max_len=12)

    @classmethod
    def infer_market_label(cls, symbols=None, data_source=None, selection=None):
        prefixes = set()
        symbol_list = symbols or []

        for sym in symbol_list:
            raw = str(sym or "").strip().upper()
            if not raw:
                continue

            # 常见交易所前缀格式: SHSE.510300 / NASDAQ.AAPL / SEHK.700
            if "." in raw:
                prefix = raw.split(".", 1)[0]
                if prefix:
                    prefixes.add(prefix)
                continue

            # 无前缀代码交给 data_source 做二次推断
            if raw.endswith(".SS"):
                prefixes.add("SH")
            elif raw.endswith(".SZ"):
                prefixes.add("SZ")
            elif raw.endswith(".HK"):
                prefixes.add("HK")
            elif raw.endswith("USDT") or raw.endswith("USD"):
                prefixes.add("CRYPTO")
            else:
                prefixes.add("RAW")

        if prefixes:
            if prefixes.issubset(cls.CN_EXCHANGE_PREFIXES):
                return "CN"
            if prefixes.issubset(cls.HK_EXCHANGE_PREFIXES):
                return "HK"
            if "CRYPTO" in prefixes:
                return "CRYPTO"
            if prefixes.issubset(cls.US_EXCHANGE_PREFIXES):
                return "US"
            if prefixes == {"RAW"}:
                prefixes = set()
            elif len(prefixes) == 1:
                return cls._sanitize_name_token(next(iter(prefixes)), default="MKT", max_len=12)
            else:
                return f"MIX{len(prefixes)}"

        source_hint = str(data_source or "").split(",")[0].strip().lower()
        if source_hint:
            market_by_source = {
                "tushare": "CN",
                "sxsc_tushare": "CN",
                "akshare": "CN",
                "gm": "CN",
                "ibkr": "US",
                "tiingo": "US",
                "yf": "GLOBAL",
                "csv": "LOCAL",
            }
            return market_by_source.get(
                source_hint,
                cls._sanitize_name_token(source_hint.upper(), default="MKT", max_len=12),
            )

        if selection:
            return cls._sanitize_name_token(selection, default="SEL", max_len=16)

        return "MKT"

    @classmethod
    def build_optuna_name_tag(
            cls,
            metric,
            train_period,
            test_period,
            train_range,
            test_range,
            data_source=None,
            symbols=None,
            selection=None,
            run_dt=None,
            run_pid=None,
    ):
        train_period_tag = cls._sanitize_name_token(
            str(train_period or "ALL").upper(),
            default="ALL",
            max_len=16,
        )
        test_period_raw = str(test_period).upper() if test_period else "REFIT"
        test_period_tag = cls._sanitize_name_token(test_period_raw, default="REFIT", max_len=16)
        metric_tag = cls._sanitize_name_token(metric, default="metric", max_len=36)
        market_tag = cls.infer_market_label(symbols=symbols, data_source=data_source, selection=selection)

        tr_s = cls._normalize_date_tag((train_range or (None, None))[0], default="NA")
        tr_e = cls._normalize_date_tag((train_range or (None, None))[1], default="NA")

        te_s_raw = (test_range or (None, None))[0]
        te_e_raw = (test_range or (None, None))[1]
        if te_s_raw and te_e_raw:
            te_s = cls._normalize_date_tag(te_s_raw, default="NA")
            te_e = cls._normalize_date_tag(te_e_raw, default="NA")
            test_range_tag = f"TE{te_s}-{te_e}"
        else:
            test_range_tag = "TE_REFIT"

        run_dt = run_dt or datetime.datetime.now()
        run_stamp = run_dt.strftime("%Y%m%d-%H%M%S")
        pid_stamp = str(run_pid if run_pid is not None else os.getpid())

        return (
            f"{train_period_tag}_{test_period_tag}_{metric_tag}_{market_tag}_"
            f"TR{tr_s}-{tr_e}_{test_range_tag}_RUN{run_stamp}_{pid_stamp}"
        )

    def _auto_refine_study_name(self):
        """
        基于时间维度的自动化命名逻辑（始终自动生成）
        格式：[训练周期]_[测试周期]_[指标]_[市场]_[训练集范围]_[测试集范围]_[运行时间]
        """
        new_name = self.build_optuna_name_tag(
            metric=self.args.metric,
            train_period=self.args.train_roll_period,
            test_period=getattr(self.args, "test_roll_period", None),
            train_range=self.train_range,
            test_range=self.test_range,
            data_source=getattr(self.args, "data_source", None),
            symbols=self.target_symbols,
            selection=getattr(self.args, "selection", None),
            run_dt=datetime.datetime.now(),
            run_pid=os.getpid(),
        )

        print(f"[Optimizer] Auto-refining study_name (Date-Based): {new_name}")
        self.args.study_name = new_name

    def _launch_dashboard(self, log_file, port=8080, background=True):
        """
        直接在代码中运行 Optuna Dashboard。
        - background=True: 后台线程模式（默认）
        - background=False: 前台阻塞模式（按 Ctrl-C 退出）
        """
        if not HAS_DASHBOARD:
            print("[Warning] 'optuna-dashboard' not installed. Skipping.")
            return

        import logging
        import http.server
        import wsgiref.simple_server

        # 直接覆盖标准库 http.server 的日志方法，彻底消除访问日志
        def silent_log_message(self, format, *args):
            return  # 什么都不做，直接返回

        # 覆盖 http.server 的日志方法 (bottle 默认 server 基于此)
        http.server.BaseHTTPRequestHandler.log_message = silent_log_message
        # 同时也覆盖 wsgiref 的日志方法 (双重保险)
        wsgiref.simple_server.WSGIRequestHandler.log_message = silent_log_message

        mode_str = "Thread Mode" if background else "Foreground Mode"
        print("\n" + "=" * 60)
        print(f">>> STARTING DASHBOARD ({mode_str}) <<<")
        print("=" * 60)

        def build_storage_and_run():
            # 静默日志
            loggers_to_silence = [
                "optuna",
                "optuna_dashboard",
                "sqlalchemy",
                "bottle",
                "waitress",
                "werkzeug"
            ]
            for name in loggers_to_silence:
                logging.getLogger(name).setLevel(logging.ERROR)

            # 1. 在线程内部初始化存储对象
            # 这样可以确保它读取的是最新的文件
            storage = JournalStorage(JournalFileBackendCls(log_file))

            # 2. 启动服务 (这是一个阻塞操作，会一直运行)
            run_server(storage, host="127.0.0.1", port=port)

        def open_browser_later(url):
            try:
                time.sleep(1.0)
                webbrowser.open(url)
            except Exception:
                pass

        dashboard_url = f"http://127.0.0.1:{port}"
        print(f"[Success] Dashboard is running at: {dashboard_url}")

        if background:
            def start_server():
                try:
                    build_storage_and_run()
                except OSError as e:
                    if "Address already in use" in str(e) or (hasattr(e, 'winerror') and e.winerror == 10048):
                        print(f"\n[Error] Port {port} was seized by another process just now! Dashboard failed.")
                    else:
                        print(f"\n[Error] Dashboard thread failed: {e}")
                except Exception as e:
                    print(f"\n[Error] Dashboard crashed: {e}")

            # 3. 创建并启动守护线程
            t = threading.Thread(target=start_server, daemon=True)
            t.start()

            # 4. 尝试打开浏览器
            open_browser_later(dashboard_url)

            print("[INFO] Dashboard running in background thread.")
            print("=" * 60 + "\n")
            return

        # 前台模式：主线程阻塞，允许用户人工排查后 Ctrl-C 退出
        print("[INFO] Dashboard running in foreground. Press Ctrl-C to stop.")
        print("=" * 60 + "\n")
        threading.Thread(target=open_browser_later, args=(dashboard_url,), daemon=True).start()
        try:
            build_storage_and_run()
        except KeyboardInterrupt:
            print("\n[INFO] Dashboard stopped by user (Ctrl-C).")
        except OSError as e:
            if "Address already in use" in str(e) or (hasattr(e, 'winerror') and e.winerror == 10048):
                print(f"\n[Error] Port {port} was seized by another process just now! Dashboard failed.")
            else:
                print(f"\n[Error] Dashboard failed: {e}")
        except Exception as e:
            print(f"\n[Error] Dashboard crashed: {e}")

    def _estimate_n_trials(self):
        """
        启发式算法：熵模型保底 + 动态多核历史公式放大校准。
        核心公式：
            N = max(N_entropy, N_legacy_dynamic)
            N_legacy_dynamic = (100 + S * sqrt(d_all)) * (1 + sqrt(C))
        其中：
            - N_entropy: 熵复杂度估计（基于参数空间的离散组合或连续区间）
            - N_legacy_dynamic: 基于当前机器真实CPU核数(C)动态缩放的历史经验公式
            - S: 历史复杂度评分（沿用旧版评分口径）
            - d_all: 总参数维度
            - C: 当前机器可用的 CPU 核心数
        """
        entropy_nats = 0.0
        effective_dims = 0
        continuous_dims = 0
        finite_space_size = 1
        is_finite_space = True

        for _, p_cfg in self.opt_params_def.items():
            cardinality, is_finite = self._estimate_param_cardinality(p_cfg)
            cardinality = max(1, int(cardinality))

            if cardinality <= 1:
                continue

            effective_dims += 1
            entropy_nats += math.log(cardinality)

            if is_finite:
                finite_space_size *= cardinality
            else:
                continuous_dims += 1
                is_finite_space = False

        if effective_dims == 0:
            return 1

        # 熵主导 + 交互惩罚 + 连续参数惩罚（KISS：常数内置，不暴露配置）
        entropy_term = 80.0 * entropy_nats
        interaction_term = 35.0 * effective_dims * math.log(effective_dims + 1.0)
        continuous_term = 120.0 * continuous_dims
        floor_term = 30.0 * effective_dims

        entropy_estimated = int(round(max(floor_term, entropy_term + interaction_term + continuous_term)))
        entropy_estimated = max(1, entropy_estimated)

        # 16核历史公式：恢复你之前常用的训练规模量级（约 16k）
        legacy_complexity_score = 0.0
        total_dims = max(1, len(self.opt_params_def))
        for _, p_cfg in self.opt_params_def.items():
            p_type = p_cfg.get('type')
            if p_type == 'int':
                low = int(p_cfg.get('low', 0))
                high = int(p_cfg.get('high', low))
                step = int(p_cfg.get('step', 1) or 1)
                step = max(1, step)
                range_len = abs(high - low) / step
                legacy_complexity_score += math.log(max(range_len, 2.0)) * 30.0
            elif p_type == 'float':
                # 与旧公式一致：float 统一固定权重
                legacy_complexity_score += 60.0
            elif p_type == 'categorical':
                legacy_complexity_score += len(p_cfg.get('choices', [])) * 15.0
            else:
                legacy_complexity_score += 10.0

        legacy_base = 100.0 + legacy_complexity_score * math.sqrt(total_dims)

        # 获取当前机器的真实核心数，用于动态缩放算力预算
        actual_cores = self._get_total_cpu_cores()
        dynamic_core_scale = 1.0 + math.sqrt(float(actual_cores))
        legacy_dynamic_estimated = int(round(max(1.0, legacy_base * dynamic_core_scale)))

        estimated = max(entropy_estimated, legacy_dynamic_estimated)

        # 有限空间下不超过总组合数
        if is_finite_space:
            estimated = min(estimated, finite_space_size)

        print(
            "[Optimizer] n_trials estimator: "
            f"entropy={entropy_nats:.2f}, dims={effective_dims}, cont_dims={continuous_dims}, "
            f"entropy_est={entropy_estimated}, legacy_{actual_cores}cores_est={legacy_dynamic_estimated}, "
            f"finite_space={'yes' if is_finite_space else 'no'} -> {estimated}"
        )
        return estimated

    @staticmethod
    def _estimate_param_cardinality(param_cfg):
        """
        返回参数的有效离散基数 K 与是否为有限离散空间。
        连续 float（无 step）使用虚拟离散基数近似熵，不参与有限空间上限。
        """
        p_type = param_cfg.get('type')

        if p_type == 'int':
            low = int(param_cfg.get('low', 0))
            high = int(param_cfg.get('high', low))
            step = int(param_cfg.get('step', 1) or 1)
            step = max(1, step)
            if high < low:
                low, high = high, low
            count = ((high - low) // step) + 1
            return max(1, count), True

        if p_type == 'float':
            low = float(param_cfg.get('low', 0.0))
            high = float(param_cfg.get('high', low))
            if high < low:
                low, high = high, low
            if abs(high - low) <= 1e-12:
                return 1, True
            step = param_cfg.get('step', None)
            if step is not None:
                try:
                    step = float(step)
                except (TypeError, ValueError):
                    step = None
            if step is not None and step > 0:
                count = int(math.floor((high - low) / step + 1e-12)) + 1
                return max(1, count), True

            # 连续空间：用区间宽度映射为有限“信息桶”近似熵
            span = max(0.0, high - low)
            virtual_bins = int(math.ceil(span * 20.0)) + 1
            virtual_bins = max(32, min(128, virtual_bins))
            return virtual_bins, False

        if p_type == 'categorical':
            choices = param_cfg.get('choices', [])
            return max(1, len(choices)), True

        return 1, True

    def _evaluate_trial_params(self, current_params):
        import math
        if not self.train_datas:
            return -9999.0

        bt_instance = None
        strat = None
        try:
            bt_instance = Backtester(
                datas=self.train_datas,
                strategy_class=self.strategy_class,
                params=current_params,
                start_date=self.train_range[0],
                end_date=self.train_range[1],
                cash=self.args.cash,
                commission=self.args.commission,
                slippage=self.args.slippage,
                risk_control_classes=self.risk_control_classes,
                risk_control_params=self.risk_params,
                timeframe=self.args.timeframe,
                compression=self.args.compression,
                enable_plot=False,
                verbose=False,
                indicator_cache=getattr(self, "_indicator_cache", None),
            )

            bt_instance.run()

            # 检查回测是否成功生成结果，防止烂参数导致引擎空转
            if not getattr(bt_instance, 'results', None) or len(bt_instance.results) == 0:
                return -100.0

            strat = bt_instance.results[0]

            try:
                # 收益率 (百分比)
                total_return_pct = (bt_instance.get_custom_metric('return') or 0.0) * 100.0

                # 夏普比率
                sharpe = float(bt_instance.get_custom_metric('sharpe') or 0.0)
                sharpe = 0.0 if (math.isinf(sharpe) or math.isnan(sharpe)) else sharpe

                # 卡玛比率
                calmar = bt_instance.get_custom_metric('calmar') or 0.0
                calmar = 0.0 if (math.isinf(calmar) or math.isnan(calmar)) else calmar

                # 交易统计分析
                ta = strat.analyzers.getbyname('tradeanalyzer').get_analysis()
                total_trades = ta.get('total', {}).get('total', 0)
                win_rate = ta.get('won', {}).get('total', 0) / max(total_trades, 1)

                # 盈亏因子计算
                won_total = ta.get('won', {}).get('pnl', {}).get('total', 0)
                lost_total = abs(ta.get('lost', {}).get('pnl', {}).get('total', 0))
                profit_factor = won_total / lost_total if lost_total > 0 else won_total

                # 最大回撤
                mdd = strat.analyzers.getbyname('drawdown').get_analysis().get('max', {}).get('drawdown', 100.0)
                safe_mdd = max(mdd, 1.0)  # 防除零溢出

                # 运行时间折算 (用于计算年化要求)
                if len(strat.data) > 0:
                    days = (strat.data.datetime.datetime(0) - strat.data.datetime.datetime(-len(strat.data) + 1)).days
                    years = max(days / 365.25, 0.1)
                else:
                    years = 1.0

                # 月度胜率用于一致性评分。过滤零收益月份，避免现金空转月污染。
                monthly_win_rate = 0.0
                try:
                    monthly_returns = strat.analyzers.getbyname('timereturn_monthly').get_analysis()
                    active_monthly_returns = []
                    for monthly_return in monthly_returns.values():
                        try:
                            monthly_return = float(monthly_return)
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(monthly_return) and abs(monthly_return) > 1e-12:
                            active_monthly_returns.append(monthly_return)
                    if active_monthly_returns:
                        monthly_win_rate = (
                            sum(1 for monthly_return in active_monthly_returns if monthly_return > 0)
                            / len(active_monthly_returns)
                        )
                except MemoryError:
                    self._release_memory_pressure()
                    raise
                except Exception:
                    monthly_win_rate = 0.0

            except MemoryError:
                self._release_memory_pressure()
                raise
            except Exception as e:
                # Analyzer 解析失败，通常意味着参数导致了无法交易，直接判死刑
                return -100.0

            # 封装标准化指标字典，空投给私有打分插件
            stats = {
                'total_return_pct': total_return_pct,
                'sharpe': sharpe,
                'calmar': calmar,
                'total_trades': total_trades,
                'win_rate': win_rate,
                'profit_factor': profit_factor,
                'mdd': mdd,
                'safe_mdd': safe_mdd,
                'years': years,
                'monthly_win_rate': monthly_win_rate,
            }

            if self.args.metric in ['sharpe', 'calmar', 'return']:
                metric_val = bt_instance.get_custom_metric(self.args.metric)
                if metric_val == -999.0 and self.args.metric == 'sharpe':
                    ret = bt_instance.get_custom_metric('return')
                    metric_val = ret * 0.1 if ret > 0 else ret
                return metric_val

            # 触发插件化的复合打分 (全域动态路由)
            else:
                try:
                    import math  # 确保内部可以使用 math
                    # 获取缓存的内存函数指针 (调用文件顶部的路由雷达)
                    metric_func = get_metric_function(self.args.metric)

                    # 执行外部私有打分逻辑
                    final_score = metric_func(stats, strat=strat, args=self.args)

                    # 容错降级：如果用户写的打分插件有 bug 返回了 NaN/Inf，直接给惩罚分保护引擎
                    if final_score is None or math.isnan(final_score) or math.isinf(final_score):
                        return -100.0

                    return float(final_score)

                except MemoryError:
                    self._release_memory_pressure()
                    raise
                except Exception as e:
                    # 捕获外部插件抛出的异常，防止某一次试错导致整个 Optuna Study 崩溃退出
                    return -100.0

        except MemoryError:
            self._release_memory_pressure()
            raise
        except Exception as e:
            import traceback
            print(f"Trial failed: {e}")
            traceback.print_exc()
            return -9999.0
        finally:
            strat = None
            bt_instance = None

    def objective(self, trial):
        try:
            current_params = copy.deepcopy(self.fixed_params)
            trial_params_dict = {}

            for param_name, config in self.opt_params_def.items():
                p_type = config.get('type')
                if p_type == 'int':
                    step = config.get('step', 1)
                    high = config['high']
                    low = config['low']
                    corrected_high = low + int((high - low) // step) * step
                    val = trial.suggest_int(param_name, low, corrected_high, step=step)
                elif p_type == 'float':
                    step = config.get('step', None)
                    low = config['low']
                    high = config['high']
                    if step is not None:
                        steps = math.floor((high - low) / step + 1e-10)
                        corrected_high = low + steps * step
                        if abs(corrected_high - high) > 1e-10:
                            high = corrected_high
                    val = trial.suggest_float(param_name, low, high, step=step)
                elif p_type == 'categorical':
                    val = trial.suggest_categorical(param_name, config['choices'])
                else:
                    val = config.get('value')

                current_params[param_name] = val
                trial_params_dict[param_name] = val

            params_key = self._params_to_key(trial_params_dict)

            cached_value = self._get_cached_trial_value(params_key)
            if cached_value is not None:
                return cached_value

            score = self._evaluate_trial_params(current_params)
            self._cache_completed_trial(params_key, score)
            return score
        except MemoryError:
            self._release_memory_pressure()
            raise

    def prepare_data_index(self, df: pd.DataFrame) -> pd.DataFrame:
        """确保 DataFrame 的索引是 DatetimeIndex"""
        if isinstance(df.index, pd.DatetimeIndex):
            return df

        date_cols = ['date', 'datetime', 'trade_date', 'Date', 'Datetime']
        for col in date_cols:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col])
                df.set_index(col, inplace=True)
                return df

        try:
            df.index = pd.to_datetime(df.index)
            return df
        except:
            pass
        return df

    def slice_datas(self, start_date: str, end_date: str):
        """根据日期切分数据字典，并保留逻辑起点前的指标预热数据。"""
        sliced = {}
        if not start_date and not end_date:
            return self.raw_datas

        s = pd.to_datetime(self._warmup_start_date(start_date)) if start_date else pd.Timestamp.min
        e = pd.to_datetime(end_date) if end_date else pd.Timestamp.max

        for symbol, df in self.raw_datas.items():
            df = self.prepare_data_index(df)
            try:
                mask = (df.index >= s) & (df.index <= e)
                sub_df = df.loc[mask]
                if not sub_df.empty:
                    sliced[symbol] = sub_df
                else:
                    pass
            except Exception as e:
                print(f"Error slicing data for {symbol}: {e}")

        return sliced

    def _infer_recent_3y_window(self):
        """
        使用与 CLI 缺省 start_date 一致的逻辑，推断最近三年区间。
        """
        end_str = self.args.end_date or datetime.datetime.now().strftime('%Y%m%d')
        end_dt = pd.to_datetime(str(end_str))
        start_dt = end_dt - pd.DateOffset(years=3)
        return start_dt.strftime('%Y%m%d'), end_dt.strftime('%Y%m%d')

    def _infer_main_eval_window(self):
        """
        推断训练后主回测窗口：至少最近三年，最长覆盖训练+测试逻辑窗口。
        该窗口不包含 warm-up 起点；warm-up 只在数据切片时补入。
        """
        _, recent_end = self._infer_recent_3y_window()
        end_str = recent_end
        if getattr(self, "test_range", None) and self.test_range[1]:
            end_str = self.test_range[1]
        elif self.args.end_date:
            end_str = self.args.end_date

        end_dt = pd.to_datetime(str(end_str))
        recent_start = (end_dt - pd.DateOffset(years=3)).strftime('%Y%m%d')
        start_candidates = [recent_start]
        if getattr(self, "train_range", None) and self.train_range[0]:
            start_candidates.append(self.train_range[0])
        elif self.args.start_date:
            start_candidates.append(self.args.start_date)

        start_dt = min(pd.to_datetime(str(value)) for value in start_candidates if value)
        return start_dt.strftime('%Y%m%d'), end_dt.strftime('%Y%m%d')

    def _infer_yearly_validation_windows(self):
        """
        Build natural-year validation windows from already loaded raw data.

        This intentionally does not fetch provider data. It is a diagnostic
        report for the optimizer summary, so it must preserve Tiingo quota.
        """
        indexes = []
        for df in getattr(self, "raw_datas", {}).values():
            if df is None or getattr(df, "empty", True):
                continue
            prepared = self.prepare_data_index(df)
            if isinstance(prepared.index, pd.DatetimeIndex) and len(prepared.index) > 0:
                indexes.append(prepared.index)

        if not indexes:
            return []

        all_index = indexes[0]
        for idx in indexes[1:]:
            all_index = all_index.union(idx)
        if len(all_index) == 0:
            return []

        min_dt = pd.to_datetime(all_index.min()).normalize()
        max_dt = pd.to_datetime(all_index.max()).normalize()
        logical_min_dt = max_dt - pd.DateOffset(years=5)
        if getattr(self, "train_range", None) and self.train_range[0]:
            try:
                logical_min_dt = min(logical_min_dt, pd.to_datetime(self.train_range[0]).normalize())
            except Exception:
                pass

        windows = []
        for year in range(int(logical_min_dt.year), int(max_dt.year) + 1):
            start_dt = pd.Timestamp(year=year, month=1, day=1)
            end_dt = pd.Timestamp(year=year, month=12, day=31)
            start_dt = max(start_dt, logical_min_dt)
            end_dt = min(end_dt, max_dt)
            if start_dt > end_dt:
                continue

            # 跳过过小的边缘窗口；它们会增加噪声并消耗 CPU，收益很低。
            available_dates = all_index[(all_index >= start_dt) & (all_index <= end_dt)]
            if len(available_dates) < 60:
                continue

            windows.append((start_dt.strftime('%Y%m%d'), end_dt.strftime('%Y%m%d')))

        return windows

    def _collect_backtest_metrics(self, perf, start_date, end_date):
        return {
            "start_date": start_date or perf.get("start_date"),
            "end_date": end_date or perf.get("end_date"),
            "total_return": perf.get("total_return"),
            "annual_return": perf.get("annual_return"),
            "sharpe_ratio": perf.get("sharpe_ratio"),
            "max_drawdown": perf.get("max_drawdown"),
            "calmar_ratio": perf.get("calmar_ratio"),
            "total_trades": perf.get("total_trades"),
            "win_rate": perf.get("win_rate"),
            "monthly_win_rate": perf.get("monthly_win_rate"),
            "profit_factor": perf.get("profit_factor"),
            "final_portfolio": perf.get("final_portfolio"),
        }

    def _run_backtest_on_datas(self, datas, final_params, start_date, end_date, verbose=False):
        bt_instance = Backtester(
            datas=datas,
            strategy_class=self.strategy_class,
            params=final_params,
            start_date=start_date,
            end_date=end_date,
            cash=self.args.cash,
            commission=self.args.commission,
            slippage=self.args.slippage,
            risk_control_classes=self.risk_control_classes,
            risk_control_params=self.risk_params,
            timeframe=self.args.timeframe,
            compression=self.args.compression,
            enable_plot=False,
            verbose=False,
            indicator_cache=getattr(self, "_indicator_cache", None),
        )
        bt_instance.run()
        if verbose:
            bt_instance.display_results()

        perf = bt_instance.get_performance_metrics()
        if not perf:
            return None

        metrics = self._collect_backtest_metrics(perf, start_date, end_date)
        report = None
        if hasattr(bt_instance, "get_trade_micro_attribution_report"):
            try:
                report = bt_instance.get_trade_micro_attribution_report()
            except Exception as exc:
                print(f"[Optimizer] Warning: trade attribution report unavailable: {exc}")
        if report:
            metrics["trade_micro_attribution_report"] = report
        return metrics

    def _fetch_datas_for_window(self, start_date: str, end_date: str):
        """
        按指定窗口获取数据：
        1) 优先复用内存中的 raw_datas 切片（零网络请求）
        2) 对覆盖不足的标的才向 provider 补拉
        3) 结果做窗口级缓存，供多指标/基准复用
        """
        cache_key = f"{start_date}:{end_date}:warmup{self.warmup_days}"
        if cache_key in self._window_data_cache:
            print(f"[Optimizer] Reusing cached window data: {start_date} to {end_date}")
            return self._window_data_cache[cache_key]

        datas = {}
        physical_start_date = self._warmup_start_date(start_date)
        s = pd.to_datetime(physical_start_date) if physical_start_date else pd.Timestamp.min
        e = pd.to_datetime(end_date) if end_date else pd.Timestamp.max
        raw_fetch_start, raw_fetch_end = getattr(self, "_raw_data_fetch_range", (None, None))
        try:
            preloaded_request_covers_window = (
                bool(raw_fetch_start)
                and bool(raw_fetch_end)
                and pd.to_datetime(str(raw_fetch_start)) <= s
                and pd.to_datetime(str(raw_fetch_end)) >= e
            )
        except Exception:
            preloaded_request_covers_window = False

        for symbol in self.target_symbols:
            used_preloaded = False
            raw_df = self.raw_datas.get(symbol)
            if raw_df is not None and not raw_df.empty:
                prepared_df = self.prepare_data_index(raw_df)
                try:
                    raw_end = prepared_df.index.max()
                    has_window = (
                        len(prepared_df) > 0
                        and (prepared_df.index.min() <= s or preloaded_request_covers_window)
                        and (raw_end >= e or preloaded_request_covers_window)
                    )
                    if has_window:
                        mask = (prepared_df.index >= s) & (prepared_df.index <= e)
                        sliced_df = prepared_df.loc[mask]
                        if not sliced_df.empty:
                            datas[symbol] = sliced_df
                            used_preloaded = True
                            if raw_end < e and preloaded_request_covers_window:
                                print(
                                    f"[Optimizer] Reusing preloaded data for {symbol} through "
                                    f"{pd.to_datetime(raw_end).strftime('%Y%m%d')}; "
                                    f"requested end is {end_date}."
                                )
                except Exception:
                    pass

            if used_preloaded:
                continue

            df = self.data_manager.get_data(
                symbol,
                start_date=physical_start_date,
                end_date=end_date,
                specified_sources=self.args.data_source,
                timeframe=self.args.timeframe,
                compression=self.args.compression,
                refresh=self.args.refresh
            )
            if df is not None and not df.empty:
                datas[symbol] = df
            else:
                print(f"[Optimizer] Warning: No evaluation data for {symbol}, skipping.")

        self._window_data_cache[cache_key] = datas
        return datas

    def _run_main_eval_backtest(self, final_params):
        """
        优化结束后自动执行主回测，并返回核心指标用于最终汇总。
        """
        eval_start, eval_end = self._infer_main_eval_window()

        print("-" * 60)
        print(f"Running Main Evaluation Backtest: {eval_start} to {eval_end}")
        print("-" * 60)

        eval_datas = self._fetch_datas_for_window(eval_start, eval_end)
        if not eval_datas:
            print("[Optimizer] Warning: Main evaluation backtest skipped (no valid data).")
            return None

        try:
            metrics = self._run_backtest_on_datas(
                eval_datas,
                final_params,
                eval_start,
                eval_end,
                verbose=True,
            )
            if not metrics:
                print("[Optimizer] Warning: Main evaluation backtest finished but metrics are unavailable.")
                return None
        except Exception as e:
            print(f"[Optimizer] Warning: Main evaluation backtest failed: {e}")
            return None

        return metrics

    def _run_recent_3y_backtest(self, final_params):
        """
        Backward-compatible alias for callers still using the old method name.
        """
        return self._run_main_eval_backtest(final_params)

    def _run_test_set_backtest(self, final_params, verbose=False):
        """
        对训练集外测试窗口执行自动回测，并返回核心指标用于多指标汇总对比。
        """
        if not self.test_datas:
            return None

        test_start = (self.test_range or (None, None))[0]
        test_end = (self.test_range or (None, None))[1]

        print("-" * 60)
        print(f"Running Validation on Test Set: {test_start} to {test_end}")
        print("-" * 60)

        try:
            metrics = self._run_backtest_on_datas(
                self.test_datas,
                final_params,
                test_start,
                test_end,
                verbose=verbose,
            )
            if not metrics:
                print("[Optimizer] Warning: Test-set backtest finished but metrics are unavailable.")
                return None
        except Exception as e:
            print(f"[Optimizer] Warning: Test-set backtest failed: {e}")
            return None

        return metrics

    def _run_yearly_validation_backtests(self, final_params):
        windows = self._infer_yearly_validation_windows()
        if not windows:
            return []

        test_start, test_end = self.test_range or (None, None)
        test_key = (
            self._normalize_date_tag(test_start) if test_start else None,
            self._normalize_date_tag(test_end) if test_end else None,
        )

        print("-" * 60)
        print("Running Yearly Fixed-Window Validation (reusing in-memory raw data)")
        print("-" * 60)

        reports = []
        for start_date, end_date in windows:
            window_key = (self._normalize_date_tag(start_date), self._normalize_date_tag(end_date))
            if all(test_key) and window_key == test_key:
                continue

            datas = self.slice_datas(start_date, end_date)
            if not datas:
                print(f"[Optimizer] Warning: Yearly validation skipped (no data): {start_date} to {end_date}")
                continue

            try:
                metrics = self._run_backtest_on_datas(
                    datas,
                    final_params,
                    start_date,
                    end_date,
                    verbose=False,
                )
                if metrics:
                    reports.append(metrics)
            except Exception as e:
                print(f"[Optimizer] Warning: Yearly validation failed {start_date} to {end_date}: {e}")

        return reports

    def run(self):
        # 1. 配置存储 (支持多核)
        storage = None
        n_jobs = getattr(self.args, 'n_jobs', 1)
        resolved_requested_workers = self._resolve_worker_count(n_jobs)
        auto_launch_dashboard = bool(getattr(self.args, 'auto_launch_dashboard', True))
        shared_journal_log_file = getattr(self.args, 'shared_journal_log_file', None)
        use_journal_storage = (resolved_requested_workers != 1) or bool(shared_journal_log_file)

        log_file = None

        if use_journal_storage:
            if HAS_JOURNAL:
                log_dir = os.path.join(os.getcwd(), config.DATA_PATH, 'optuna')
                os.makedirs(log_dir, exist_ok=True)

                # 支持多指标共享同一个 Journal 文件，以便最终只弹出一个聚合 Dashboard
                if shared_journal_log_file:
                    shared_dir = os.path.dirname(shared_journal_log_file)
                    if shared_dir:
                        os.makedirs(shared_dir, exist_ok=True)
                    log_file = shared_journal_log_file
                else:
                    # 为每个 study 创建独立的日志文件，彻底消除跨任务的锁争抢
                    log_file = os.path.join(log_dir, f"optuna_{self.args.study_name}.log")

                try:
                    # 尝试创建文件存储
                    storage = JournalStorage(JournalFileBackendCls(log_file))
                    if resolved_requested_workers != 1:
                        print(
                            f"\n[Optimizer] Multi-core mode enabled "
                            f"(n_jobs={n_jobs} -> workers={resolved_requested_workers})."
                        )
                    else:
                        print(f"\n[Optimizer] JournalStorage enabled for dashboard/log persistence (n_jobs=1).")
                    print(f"[Optimizer] Using JournalStorage: {log_file}")
                except OSError as e:
                    # 专门捕获 Windows 权限错误 (WinError 1314)
                    if hasattr(e, 'winerror') and e.winerror == 1314:
                        print("\n" + "!" * 60)
                        print("[ERROR] Windows Permission Error (WinError 1314)")
                        print(
                            "Multi-core optimization on Windows (using JournalStorage) requires symbolic link privileges.")
                        print("\nPLEASE TRY ONE OF THE FOLLOWING:")
                        print("  1. Run your PowerShell/Terminal as Administrator.")
                        print(
                            "  2. OR Enable 'Developer Mode' in Windows Settings (Privacy & security -> For developers).")
                        print("  3. OR Run with --n_jobs 1 to use single-core mode.")
                        print("!" * 60 + "\n")
                        sys.exit(1)
                    else:
                        raise e
            else:
                if resolved_requested_workers != 1:
                    print("\n[Warning] optuna.storages.JournalStorage not found.")
                    print("[Warning] Fallback to single-core to avoid SQLite dependency.")
                    n_jobs = 1
                    resolved_requested_workers = 1
                elif shared_journal_log_file:
                    print("\n[Warning] JournalStorage unavailable. Dashboard persistence disabled for this run.")

        # 2. 确定 n_trials；TPE 采样器需要基于训练规模选择候选数。
        n_trials = self.args.n_trials
        if n_trials is None:
            n_trials = self._estimate_n_trials()
            print(f"[Optimizer] Auto-inferred n_trials: {n_trials} (entropy-complexity model)")
        else:
            n_trials = int(n_trials)
            if n_trials <= 0:
                raise ValueError(f"n_trials must be a positive integer, got: {n_trials}")

        # 使用 TPESampler(constant_liar=True)
        # 这会防止多个 Worker 同时采样到同一个点（并发踩踏）
        sampler = TPESampler(
            constant_liar=True,
            n_ei_candidates=self.TPE_DEFAULT_N_EI_CANDIDATES,
        )

        # 2. 创建 Study (包裹 try-except 以捕获 Windows 权限错误)
        try:
            study = optuna.create_study(
                direction='maximize',
                study_name=self.args.study_name,
                storage=storage,
                load_if_exists=True,
                sampler=sampler,
            )

            # 将命令行参数记录到 Study User Attributes
            # vars(args) 可以将 Namespace 转换为字典，方便遍历
            for key, value in vars(self.args).items():
                # 为了防止日志干扰或 token 泄露，可以根据需要做简单过滤
                # 这里将所有参数转为字符串存储，方便在 Dashboard 右下角直接查阅
                study.set_user_attr(key, str(value))

        except OSError as e:
            # 捕获 WinError 1314 (Symlink 权限不足)
            if hasattr(e, 'winerror') and e.winerror == 1314:
                print("\n" + "!" * 60)
                print("[WARNING] Windows Permission Error (WinError 1314).")
                print(
                    "          Multi-core optimization requires Administrator privileges to create lock files.")
                print(
                    "          请使用管理员权限运行终端后执行，以进行多核优化")
                print("          >> AUTOMATICALLY FALLING BACK TO SINGLE-CORE MODE. <<")
                print("          >> 自动降级为单核优化模式. <<")
                print("!" * 60 + "\n")

                # 降级：重置为单核 + 内存存储
                n_jobs = 1
                resolved_requested_workers = 1
                storage = None
                study = optuna.create_study(
                    direction='maximize',
                    study_name=self.args.study_name,
                    storage=None,
                    load_if_exists=True,
                    sampler=sampler,
                )
            else:
                # 其他错误照常抛出
                raise e

        resolved_workers = self._resolve_worker_count(n_jobs)
        effective_parallel_jobs = min(resolved_workers, max(1, int(n_trials)))

        if auto_launch_dashboard and log_file and os.path.exists(log_file):
            # 端口检测与递增逻辑
            base_port = getattr(config, 'OPTUNA_DASHBOARD_PORT', 8090)
            target_port = base_port

            # 尝试寻找可用端口，最多尝试 100 次
            for i in range(100):
                if not is_port_in_use(target_port):
                    break
                target_port += 1
            else:
                print(f"[Warning] Could not find an available port starting from {base_port}. Dashboard might fail.")

            self._launch_dashboard(log_file, port=target_port)

        print(f"\n--- Starting Optimization ({n_trials} trials, {effective_parallel_jobs} parallel jobs) ---")

        # 3. 执行优化
        try:
            if effective_parallel_jobs > 1:
                self._run_multiprocess_optimization(
                    n_jobs=n_jobs,
                    n_trials=n_trials,
                    log_file=log_file,
                    prefer_fork_cow=(not auto_launch_dashboard),
                )
            else:
                # 单核/单并行场景回退为单进程线程模式（与历史版本一致）
                # 这里保留 Optuna 的 n_jobs 参数入口，避免强制写死为 1。
                thread_jobs = max(1, min(int(n_trials), self._resolve_worker_count(n_jobs)))
                if thread_jobs != 1:
                    print(f"[Optimizer] Fallback to single-process threaded mode (n_jobs={thread_jobs}).")
                study.optimize(
                    self.objective,
                    n_trials=n_trials,
                    n_jobs=thread_jobs,
                    gc_after_trial=True,
                )
        except KeyboardInterrupt:
            print("\n[Optimizer] Optimization stopped by user.")
        except MemoryError as exc:
            self._release_memory_pressure()
            print(f"\n[Optimizer] Optimization stopped early due to memory pressure: {exc}")
            print("[Optimizer] Completed trials will be used for the final report if available.")

        completed_trials = [
            trial for trial in study.trials
            if trial.state == optuna.trial.TrialState.COMPLETE
        ]
        if not completed_trials:
            print("No trials finished.")
            return

        best_params = study.best_params
        best_value = study.best_value

        print("\n" + "=" * 60)
        print(">>> FINAL REPORT & OUT-OF-SAMPLE VALIDATION <<<")
        print("=" * 60)

        final_params = copy.deepcopy(self.fixed_params)
        final_params.update(best_params)

        print(f"Best Parameters Found (Train Set):")
        for k, v in best_params.items():
            print(f"  {k}: {v}")

        best_val_display = format_float(best_value, digits=4)
        print(f"Best Training Score ({self.args.metric}): {best_val_display}")

        if self.test_datas:
            test_metrics = self._run_test_set_backtest(final_params, verbose=True)
        else:
            test_metrics = None
            print("\n(No Test Set Configured)")

        main_eval_metrics = self._run_main_eval_backtest(final_params)
        yearly_metrics = self._run_yearly_validation_backtests(final_params)

        print("\n" + "=" * 60)
        print(" SUMMARY OF BEST CONFIGURATION")
        print("=" * 60)
        print(f" Strategy: {self.args.strategy}")
        print(f" Params:   {final_params}")
        if main_eval_metrics:
            recent_fmt = format_recent_backtest_metrics(main_eval_metrics)
            print(f" MainEval: {main_eval_metrics.get('start_date')} -> {main_eval_metrics.get('end_date')}")
            print(f" Annual:   {recent_fmt['annual_return']}")
            print(f" Drawdown: {recent_fmt['max_drawdown']}")
            print(f" Calmar:   {recent_fmt['calmar_ratio']}")
            print(f" Sharpe:   {recent_fmt['sharpe_ratio']}")
            print(f" Trades:   {recent_fmt['total_trades']}")
            print(f" WinRate:  {recent_fmt['win_rate']}")
            print(f" PF:       {recent_fmt['profit_factor']}")
        if test_metrics:
            test_fmt = format_recent_backtest_metrics(test_metrics)
            print(f" TestSet:  {test_metrics.get('start_date')} -> {test_metrics.get('end_date')}")
            print(f" Annual:   {test_fmt['annual_return']}")
            print(f" Drawdown: {test_fmt['max_drawdown']}")
            print(f" Calmar:   {test_fmt['calmar_ratio']}")
            print(f" Sharpe:   {test_fmt['sharpe_ratio']}")
            print(f" Trades:   {test_fmt['total_trades']}")
            print(f" WinRate:  {test_fmt['win_rate']}")
            print(f" PF:       {test_fmt['profit_factor']}")
        if yearly_metrics:
            print(f" YearlyWindows: {len(yearly_metrics)}")
        print("=" * 60 + "\n")

        return {
            "best_score": best_val_display,
            "best_params": best_params,
            "trials_completed": len(study.trials),
            "log_file": log_file,
            "main_eval_backtest": main_eval_metrics,
            "recent_backtest": main_eval_metrics,
            "test_backtest": test_metrics,
            "yearly_backtests": yearly_metrics,
        }


def _optimize_worker_entry(
    worker_payload,
    study_name,
    log_file,
    n_trials,
    worker_idx,
    sampler_seed,
    tpe_n_ei_candidates=None,
):
    """
    多进程子进程入口：每个 worker 连接同一个 Study，执行固定 trial 配额。
    """
    if not HAS_JOURNAL:
        raise RuntimeError("JournalStorage is required for multi-process optimization.")

    if worker_payload is None:
        # fork + COW 模式：从模块全局中读取父进程继承的 payload
        worker_payload = _FORK_SHARED_WORKER_PAYLOAD
    if worker_payload is None:
        raise RuntimeError("Worker payload is missing.")

    # 在 worker 内同步主进程日志开关；缺省按训练静音处理。
    config.LOG = bool(worker_payload.get("log_enabled", False))
    worker_tee = None
    terminal_log_path = worker_payload.get("terminal_log_path") or get_optimizer_terminal_log_path()
    if terminal_log_path:
        worker_tee = install_optimizer_terminal_log(terminal_log_path, announce=False)

    worker_shm_handles = []
    job = None
    try:
        if worker_payload.get("train_datas") is None and worker_payload.get("train_datas_shared"):
            restored_train_datas, worker_shm_handles = OptimizationJob._restore_train_datas_from_shared(
                worker_payload["train_datas_shared"]
            )
            worker_payload = dict(worker_payload)
            worker_payload["train_datas"] = restored_train_datas

        storage = JournalStorage(JournalFileBackendCls(log_file))
        if tpe_n_ei_candidates is None:
            tpe_n_ei_candidates = OptimizationJob.TPE_DEFAULT_N_EI_CANDIDATES
        sampler = TPESampler(
            constant_liar=True,
            seed=sampler_seed,
            n_ei_candidates=tpe_n_ei_candidates,
        )

        study = optuna.create_study(
            direction='maximize',
            study_name=study_name,
            storage=storage,
            load_if_exists=True,
            sampler=sampler,
        )

        job = OptimizationJob.from_worker_payload(worker_payload)
        try:
            study.optimize(
                job.objective,
                n_trials=n_trials,
                n_jobs=1,
                gc_after_trial=True,
            )
        except MemoryError as exc:
            job._release_memory_pressure()
            print(
                f"[Optimizer] Worker {worker_idx} stopped early due to memory pressure: {exc}. "
                "Completed trials are kept in JournalStorage."
            )
            return {
                "worker_idx": worker_idx,
                "n_trials": n_trials,
                "stopped_early": True,
                "reason": "memory_pressure",
            }
        return {"worker_idx": worker_idx, "n_trials": n_trials, "stopped_early": False}
    except MemoryError as exc:
        if job is not None:
            job._release_memory_pressure()
        print(
            f"[Optimizer] Worker {worker_idx} stopped early due to setup memory pressure: {exc}. "
            "Completed trials are kept in JournalStorage."
        )
        return {
            "worker_idx": worker_idx,
            "n_trials": n_trials,
            "stopped_early": True,
            "reason": "memory_pressure",
        }
    finally:
        OptimizationJob._cleanup_shared_segments(worker_shm_handles, unlink=False)
        if worker_tee is not None:
            worker_tee.close()
