"""覆盖会产生副作用的 paper smoke worker cleanup guard。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


_WORKER_PATH = Path(__file__).with_name("_live_rebalance_worker.py")
_SPEC = importlib.util.spec_from_file_location("quantada_live_rebalance_worker", _WORKER_PATH)
_WORKER = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_WORKER)


def test_gm_worker_skips_placeholder_strategy_id(monkeypatch):
    """config 占位符不得传入 GM SDK strategy binding。"""
    monkeypatch.delenv("QUANTADA_GM_STRATEGY_ID", raising=False)
    monkeypatch.setenv("QUANTADA_GM_TOKEN", "paper-token")

    with pytest.raises(_WORKER.BrokerUnavailable, match="QUANTADA_GM_STRATEGY_ID"):
        _WORKER._build_gm_broker("SHSE.510300", 1000.0)


class _Proxy:
    def __init__(self, side="BUY"):
        self.id = f"{side}-1"
        self.status = "Submitted"

    def is_completed(self):
        return self.status == "Filled"

    def is_rejected(self):
        return self.status == "Rejected"

    def is_canceled(self):
        return self.status in {"Cancelled", "ApiCancelled"}

    def is_pending(self):
        return self.status in {"Submitted", "PendingSubmit", "PreSubmitted"}


class _Broker:
    def __init__(self):
        self.data = SimpleNamespace(_name="SPY.ARCA")
        self.buy_proxy = _Proxy("BUY")
        self.sell_proxy = _Proxy("SELL")
        self.cancelled = []
        self.position_size = 0.5
        self.sell_submitted = False

    def cancel_pending_order(self, order_id):
        self.cancelled.append(order_id)
        self.buy_proxy.status = "ApiCancelled"
        return True

    def get_position(self, data):
        return SimpleNamespace(size=self.position_size)

    def order_target_value(self, data, target):
        assert target == 0.0
        self.sell_submitted = True
        self.position_size = 0.0
        self.sell_proxy.status = "Filled"
        return self.sell_proxy


def test_ib_timeout_cancels_buy_and_flattens_late_fill_residual():
    """超时后仍必须撤销 BUY，并提交残余仓位的 cleanup SELL。"""
    broker = _Broker()

    with pytest.raises(_WORKER.SmokeFailure, match="did not reach Filled"):
        _WORKER._wait_for_order_terminal(
            broker,
            broker.buy_proxy,
            side="BUY",
            timeout_seconds=1.0,
        )

    assert broker.cancelled == ["BUY-1"]
    _WORKER._cleanup_ib_position(
        broker,
        broker.data,
        broker.data._name,
        broker.order_target_value,
    )
    assert broker.sell_submitted
    assert broker.position_size == 0.0
