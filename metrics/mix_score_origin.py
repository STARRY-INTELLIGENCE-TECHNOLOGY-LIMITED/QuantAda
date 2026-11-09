# 评分公式或量纲变化时递增；Study 记录该版本，版本不同的旧 Study 不续传，避免新旧分数混在一起。
METRIC_VERSION = 2


def _num(value):
    """NaN 和不可转换的值按 0 处理，正负无穷饱和为 ±1e9，保证得分有限且单调。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if number != number else min(max(number, -1e9), 1e9)


def evaluate(stats, strat=None, args=None):
    """
    混合评分 负对照组 适合A股的原始版 (Sharpe/16)
    测试策略绝对收益上限的“野生”基准线。
    目标：无脑刷分，暴露策略在没有任何回撤惩罚下的绝对收益上限。

    尽管错误地除以了 16（255个交易日求根号），Sharpe 比率在总分中的权重被彻底“物理阉割”了，它的贡献度甚至不到 1%，完美契合了 A 股“牛短熊长、暴涨暴跌、情绪市”的底层生态。。
    公式在实质上变成了一个纯粹的“双因子暴力驱动模型”：只看总收益（Return）和抗回撤能力（Calmar），完全无视波动率。
    """
    # 1. 提取标准化特征
    # 注意：系统底层 stats['total_return_pct'] 是百分比 (如 504.0)
    # 原始逻辑中使用的是小数形式 (如 5.04)，所以需要除以 100.0 还原量纲
    total_return_raw = _num(stats['total_return_pct']) / 100.0

    calmar = _num(stats['calmar'])
    sharpe = _num(stats['sharpe'])
    total_trades = _num(stats['total_trades'])

    # 2. 原始权重精确还原
    # Calmar: 2.0 (生存第一)
    # Return: 2.0 (收益第二)
    # Sharpe: 1.0 / 16.0 (平滑第三)
    raw_score = (calmar * 2.0) + (total_return_raw * 2.0) + (sharpe / 16.0)

    # 3. 样本量惩罚：交易不足 10 笔时按缺口线性扣分，0 笔扣满 10 分；满 10 笔无惩罚，不再有阈值跳变。
    penalty = -10.0 * max(0.0, 10.0 - total_trades) / 10.0

    return raw_score + penalty
