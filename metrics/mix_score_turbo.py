import math

# 年化收益上限（百分点），只用于避免极端输入溢出，正常区间内保持线性。
_ANN_CAP = 500.0
# 回撤软降权：15% 以内不扣分，75% 时达到上限，中间线性过渡。
_MDD_FREE = 15.0
_MDD_SPAN = 60.0
_MDD_MAX_PENALTY = 40.0
# 样本量软降权：20 笔以上不扣分，0 笔时扣满 10 分。
_TRADES_FULL = 20.0
_TRADES_SLOPE = 0.5


# 评分公式或量纲变化时递增；Study 记录该版本，版本不同的旧 Study 不续传，避免新旧分数混在一起。
METRIC_VERSION = 2


def _num(value, default):
    """转成浮点数；None、NaN 和不可转换的值使用默认值，保留正负无穷交给调用方截断。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return float(default) if math.isnan(number) else number


def _ramp(value, lower, upper):
    """把 value 线性映射到 [0, 1]，lower 以下为 0，upper 以上为 1。"""
    return min(max((value - lower) / (upper - lower), 0.0), 1.0)


def _compound_annual_return_pct(total_return_pct, years):
    """把总收益百分比换成报表同口径的复合年化百分比，结果限制在 [-100, _ANN_CAP]。"""
    total_return = _num(total_return_pct, 0.0) / 100.0
    if total_return <= -1.0:
        return -100.0
    span = max(_num(years, 1.0), 0.1)
    try:
        annual_return = ((1.0 + total_return) ** (1.0 / span) - 1.0) * 100.0
    except OverflowError:
        return _ANN_CAP
    return min(max(annual_return, -100.0), _ANN_CAP)


def evaluate(stats, strat=None, args=None):
    """
    混合评分 个人账户冲锋 狂暴推进版 (Turbo)
    激进进取，收益主导，适用于A股狂暴模式。
    公式：2 * 复合年化收益(%) + 胜率平滑奖励(0~5) + 盈亏比平滑奖励(0~5) - 回撤软降权(0~40) - 样本量软降权(0~10)。
    收益只计一次（不再叠加 总收益/回撤）；所有奖惩项都是连续有界的线性斜坡，没有阈值跳变和硬门槛。
    回撤 15% 以内不扣分，75% 才扣满；交易不足 20 笔线性扣分，最多 10 分。
    """
    ann_return = _compound_annual_return_pct(stats['total_return_pct'], stats['years'])
    score_return = ann_return * 2.0

    # 胜率 40%~70%、盈亏比 1~3 之间线性给 0~5 分，两端饱和；NaN 按最差处理。
    win_bonus = 5.0 * _ramp(_num(stats['win_rate'], 0.0), 0.40, 0.70)
    pf_bonus = 5.0 * _ramp(_num(stats['profit_factor'], 0.0), 1.0, 3.0)

    # 回撤和样本量只做有界降权，不把明显更好的账户结果打成固定负分；回撤 NaN 按 100% 处理。
    mdd_penalty = _MDD_MAX_PENALTY * _ramp(_num(stats['mdd'], 100.0), _MDD_FREE, _MDD_FREE + _MDD_SPAN)
    trades = max(_num(stats['total_trades'], 0.0), 0.0)
    trade_penalty = _TRADES_SLOPE * max(0.0, _TRADES_FULL - trades)

    return score_return + win_bonus + pf_bonus - mdd_penalty - trade_penalty
