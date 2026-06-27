import re

import pandas as pd

from common.formatters import format_float, format_recent_backtest_metrics


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


def print_yearly_validation_rows(baseline_payloads, report_payloads):
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
        f"| {'窗口 (Window)':<21} | {'指标 (Metric)':<30} | {'年化收益':<10} | {'回撤':<10} | "
        f"{'Calmar':<8} | {'Sharpe':<8} | {'交易数':<8} | {'胜率':<10} | {'PF':<8} |"
    )
    yearly_width = len(header_yearly)
    print("-" * yearly_width)
    print("年度固定窗口回测结果 (Yearly Fixed-Window Validation, Reused In-Memory Data)")
    print("-" * yearly_width)
    print(header_yearly)
    print("-" * yearly_width)

    for key in sorted(rows_by_window):
        window_text = f"{key[0]}->{key[1]}"
        for label, payload in rows_by_window[key]:
            fmt = format_recent_backtest_metrics(payload or {})
            print(
                f"| {window_text:<21} | {str(label)[:30]:<30} | "
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
    explicit_params_passed=False,
    fixed_params=None,
    baseline_report=None,
    baseline_test_report=None,
    baseline_yearly_reports=None,
    baseline_elapsed_hours=None,
    test_set_requested=False,
    test_section_title=None,
):
    print("=== 请忽略上文日志输出，请将下文提供给AI辅助分析 ===")
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

        print("-" * table_width + "\n")

    print_yearly_validation_rows(baseline_yearly_reports, final_reports)
    print_trade_attribution_sections(
        final_reports=final_reports,
        explicit_params_passed=explicit_params_passed,
        test_set_requested=test_set_requested,
        baseline_report=baseline_report,
        baseline_test_report=baseline_test_report,
    )
    print("=== 请将上文提供给AI辅助分析 ===")
