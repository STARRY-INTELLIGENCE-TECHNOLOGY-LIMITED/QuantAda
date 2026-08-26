from common.formatters import format_with_spec


def format_backtest_results_report(metrics, attribution_report=None):
    """
    为已完成的回测构造终端报告。
    """
    if not metrics:
        return "Backtest generated no valid performance metrics."

    lines = []
    if attribution_report:
        lines.append(str(attribution_report).rstrip())

    lines.extend([
        "",
        "=" * 50,
        "            Backtest Performance Metrics",
        "=" * 50,
        f" Time Frame:           {metrics['start_date'].strftime('%Y-%m-%d')} to {metrics['end_date'].strftime('%Y-%m-%d')}",
        f" Initial Portfolio:    {metrics['initial_portfolio']:,.2f}",
        f" Final Portfolio:      {metrics['final_portfolio']:,.2f}",
        "-" * 50,
        f" Total Return:         {format_with_spec(metrics['total_return'], '.2%')}",
        f" Annualized Return:    {format_with_spec(metrics['annual_return'], '.2%')}",
        f" Sharpe Ratio:         {format_with_spec(metrics['sharpe_ratio'], '.2f')}",
        f" Max Drawdown:         {format_with_spec(metrics['max_drawdown'], '.2%')}",
        f" Calmar Ratio:         {format_with_spec(metrics['calmar_ratio'], '.2f')}",
        "-" * 50,
        f" Total Trades:         {metrics['total_trades']}",
        f" Win Rate:             {format_with_spec(metrics['win_rate'], '.2f')}%",
        f" Profit Factor:        {format_with_spec(metrics['profit_factor'], '.2f')}",
        f" Avg. Win / Avg. Loss: {format_with_spec(metrics['pnl_ratio'], '.2f')}",
        "=" * 50,
        "",
    ])
    return "\n".join(lines)
