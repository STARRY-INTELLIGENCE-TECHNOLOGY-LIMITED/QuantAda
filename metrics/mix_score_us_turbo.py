import math


def _compound_annual_return_pct(total_return_pct, years):
    """把总收益百分比换成报表同口径的复合年化百分比。本金亏光时返回 -100。"""
    total_return = float(total_return_pct) / 100.0
    span = float(years)
    if total_return <= -1.0:
        return -100.0
    if span <= 0.0:
        return 0.0
    annual_return = (1.0 + total_return) ** (1.0 / span) - 1.0
    if not math.isfinite(annual_return):
        return 0.0
    return annual_return * 100.0


def evaluate(stats, strat=None, args=None):
    """
    混合评分 美股/国际市场冲锋版 (US Turbo)
    基于 A 股 Turbo 评分公式，增加月度胜率一致性惩罚。
    目标：收益主导，同时压制单年度暴利但月度持续性不足的过拟合参数。
    回撤超过 25% 或交易不足 20 笔时只做有界降权，保留账户收益排序。
    年化收益使用报表同口径的复合年化，不用总收益除以年数。
    """
    mdd = stats['mdd']
    total_return_pct = stats['total_return_pct']
    years = stats['years']
    total_trades = stats['total_trades']
    safe_mdd = stats['safe_mdd']
    win_rate = stats['win_rate']
    profit_factor = stats['profit_factor']

    monthly_win_rate = stats.get('monthly_win_rate')
    if monthly_win_rate is None and strat is not None:
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
            monthly_win_rate = (
                sum(1 for monthly_return in active_monthly_returns if monthly_return > 0) / len(active_monthly_returns)
                if active_monthly_returns else 0.0
            )
        except Exception:
            monthly_win_rate = 0.0
    try:
        monthly_win_rate = 0.0 if monthly_win_rate is None else float(monthly_win_rate)
    except (TypeError, ValueError):
        monthly_win_rate = 0.0
    if not math.isfinite(monthly_win_rate):
        monthly_win_rate = 0.0
    if monthly_win_rate > 1.0:
        monthly_win_rate = monthly_win_rate / 100.0
    monthly_win_rate = max(0.0, min(monthly_win_rate, 1.0))

    # 1. 收益主导 (年化收益越高，分数线性放大)
    ann_return = _compound_annual_return_pct(total_return_pct, years)
    score_return = ann_return * 2.0

    # 2. 弱化的风险惩罚 (使用 1.0 次方代替原本红队的 1.5 次方)
    if total_return_pct > 0:
        score_risk = total_return_pct / (safe_mdd ** 1.0)
    else:
        score_risk = total_return_pct * safe_mdd

    # 3. 高胜率/高盈亏比彩蛋奖励
    bonus = 0.0
    if win_rate > 0.55: bonus += 5.0
    if profit_factor > 2.0: bonus += 5.0

    # 4. 月度一致性惩罚：低于 40% 正收益月份占比时线性扣分
    consistency_penalty = 0.0
    if monthly_win_rate < 0.40:
        consistency_penalty = -10.0 * ((0.40 - monthly_win_rate) / 0.40)

    # 5. 回撤和样本量只做有界降权，不把明显更好的账户结果打成固定 -20。
    mdd_penalty = 0.5 * max(0.0, float(mdd) - 25.0)
    trade_penalty = 0.5 * max(0.0, 20.0 - float(total_trades))

    return score_return + score_risk + bonus + consistency_penalty - mdd_penalty - trade_penalty
