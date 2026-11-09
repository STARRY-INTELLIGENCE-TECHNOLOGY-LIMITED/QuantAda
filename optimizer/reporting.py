import re

import pandas as pd

from common.formatters import format_float, format_recent_backtest_metrics
from common.runtime_command import format_cli_command
from common.terminal_log import (
    OPTIMIZER_AI_ANALYSIS_END_MARKER,
    OPTIMIZER_AI_ANALYSIS_START_MARKER,
)


def normalize_metric_date(value):
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value)
        if pd.isna(ts):
            raise ValueError("NaT")
        return ts.strftime('%Y%m%d')
    except Exception:
        text = str(value).strip()
        if not text:
            return None
        digits = re.sub(r"[^0-9]", "", text)
        if len(digits) >= 8:
            return digits[:8]
        return text


def format_metric_label(report):
    metric_name = str(report.get('metric_name', 'Unknown'))
    score_str = str(report.get('best_score', 'N/A'))
    return f"{metric_name} ({score_str})" if score_str != "N/A" else metric_name


def training_elapsed_hours(trials):
    """返回试验墙钟小时数；并行重叠只计一次，没有试验的空档不计。"""
    intervals = []
    for trial in trials or ():
        start = _naive_timestamp(getattr(trial, "datetime_start", None))
        end = _naive_timestamp(getattr(trial, "datetime_complete", None))
        if start is None or end is None or end < start:
            continue
        intervals.append((start, end))
    if not intervals:
        return None
    intervals.sort(key=lambda item: (item[0], item[1]))
    merged_start, merged_end = intervals[0]
    total = pd.Timedelta(0)
    for start, end in intervals[1:]:
        if start <= merged_end:
            if end > merged_end:
                merged_end = end
            continue
        total += merged_end - merged_start
        merged_start, merged_end = start, end
    total += merged_end - merged_start
    return total.total_seconds() / 3600.0


def _naive_timestamp(value):
    """把试验时间转成可相减的 naive 时间；无效值返回 None。"""
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return stamp


def print_metric_row(metric_label, metrics_payload, elapsed_hours, params_payload, log_payload):
    fmt = format_recent_backtest_metrics(metrics_payload or {})
    m_str = str(metric_label)[:30]
    ret_str = fmt['annual_return']
    dd_str = fmt['max_drawdown']
    calmar_str = fmt['calmar_ratio']
    sharpe_str = fmt['sharpe_ratio']
    trades_str = fmt['total_trades']
    winrate_str = fmt['win_rate']
    pf_str = fmt['profit_factor']
    t_str = format_float(elapsed_hours, digits=1)
    b_str = str(params_payload)
    db_str = str(log_payload)
    print(
        f"| {m_str:<30} | {ret_str:<10} | {dd_str:<10} | "
        f"{calmar_str:<8} | {sharpe_str:<8} | {trades_str:<8} | {winrate_str:<10} | {pf_str:<8} | "
        f"{t_str:<8} | {b_str:<22} | {db_str}"
    )


def window_key(metrics_payload):
    metrics_payload = metrics_payload or {}
    return (
        normalize_metric_date(metrics_payload.get('start_date')),
        normalize_metric_date(metrics_payload.get('end_date')),
    )


def classify_sample_window(start, end, train_range):
    """
    判断报告窗口与训练期的关系，返回 (类别, 文本)。

    类别为 in/partial/out/before/unknown；只有整体晚于训练期结束的窗口才算样本外。
    """
    start, end = normalize_metric_date(start), normalize_metric_date(end)
    train_start = normalize_metric_date((train_range or (None, None))[0])
    train_end = normalize_metric_date((train_range or (None, None))[1])
    try:
        w_start, w_end = pd.to_datetime(start), pd.to_datetime(end)
        t_start, t_end = pd.to_datetime(train_start), pd.to_datetime(train_end)
        if pd.isna(w_start) or pd.isna(w_end) or pd.isna(t_start) or pd.isna(t_end):
            raise ValueError("invalid date")
    except Exception:
        return "unknown", "归属未知"
    if w_start > t_end:
        return "out", "样本外"
    if w_end < t_start:
        return "before", "训练期前(非样本外)"
    if w_start >= t_start and w_end <= t_end:
        return "in", "样本内(完全)"
    days = max((w_end - w_start).days + 1, 1)
    overlap = (min(w_end, t_end) - max(w_start, t_start)).days + 1
    return "partial", f"样本内(重叠{max(overlap, 0) / days:.0%})"


def _stress_change(base, stressed):
    """成本加压相对变化；基准缺失或为 0 时返回 N/A。"""
    try:
        base, stressed = float(base), float(stressed)
        if base == 0 or base != base or stressed != stressed:
            return "N/A"
        return f"{(stressed - base) / abs(base):+.0%}"
    except (TypeError, ValueError):
        return "N/A"


def print_sample_scope_line(label, payload, train_range):
    """打印单个窗口的样本归属一行说明。"""
    start, end = window_key(payload)
    if not (start and end):
        return
    print(f"  [{label}] 窗口 {start}->{end}: {classify_sample_window(start, end, train_range)[1]}")


def print_cost_stress_rows(final_reports):
    """打印 MainEval 成本加压回测与基准成本的对照；只用于报告，不影响最优参数选择。"""
    printed = False
    for report in final_reports or []:
        stress = report.get('cost_stress_backtest')
        if not stress:
            continue
        if not printed:
            print("MainEval 成本加压回测 (Cost Stress: 佣金与滑点翻倍；基数为 0 时取下限，仅报告)")
            printed = True
        label = format_metric_label(report)
        if stress.get('error'):
            print(f"  [{label}] 成本加压回测失败: {stress['error']}")
            continue
        base = report.get('main_eval_backtest') or report.get('recent_backtest') or {}
        base_fmt = format_recent_backtest_metrics(base)
        stress_fmt = format_recent_backtest_metrics(stress)
        print(
            f"  [{label}] 佣金 {stress.get('base_commission', 0):g}->{stress.get('commission', 0):g} "
            f"滑点 {stress.get('base_slippage', 0):g}->{stress.get('slippage', 0):g}"
        )
        for name, key in (
            ("年化收益", "annual_return"), ("Sharpe", "sharpe_ratio"),
            ("回撤", "max_drawdown"), ("交易数", "total_trades"),
        ):
            print(
                f"    {name}: {base_fmt[key]} -> {stress_fmt[key]} "
                f"(相对变化 {_stress_change(base.get(key), stress.get(key))})"
            )
    return printed


def print_sample_scope_summary(windows, train_range):
    """汇总所有已报告窗口中真正样本外的个数，避免把样本内统计误读为验证。"""
    unique = {(normalize_metric_date(s), normalize_metric_date(e)) for s, e in windows if s and e}
    if not unique:
        return
    out_count = sum(classify_sample_window(s, e, train_range)[0] == "out" for s, e in unique)
    train_text = (
        f"{normalize_metric_date(train_range[0])}->{normalize_metric_date(train_range[1])}"
        if train_range and all(train_range) else "未知"
    )
    print(
        f"样本归属汇总: 共 {len(unique)} 个报告窗口，其中 {out_count} 个完全位于训练期({train_text})结束之后，"
        f"属于真正样本外；其余窗口含训练期数据，其统计量是样本内表现，不能当作验证。"
    )


def print_yearly_validation_rows(baseline_payloads, report_payloads, train_range=None):
    rows_by_window = {}

    for payload in baseline_payloads or []:
        key = window_key(payload)
        if all(key):
            rows_by_window.setdefault(key, []).append(("当前基准", payload))

    for report in report_payloads or []:
        label = format_metric_label(report)
        for payload in report.get('yearly_backtests') or []:
            key = window_key(payload)
            if all(key):
                rows_by_window.setdefault(key, []).append((label, payload))

    if not rows_by_window:
        return False

    header_yearly = (
        f"| {'窗口 (Window)':<21} | {'样本归属':<16} | {'指标 (Metric)':<30} | {'年化收益':<10} | "
        f"{'回撤':<10} | {'Calmar':<8} | {'Sharpe':<8} | {'交易数':<8} | {'胜率':<10} | {'PF':<8} |"
    )
    yearly_width = len(header_yearly)
    print("-" * yearly_width)
    print("年度固定窗口回测结果 (Yearly Fixed-Window Backtest, Reused In-Memory Data; 仅样本外行可作验证)")
    print("-" * yearly_width)
    print(header_yearly)
    print("-" * yearly_width)

    for key in sorted(rows_by_window):
        window_text = f"{key[0]}->{key[1]}"
        scope_text = classify_sample_window(key[0], key[1], train_range)[1]
        for label, payload in rows_by_window[key]:
            fmt = format_recent_backtest_metrics(payload or {})
            print(
                f"| {window_text:<21} | {scope_text:<16} | {str(label)[:30]:<30} | "
                f"{fmt['annual_return']:<10} | {fmt['max_drawdown']:<10} | "
                f"{fmt['calmar_ratio']:<8} | {fmt['sharpe_ratio']:<8} | "
                f"{fmt['total_trades']:<8} | {fmt['win_rate']:<10} | {fmt['profit_factor']:<8} |"
            )
        print("-" * yearly_width)

    return True


def print_run_summary(
    final_reports,
    total_metrics,
    explicit_params_passed=False,
    baseline_report=None,
    baseline_test_report=None,
    baseline_yearly_reports=None,
):
    completed_metrics = len(final_reports)
    failed_metrics = max(0, total_metrics - completed_metrics)
    completed_trials = 0
    for report in final_reports:
        try:
            completed_trials += int(report.get('trials_completed') or 0)
        except (TypeError, ValueError):
            pass

    main_eval_count = sum(1 for report in final_reports if report.get('main_eval_backtest') or report.get('recent_backtest'))
    test_count = sum(1 for report in final_reports if report.get('test_backtest'))
    yearly_count = sum(len(report.get('yearly_backtests') or []) for report in final_reports)
    if baseline_report:
        main_eval_count += 1
    if baseline_test_report:
        test_count += 1
    yearly_count += len(baseline_yearly_reports or [])

    dashboard_logs = collect_dashboard_logs(final_reports)

    print("\n>>> 运行概要 (RUN SUMMARY) <<<")
    print(f"Metrics requested:  {total_metrics}")
    print(f"Metrics completed:  {completed_metrics}")
    print(f"Metrics failed:     {failed_metrics}")
    print(f"Completed trials:   {completed_trials}")
    audits = [
        (format_metric_label(report), report.get("search_audit") or {})
        for report in final_reports
        if report.get("search_audit")
    ]
    if audits:
        print("Search coverage:")
        for label, audit in audits:
            coverage = audit.get("coverage")
            coverage_text = "n/a" if coverage is None else f"{float(coverage) * 100:.2f}%"
            blocks = audit.get("n_blocks")
            blocks_text = "" if blocks is None else f"blocks={blocks} "
            ready = audit.get("ready_blocks")
            ready_text = "" if ready is None else f"ready={ready} "
            layer = audit.get("layer")
            layer_text = "" if layer is None else f"layer={layer} "
            phase = audit.get("phase")
            phase_text = "" if not phase else f"phase={phase} "
            print(
                f"  {label}: unique={audit.get('unique', 0)} "
                f"finished={audit.get('finished', 0)} "
                f"duplicates={audit.get('duplicates', 0)} "
                f"{blocks_text}{ready_text}{layer_text}{phase_text}"
                f"coverage={coverage_text} "
                f"target={audit.get('target_trials', 'n/a')} "
                f"max={audit.get('max_target_trials', 'n/a')}"
            )
    print(f"MainEval reports:   {main_eval_count}")
    print(f"TestSet reports:    {test_count}")
    print(f"Yearly reports:     {yearly_count}")
    print(f"Baseline included:  {'yes' if explicit_params_passed else 'no'}")
    print(f"Optuna log files:   {len(dashboard_logs)}")
    for log_file in dashboard_logs:
        print(f"  - {log_file}")


def collect_dashboard_logs(final_reports):
    dashboard_logs = []
    for report in final_reports:
        log_file = report.get('log_file')
        if log_file and log_file not in dashboard_logs:
            dashboard_logs.append(log_file)
    return dashboard_logs


def print_trade_attribution_sections(
    final_reports,
    explicit_params_passed=False,
    test_set_requested=False,
    baseline_report=None,
    baseline_test_report=None,
):
    printed = False

    def emit_report(label, payload):
        nonlocal printed
        report = (payload or {}).get("trade_micro_attribution_report")
        if not report:
            return
        if not printed:
            print("\n" + "=" * 60)
            print("TRADE MICRO ATTRIBUTION")
            print("=" * 60)
            printed = True
        print(f"[{label}]")
        print(report)

    if explicit_params_passed:
        emit_report("当前基准 MainEval", baseline_report)
        if test_set_requested:
            emit_report("当前基准 TestSet", baseline_test_report)

    for report in final_reports:
        label = format_metric_label(report)
        emit_report(f"{label} MainEval", report.get('main_eval_backtest') or report.get('recent_backtest'))
        if test_set_requested:
            emit_report(f"{label} TestSet", report.get('test_backtest'))


def print_optimizer_ai_summary(
    final_reports,
    total_metrics,
    original_argv,
    original_exact=True,
    explicit_params_passed=False,
    fixed_params=None,
    baseline_report=None,
    baseline_test_report=None,
    baseline_yearly_reports=None,
    baseline_elapsed_hours=None,
    test_set_requested=False,
    test_section_title=None,
    train_range=None,
):
    print(OPTIMIZER_AI_ANALYSIS_START_MARKER)
    print("Original launch command" + (":" if original_exact else " (reconstructed from saved settings):"))
    print(format_cli_command(["python", "run.py", *original_argv]))
    print()
    print(">>> 多臂赌博机训练结果汇总(MULTI-METRIC BANDIT SUMMARY)  <<<")

    header = (
        f"| {'指标 (Metric)':<30} | {'年化收益':<10} | {'回撤':<10} | "
        f"{'Calmar':<8} | {'Sharpe':<8} | {'交易数':<8} | {'胜率':<10} | {'PF':<8} | "
        f"{'耗时(h)':<8} | {'最优参数 (Params)':<22} | {'关联日志 (Log)'}"
    )
    table_width = len(header)
    print("-" * table_width)
    print(header)
    print("-" * table_width)

    if explicit_params_passed:
        print_metric_row(
            metric_label="当前基准",
            metrics_payload=baseline_report or {},
            elapsed_hours=baseline_elapsed_hours,
            params_payload=fixed_params,
            log_payload="N/A",
        )
        if final_reports:
            print("-" * table_width)

    for report in final_reports:
        print_metric_row(
            metric_label=format_metric_label(report),
            metrics_payload=report.get('main_eval_backtest') or report.get('recent_backtest') or {},
            elapsed_hours=report.get('elapsed_hours', 0),
            params_payload=report.get('best_params', 'N/A'),
            log_payload=report.get('log_file', 'N/A'),
        )

    scope_windows = []
    main_payloads = [("当前基准", baseline_report)] if explicit_params_passed else []
    main_payloads += [
        (format_metric_label(r), r.get('main_eval_backtest') or r.get('recent_backtest')) for r in final_reports
    ]
    for label, payload in main_payloads:
        print_sample_scope_line(f"{label} MainEval", payload, train_range)
        scope_windows.append(window_key(payload))
    print_cost_stress_rows(final_reports)

    if test_set_requested:
        test_title = test_section_title or "测试集回测结果 (Out-of-Sample Test Set)"
        print("-" * table_width)
        print(test_title)
        print("-" * table_width)
        print(header)
        print("-" * table_width)

        if explicit_params_passed:
            print_metric_row(
                metric_label="当前基准",
                metrics_payload=baseline_test_report or {},
                elapsed_hours=baseline_elapsed_hours,
                params_payload=fixed_params,
                log_payload="N/A",
            )
            if final_reports:
                print("-" * table_width)

        for report in final_reports:
            print_metric_row(
                metric_label=format_metric_label(report),
                metrics_payload=report.get('test_backtest') or {},
                elapsed_hours=report.get('elapsed_hours', 0),
                params_payload=report.get('best_params', 'N/A'),
                log_payload=report.get('log_file', 'N/A'),
            )

        for label, payload in (
            ([("当前基准", baseline_test_report)] if explicit_params_passed else [])
            + [(format_metric_label(r), r.get('test_backtest')) for r in final_reports]
        ):
            print_sample_scope_line(f"{label} TestSet", payload, train_range)
            scope_windows.append(window_key(payload))
        print("-" * table_width + "\n")

    print_yearly_validation_rows(baseline_yearly_reports, final_reports, train_range)
    scope_windows += [window_key(p) for p in baseline_yearly_reports or []]
    scope_windows += [window_key(p) for r in final_reports for p in r.get('yearly_backtests') or []]
    print_sample_scope_summary(scope_windows, train_range)
    print_trade_attribution_sections(
        final_reports=final_reports,
        explicit_params_passed=explicit_params_passed,
        test_set_requested=test_set_requested,
        baseline_report=baseline_report,
        baseline_test_report=baseline_test_report,
    )
    print_run_summary(
        final_reports=final_reports,
        total_metrics=total_metrics,
        explicit_params_passed=explicit_params_passed,
        baseline_report=baseline_report,
        baseline_test_report=baseline_test_report,
        baseline_yearly_reports=baseline_yearly_reports,
    )
    print(OPTIMIZER_AI_ANALYSIS_END_MARKER)
