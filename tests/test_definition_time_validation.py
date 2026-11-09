"""定义期错误必须快速失败：配置非法、类不合法、风控参数拼写错误；运行期风控失败首次升级报警。"""

import textwrap

import pandas as pd
import pytest

import config
from common.loader import get_class_from_name
from data_providers.base_provider import BaseDataProvider, ProviderConfigError
from data_providers.manager import DataManager
from live_trader.engine import _RiskControlChain
from risk_controls.sample_trend_protection import SampleTrendProtection


class _Broker:
    def __init__(self):
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)


# ---- 数据源配置校验 ----

@pytest.mark.parametrize("bad", [3, -1, "abc", None])
def test_gm_invalid_price_adjustment_raises_instead_of_returning_none(monkeypatch, bad):
    import data_providers.gm_provider as gm_module

    monkeypatch.setattr(gm_module, "history", lambda **kwargs: pytest.fail("不应访问 SDK"))
    monkeypatch.setattr(config, "GM_PRICE_ADJUSTMENT", bad, raising=False)
    provider = gm_module.GmDataProvider(token="historical-token")
    with pytest.raises(ProviderConfigError, match="GM_PRICE_ADJUSTMENT"):
        provider.get_data("SHSE.510300", "20240101", "20240131", "Days", 1)


def test_data_manager_chain_reraises_config_error_but_falls_through_on_data_errors():
    class BrokenData(BaseDataProvider):
        def get_data(self, *args, **kwargs):
            raise RuntimeError("network down")

    class Good(BaseDataProvider):
        def get_data(self, *args, **kwargs):
            return pd.DataFrame(
                {"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [10.0]},
                index=pd.to_datetime(["2024-01-02"]),
            )

    class BadConfig(BaseDataProvider):
        def get_data(self, *args, **kwargs):
            raise ProviderConfigError("bad config")

    manager = DataManager()
    # 数据/网络类异常仍回退到下一个 Provider。
    assert manager._fetch_from_providers("X", None, None, [BrokenData(), Good()]) is not None
    # 配置错误不得回退。
    with pytest.raises(ProviderConfigError):
        manager._fetch_from_providers("X", None, None, [BadConfig(), Good()])


# ---- 风控链首次失败升级 ----

class _Boom:
    def __init__(self):
        self.broker = _Broker()

    def check(self, data):
        raise AttributeError("missing method")

    def notify_order(self, order):
        raise RuntimeError("notify boom")


class _Ok:
    broker = None

    def check(self, data):
        return "SELL"

    def notify_order(self, order):
        pass


def test_risk_chain_alarms_once_per_control_and_method_but_keeps_running():
    alarms = []
    boom = _Boom()
    chain = _RiskControlChain([boom, _Ok()], on_failure=alarms.append)

    for _ in range(3):
        assert chain.check(object()) == "SELL"  # 坏风控不阻断后面的风控
    assert len(alarms) == 1 and "_Boom.check" in alarms[0]
    assert len(boom.broker.logs) == 3  # 每次仍写日志

    chain.notify_order(object())
    chain.notify_order(object())
    assert len(alarms) == 2 and "_Boom.notify_order" in alarms[1]


def test_risk_chain_alarm_failure_does_not_break_chain():
    def broken_alarm(msg):
        raise RuntimeError("IM down")

    chain = _RiskControlChain([_Boom(), _Ok()], on_failure=broken_alarm)
    assert chain.check(object()) == "SELL"


# ---- 注册期校验 ----

def test_trend_protection_rejects_invalid_method_and_period_at_construction():
    with pytest.raises(ValueError, match="method"):
        SampleTrendProtection(_Broker(), {"method": "trend_scor"})
    with pytest.raises(ValueError, match="period"):
        SampleTrendProtection(_Broker(), {"period": 1})
    assert SampleTrendProtection(_Broker(), {"method": "trend_score"}).p.method == "trend_score"


def test_loader_rejects_non_subclass_and_abstract_plugins(tmp_path, monkeypatch):
    (tmp_path / "plugin_defs_mod.py").write_text(textwrap.dedent("""\
        from risk_controls.base_risk_control import BaseRiskControl
        from stock_selectors.base_selector import BaseSelector

        class NotRisk:
            pass

        class AbstractRisk(BaseRiskControl):
            pass

        class AbstractSelector(BaseSelector):
            pass

        class GoodRisk(BaseRiskControl):
            def check(self, data):
                return None
        """), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(TypeError, match="not a subclass"):
        get_class_from_name("plugin_defs_mod.NotRisk", ["risk_controls", "strategies"])
    with pytest.raises(TypeError, match="abstract"):
        get_class_from_name("plugin_defs_mod.AbstractRisk", ["risk_controls", "strategies"])
    with pytest.raises(TypeError, match="abstract"):
        get_class_from_name("plugin_defs_mod.AbstractSelector", ["stock_selectors"])
    assert get_class_from_name("plugin_defs_mod.GoodRisk", ["risk_controls", "strategies"]).__name__ == "GoodRisk"
    # 策略路径不受风控/选股器校验影响。
    assert get_class_from_name("plugin_defs_mod.NotRisk", ["strategies"]).__name__ == "NotRisk"
    assert get_class_from_name("sample_trend_protection", ["risk_controls"]) is SampleTrendProtection
