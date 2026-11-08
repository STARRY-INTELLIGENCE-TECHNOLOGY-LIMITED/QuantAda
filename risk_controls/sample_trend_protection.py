import numpy as np

from common import mytt
from risk_controls.base_risk_control import BaseRiskControl


class SampleTrendProtection(BaseRiskControl):
    """
    趋势保护风控模块 (Trend Protection)

    用于判断当前市场/个股的形势。当形势恶化时，强制空仓(SELL)，
    防止在主跌浪中'接飞刀'。

    支持两种判定方法 (通过 'method' 参数切换):
    1. 'ma': 价格与均线关系。
       - 逻辑: Close < MA(period) -> SELL
       - 适用: 简单的熊市过滤器。

    2. 'trend_score': 趋势得分 (Annualized Return * R^2)。
       - 逻辑: TrendScore < threshold -> SELL
       - 适用: 识别平稳下跌的趋势，过滤震荡市。
    """

    params = {
        'method': 'ma',  # 'ma' 或 'trend_score'
        'period': 60,  # 均线周期 或 趋势得分的计算窗口
        'threshold': 0.0,  # 趋势得分阈值 (仅 method='trend_score' 时有效)
        'strict_slope': False  # (仅 ma 模式) 是否要求均线本身必须向上
    }

    def check(self, data) -> str:
        # 1. 准备数据
        # 获取该标的截至当前的所有收盘价 (Backtrader中 len(data) 为当前已处理bar数)
        # 转换为 numpy array 供 mytt 使用
        current_len = len(data)

        # 兜底: 数据不足时不操作
        if current_len < self.p.period + 5:
            return None

        close = np.array(data.close.get(ago=0, size=current_len))

        # 2. 根据方法执行检查
        signal = None

        if self.p.method == 'ma':
            signal = self._check_ma(close)
        elif self.p.method == 'trend_score':
            signal = self._check_trend_score(close)

        return signal

    def _check_ma(self, close):
        """MA均线逻辑"""
        period = self.p.period

        # 计算均线
        ma_values = mytt.MA(close, period)

        current_close = close[-1]
        current_ma = ma_values[-1]

        # 核心逻辑: 价格在均线之下，视为弱势，清仓
        if current_close < current_ma:
            return 'SELL'

        # 可选逻辑: 均线本身是否向下 (均线拐头向下)
        if self.p.strict_slope:
            # 比较当前MA和昨日MA
            prev_ma = ma_values[-2]
            if current_ma < prev_ma:
                return 'SELL'

        return None

    def _check_trend_score(self, close):
        """趋势得分逻辑: 对最近 period 根收盘价取对数做线性回归，得分 = 年化收益 * R^2，低于 threshold 则 SELL。"""
        window = np.asarray(close[-int(self.p.period):], dtype=float)
        # 非正价格或缺失值无法取对数，保守不操作，避免误平仓。
        if window.size < 2 or not np.all(np.isfinite(window)) or np.any(window <= 0):
            return None

        y = np.log(window)
        x = np.arange(window.size, dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        # 价格完全不变时 R^2 无定义，趋势得分视为 0。
        if ss_tot <= 0:
            score = 0.0
        else:
            residual = y - (slope * x + intercept)
            r_squared = 1.0 - float(np.sum(residual ** 2)) / ss_tot
            # 按 250 个交易日年化日频对数斜率。
            annualized_return = float(np.exp(slope * 250) - 1.0)
            score = annualized_return * r_squared

        if score < float(self.p.threshold):
            return 'SELL'
        return None
