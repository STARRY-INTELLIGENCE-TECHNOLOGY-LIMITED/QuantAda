import math


def evaluate(stats, strat=None, args=None):
    """
    混合评分 美股/国际市场冲锋版 V2 (US Turbo V2 - 卡玛宽容/高盈亏比驱动)
    核心特征：
    1. 降低最大回撤的惩罚权重 (0.6次方)，允许美股在长牛中进行健康回调洗盘。
    2. 引入盈亏比 (Profit Factor) 超级杠杆，重奖“截断亏损，让利润奔跑”的长趋势跟踪参数。
    """
    mdd = stats.get('mdd', 0.0)
    total_return_pct = stats.get('total_return_pct', 0.0)
    years = stats.get('years', 1.0)
    total_trades = stats.get('total_trades', 0)
    safe_mdd = stats.get('safe_mdd', 1.0)
    win_rate = stats.get('win_rate', 0.0)
    profit_factor = stats.get('profit_factor', 0.0)

    # ==========================================
    # 数据提取：月度胜率 (Monthly Win Rate)
    # ==========================================
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

    # ==========================================
    # 核心打分逻辑 (V2 重构)
    # ==========================================

    # 1. 宽容的死亡红线
    # 25% 仍是物理死线，防止遇到类似2022年的宏观黑天鹅时账户无保护裸奔
    if mdd > 25.0: return -20.0
    # 美股趋势跟踪交易频率偏低，放宽至15次即及格（约每年3-5次极高质量的狙击）
    if total_trades < 15: return -20.0

    # 2. 收益主导基础分
    ann_return = total_return_pct / years
    score_return = ann_return * 2.0

    # 3. 降维的风险惩罚 (卡玛宽容核心)
    # 使用 0.6 次方代替原本的 1.0 次方。允许承受15%左右的洗盘去换取主升浪
    if total_return_pct > 0:
        score_risk = total_return_pct / (safe_mdd ** 0.6)
    else:
        score_risk = total_return_pct * safe_mdd

    # 4. 美股专属：盈亏比(PF)超级杠杆
    bonus = 0.0
    # 美股趋势跟踪胜率通常偏低(约40%)，达到45%以上即可给予基础奖励
    if win_rate > 0.45:
        bonus += 3.0

        # 终极杠杆：盈亏比越高，直接按比例放大收益得分！
    # 逼迫 AI 寻找能死死咬住纳斯达克/半导体长牛的参数
    if profit_factor > 1.5:
        score_return = score_return * (profit_factor / 1.5)

    # 5. 月度一致性惩罚 (稍微放宽)
    # 容忍美股季度性的机构洗盘，将红线从 40% 降至 35%
    consistency_penalty = 0.0
    if monthly_win_rate < 0.35:
        consistency_penalty = -10.0 * ((0.35 - monthly_win_rate) / 0.35)

    # 最终总分汇流
    return score_return + score_risk + bonus + consistency_penalty