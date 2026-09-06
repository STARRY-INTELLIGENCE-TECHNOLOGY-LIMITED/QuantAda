from types import SimpleNamespace

import pandas as pd
import pytest

from live_trader.adapters.futu_broker import FutuBrokerAdapter
from common.options.contracts import (
    InvalidOptionOrderEffect,
    apply_signed_position,
    signed_position_delta,
    validate_option_order_effect,
)
from common.options.risk import OptionRiskLeg


class _Trade:
    def __init__(self, positions=None):
        self.positions = positions if positions is not None else pd.DataFrame()
        self.orders = pd.DataFrame()
        self.place_calls = []

    def set_sync_query_connect_timeout(self, _timeout):
        pass

    def accinfo_query(self, **_kwargs):
        return 0, pd.DataFrame([{'available_funds': 10000, 'total_assets': 10000}])

    def position_list_query(self, **_kwargs):
        return 0, self.positions.copy()

    def order_list_query(self, **_kwargs):
        return 0, self.orders.copy()

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return 0, pd.DataFrame([{
            'code': kwargs['code'],
            'trd_side': kwargs['trd_side'],
            'order_status': 'SUBMITTED',
            'order_id': f'ORDER-{len(self.place_calls)}',
            'qty': kwargs['qty'],
            'price': kwargs['price'],
        }])


def _broker(positions=None):
    trade = _Trade(positions)
    context = SimpleNamespace(
        futu_trade_context=trade,
        _futu_runtime_config={'FUTU_ACCOUNT_CURRENCY': 'USD'},
    )
    broker = FutuBrokerAdapter(context)
    broker._contract_multipliers['US.AAPL260918P320000'] = 100.0
    return broker, trade


def test_signed_position_effects_are_deterministic():
    assert signed_position_delta('BUY_TO_OPEN', 2) == 2
    assert signed_position_delta('SELL_TO_CLOSE', 2) == -2
    assert signed_position_delta('SELL_TO_OPEN', 2) == -2
    assert signed_position_delta('BUY_TO_CLOSE', 2) == 2
    assert apply_signed_position(3, 'SELL_TO_CLOSE', 1) == 2
    assert apply_signed_position(-3, 'BUY_TO_CLOSE', 1) == -2


def test_invalid_effect_and_position_boundaries_fail_closed():
    with pytest.raises(InvalidOptionOrderEffect):
        validate_option_order_effect('SELL_TO_OPEN', 0, 1)
    with pytest.raises(InvalidOptionOrderEffect):
        validate_option_order_effect('SELL_TO_CLOSE', 0, 1)
    with pytest.raises(InvalidOptionOrderEffect):
        validate_option_order_effect('BUY_TO_CLOSE', 2, 1)


def test_futu_explicit_sell_to_close_preserves_effect_and_direction():
    option = 'US.AAPL260918P320000'
    broker, trade = _broker(pd.DataFrame([{
        'code': option,
        'position_market': 'US',
        'qty': 3,
        'can_sell_qty': 3,
        'average_cost': 10.0,
    }]))
    data = SimpleNamespace(_name=option)

    proxy = broker.submit_option_order(data, 1, 'SELL_TO_CLOSE', price=11.0)

    assert proxy is not None
    assert proxy.order_effect == 'SELL_TO_CLOSE'
    assert trade.place_calls[0]['trd_side'] == 'SELL'


def test_futu_short_option_can_only_use_buy_to_close_and_sell_to_open_is_blocked():
    option = 'US.AAPL260918P320000'
    broker, trade = _broker(pd.DataFrame([{
        'code': option,
        'position_market': 'US',
        'position_side': 'SHORT',
        'qty': 2,
        'average_cost': 10.0,
    }]))
    data = SimpleNamespace(_name=option)

    close_proxy = broker.submit_option_order(data, 1, 'BUY_TO_CLOSE', price=9.0)
    rejected = broker.submit_option_order(data, 1, 'SELL_TO_OPEN', price=9.0)

    assert broker.get_position(data).size == -2
    assert close_proxy is not None
    assert close_proxy.order_effect == 'BUY_TO_CLOSE'
    assert trade.place_calls[0]['trd_side'] == 'BUY'
    assert rejected is None
    assert len(trade.place_calls) == 1


def test_futu_broker_margin_snapshot_uses_real_cash_and_managed_underlying():
    broker, _ = _broker()
    data = SimpleNamespace(_name='US.AAPL')
    broker.set_datas([data])
    leg = OptionRiskLeg(
        'US.AAPL260918P050000', 'US.AAPL', 'PUT', -1, 50, 5, 300, 100
    )
    snapshot = broker.get_option_margin_snapshot(
        [leg], underlying_positions={'US.AAPL': 0}
    )
    assert snapshot.margin_used == 5000
    assert snapshot.available_margin == 5000


def test_futu_sell_to_open_requires_explicit_secured_risk_leg():
    option = 'US.AAPL260918P050000'
    broker, trade = _broker()
    data = SimpleNamespace(_name=option)
    leg = OptionRiskLeg(option, 'US.AAPL', 'PUT', -1, 50, 5, 300, 100)

    assert broker.submit_option_order(data, 1, 'SELL_TO_OPEN', price=5) is None
    proxy = broker.submit_option_order(
        data,
        1,
        'SELL_TO_OPEN',
        price=5,
        allow_sell_to_open=True,
        risk_leg=leg,
        underlying_positions={'US.AAPL': 0},
    )

    assert proxy is not None
    assert proxy.order_effect == 'SELL_TO_OPEN'
    assert trade.place_calls[-1]['trd_side'] == 'SELL'


def test_futu_sell_to_open_rejects_positive_risk_leg():
    option = 'US.AAPL260918P050000'
    broker, trade = _broker()
    data = SimpleNamespace(_name=option)
    long_leg = OptionRiskLeg(option, 'US.AAPL', 'PUT', 1, 50, 5, 300, 100)

    rejected = broker.submit_option_order(
        data,
        1,
        'SELL_TO_OPEN',
        price=5,
        allow_sell_to_open=True,
        risk_leg=long_leg,
        underlying_positions={'US.AAPL': 0},
    )

    assert rejected is None
    assert trade.place_calls == []


def test_futu_csp_cash_is_rebuilt_from_short_positions_and_pending_orders():
    option = 'US.AAPL260918P050000'

    class CSPTrade(_Trade):
        def __init__(self):
            super().__init__(pd.DataFrame([{
                'code': option,
                'position_market': 'US',
                'position_side': 'SHORT',
                'qty': 1,
                'average_cost': 5.0,
                'option_contract_multiplier': 100,
            }]))
            self.orders = pd.DataFrame()

    broker, _ = _broker()
    broker._trade_ctx = CSPTrade()
    broker.trade_ctx = broker._trade_ctx
    broker.trd_ctx = broker._trade_ctx

    obligations = broker.get_option_assignment_obligations()
    assert obligations['assignment_cash'] == 5000
    assert broker.get_csp_uncommitted_cash() == 5000
    assert broker.get_rebalance_cash() == 5000

    rejected = broker.submit_option_order(
        SimpleNamespace(_name=option),
        2,
        'SELL_TO_OPEN',
        price=5,
        allow_sell_to_open=True,
        risk_leg=OptionRiskLeg(option, 'US.AAPL', 'PUT', -2, 50, 5, 300, 100),
        underlying_positions={'US.AAPL': 0},
    )
    assert rejected is None
    assert broker._last_order_target_skip_reason == 'unsupported_option_order_effect'
