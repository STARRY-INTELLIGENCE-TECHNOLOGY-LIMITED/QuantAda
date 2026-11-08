from risk_controls.sample_trend_protection import SampleTrendProtection


class _DummyBroker:
    def __init__(self, datas):
        self.datas = datas


class _BacktestCloseLine:
    def __init__(self, closes):
        self._closes = list(closes)

    def get(self, ago=0, size=None):
        if size is None:
            return self._closes
        return self._closes[-size:]


class _BacktestLikeData:
    def __init__(self, name, closes):
        self._name = name
        self._closes = list(closes)
        self.close = _BacktestCloseLine(self._closes)

    def __len__(self):
        return len(self._closes)


class _FrameworkLine:
    def __init__(self, values):
        self._values = list(values)

    def __getitem__(self, idx):
        if idx == 0:
            return self._values[-1]
        raise IndexError("Only [0] is needed in this test.")

    def get(self, ago=0, size=None):
        if size is None:
            return self._values
        return self._values[-size:]


class _FrameworkLikeData:
    def __init__(self, name, closes):
        self._name = name
        self._closes = list(closes)
        self.close = _FrameworkLine(self._closes)

    def __len__(self):
        return len(self._closes)


def test_trend_protection_framework_proxy_contract_stays_simple():
    """
    框架契约回归:
    只要框架给到 len(data) + data.close.get(...)，样例风控应保持最简单写法。
    """
    symbol = "SHSE.600000"
    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.0]
    broker = _DummyBroker(datas=[])
    risk = SampleTrendProtection(
        broker=broker,
        params={"method": "ma", "period": 3, "strict_slope": False},
    )

    data = _FrameworkLikeData(symbol, closes)
    action = risk.check(data)

    assert action == "SELL", "框架代理契约下应正确触发均线风控。"


def test_trend_protection_backtest_path_still_works():
    """
    回测兼容回归:
    原始 backtrader 风格数据路径仍应保持可用。
    """
    symbol = "SHSE.600000"
    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.0]

    data = _BacktestLikeData(symbol, closes)
    broker = _DummyBroker(datas=[])
    risk = SampleTrendProtection(
        broker=broker,
        params={"method": "ma", "period": 3, "strict_slope": False},
    )

    action = risk.check(data)
    assert action == "SELL", "回测路径下均线风控行为不应被实盘兼容补丁破坏。"


def test_trend_protection_returns_none_when_history_not_enough():
    """
    数据门槛回归:
    历史长度不足 period+5 时，不应误触发 SELL。
    """
    symbol = "SHSE.600000"
    closes = [10.0, 10.1, 10.2, 10.3, 10.2, 10.1]
    broker = _DummyBroker(datas=[])
    risk = SampleTrendProtection(
        broker=broker,
        params={"method": "ma", "period": 3, "strict_slope": False},
    )

    data = _FrameworkLikeData(symbol, closes)
    action = risk.check(data)

    assert action is None, "历史不足时应直接返回 None，避免误平仓。"


def test_trend_score_sells_on_steady_downtrend_and_holds_on_uptrend():
    """trend_score 模式回归: 平稳下跌应 SELL，平稳上涨不应触发。"""
    broker = _DummyBroker(datas=[])
    risk = SampleTrendProtection(
        broker=broker,
        params={"method": "trend_score", "period": 10, "threshold": 0.0},
    )

    down = _FrameworkLikeData("SHSE.600000", [20.0 - 0.2 * i for i in range(20)])
    up = _FrameworkLikeData("SHSE.600000", [10.0 + 0.2 * i for i in range(20)])

    assert risk.check(down) == "SELL"
    assert risk.check(up) is None


def test_trend_score_threshold_filters_weak_uptrend():
    """trend_score 模式回归: 弱上涨得分低于阈值时也应 SELL。"""
    broker = _DummyBroker(datas=[])
    risk = SampleTrendProtection(
        broker=broker,
        params={"method": "trend_score", "period": 10, "threshold": 10.0},
    )
    weak_up = _FrameworkLikeData("SHSE.600000", [10.0 + 0.001 * i for i in range(20)])

    assert risk.check(weak_up) == "SELL"
